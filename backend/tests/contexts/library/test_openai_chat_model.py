import json
import logging
import urllib.error

import pytest

from src.contexts.library.domain.chat import ChatContext, ContextChunk, FinishReason
from src.contexts.library.infrastructure.openai_chat_model import (
    LLM_UNAVAILABLE_MESSAGE,
    OpenAiChatModel,
)
from src.shared_kernel.domain.errors import ChatStreamError


class FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def __iter__(self):
        return iter(self._lines)


class RaisingResponse:
    """Raises mid-iteration -- simulates a dropped connection after some
    frames were already delivered."""

    def __init__(self, lines: list[bytes], error: Exception) -> None:
        self._lines = lines
        self._error = error

    def __enter__(self) -> "RaisingResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def __iter__(self):
        for line in self._lines:
            yield line
        raise self._error


def _http_error(status: int, request_id: str | None = None) -> urllib.error.HTTPError:
    headers = {"x-request-id": request_id} if request_id else {}
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions", status, "error", headers, None
    )


@pytest.fixture
def context() -> ChatContext:
    return ChatContext(
        book_id="b1",
        book_title="T",
        chunks_total=1,
        anchor_chunk=0,
        chunks=(ContextChunk(0, "txt", 1, 1, True),),
        history=(),
        question="Q",
    )


def _model(http, **overrides) -> OpenAiChatModel:
    kwargs = dict(api_key="sk-secret-value", model="gpt-4.1-mini", max_output_tokens=700, http=http)
    kwargs.update(overrides)
    return OpenAiChatModel(**kwargs)


# 1. Request shape ------------------------------------------------------------


def test_request_shape(context) -> None:
    captured: dict = {}

    def http(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        return FakeResponse([b"data: [DONE]\n"])

    model = _model(http)
    list(model.stream(context))

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-secret-value"
    body = captured["body"]
    assert body["model"] == "gpt-4.1-mini"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_completion_tokens"] == 700
    assert body["temperature"] == 0.2


# 2. Delta mapping --------------------------------------------------------------


def test_delta_mapping_skips_frames_with_no_content(context) -> None:
    lines = [
        b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n',
        b'data: {"choices": [{"delta": {}}]}\n',
        b'data: {"choices": [{"delta": {"content": " World"}}]}\n',
        b"data: [DONE]\n",
    ]
    model = _model(lambda request, timeout: FakeResponse(lines))
    deltas = [d for d in model.stream(context) if d.text]
    assert [d.text for d in deltas] == ["Hello", " World"]


# 3. finish_reason mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("stop", FinishReason.END_TURN),
        ("length", FinishReason.MAX_TOKENS),
        ("content_filter", FinishReason.REFUSAL),
        ("something_new", FinishReason.END_TURN),
    ],
)
def test_finish_reason_mapping(context, raw, expected) -> None:
    lines = [
        json.dumps({"choices": [{"delta": {}, "finish_reason": raw}]}).encode(),
    ]
    lines = [b"data: " + lines[0] + b"\n", b"data: [DONE]\n"]
    model = _model(lambda request, timeout: FakeResponse(lines))
    finish_deltas = [d for d in model.stream(context) if d.finish_reason is not None]
    assert finish_deltas[0].finish_reason == expected


# 4. usage, including cached_tokens ----------------------------------------------


def test_usage_reaches_result(context) -> None:
    lines = [
        b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n',
        b'data: {"choices": [], "usage": {"prompt_tokens": 4103, "completion_tokens": 312, '
        b'"prompt_tokens_details": {"cached_tokens": 2816}}}\n',
        b"data: [DONE]\n",
    ]
    model = _model(lambda request, timeout: FakeResponse(lines))
    usage_deltas = [d for d in model.stream(context) if d.usage is not None]
    assert len(usage_deltas) == 1
    usage = usage_deltas[0].usage
    assert usage.input_tokens == 4103
    assert usage.output_tokens == 312
    assert usage.cached_input_tokens == 2816


# 5. [DONE] terminates cleanly ----------------------------------------------------


def test_done_sentinel_terminates_cleanly(context) -> None:
    lines = [
        b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n',
        b"data: [DONE]\n",
        b'data: {"choices": [{"delta": {"content": "should never arrive"}}]}\n',
    ]
    model = _model(lambda request, timeout: FakeResponse(lines))
    deltas = [d.text for d in model.stream(context) if d.text]
    assert deltas == ["Hi"]


# 6. malformed JSON frame is skipped with a WARNING, stream continues ------------


def test_malformed_frame_is_skipped_with_warning(context, caplog) -> None:
    lines = [
        b"data: {not json}\n",
        b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n',
        b"data: [DONE]\n",
    ]
    model = _model(lambda request, timeout: FakeResponse(lines))
    with caplog.at_level(logging.WARNING):
        deltas = [d.text for d in model.stream(context) if d.text]
    assert deltas == ["Hi"]
    assert any("malformed" in record.message for record in caplog.records)


# 7. 429 before the first token -> exactly one retry, then success ---------------


def test_retries_once_on_429_before_first_token(context, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.contexts.library.infrastructure.openai_chat_model.time.sleep", lambda s: None
    )
    calls = {"count": 0}

    def http(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _http_error(429)
        return FakeResponse([b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n', b"data: [DONE]\n"])

    model = _model(http)
    deltas = [d.text for d in model.stream(context) if d.text]
    assert deltas == ["Hi"]
    assert calls["count"] == 2


# 8. 429 -> 429 -> fixed public message, not the upstream body -------------------


def test_double_429_raises_fixed_message(context, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.contexts.library.infrastructure.openai_chat_model.time.sleep", lambda s: None
    )
    calls = {"count": 0}

    def http(request, timeout):
        calls["count"] += 1
        raise _http_error(429)

    model = _model(http)
    with pytest.raises(ChatStreamError) as exc:
        list(model.stream(context))

    assert calls["count"] == 2
    assert exc.value.code == "LLM_UNAVAILABLE"
    assert exc.value.public_message == LLM_UNAVAILABLE_MESSAGE


# 9. socket error after the first token -> ChatStreamError, no retry -------------


def test_no_retry_after_first_token(context) -> None:
    calls = {"count": 0}

    def http(request, timeout):
        calls["count"] += 1
        return RaisingResponse(
            [b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n'],
            ConnectionError("dropped"),
        )

    model = _model(http)
    stream = model.stream(context)
    first = next(stream)
    assert first.text == "Hi"

    with pytest.raises(ChatStreamError) as exc:
        next(stream)
    assert exc.value.code == "STREAM_ERROR"
    assert calls["count"] == 1


# 10. repr/str never contain the key ---------------------------------------------


def test_repr_and_str_do_not_contain_the_key(context) -> None:
    model = _model(lambda request, timeout: FakeResponse([b"data: [DONE]\n"]), api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(model)
    assert "sk-super-secret" not in str(model)


# 11. log scrub: no record at DEBUG contains the key, request body, or response body


def test_log_scrub(context, caplog) -> None:
    lines = [
        b'data: {"choices": [{"delta": {"content": "the reader'
        b'\'s book text should never be logged"}}]}\n',
        b"data: [DONE]\n",
    ]
    model = _model(lambda request, timeout: FakeResponse(lines), api_key="sk-do-not-log-me")
    with caplog.at_level(logging.DEBUG):
        list(model.stream(context))

    for record in caplog.records:
        message = record.getMessage()
        assert "sk-do-not-log-me" not in message
        assert "reader's book text" not in message


# Connect-error path (kept from the original suite: a transport-level failure,
# not an HTTP status, never retries and maps to CONNECT_ERROR).


def test_connect_error_maps_to_connect_error(context) -> None:
    def http(request, timeout):
        raise ValueError("cannot connect")

    model = _model(http)
    with pytest.raises(ChatStreamError) as exc:
        list(model.stream(context))
    assert exc.value.code == "CONNECT_ERROR"
