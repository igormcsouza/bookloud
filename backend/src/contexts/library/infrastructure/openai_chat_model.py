"""``ChatModel`` over OpenAI's Chat Completions API, ``stream=true``.

Shape borrowed from ``google_tts_synthesizer.py`` (PLANS/phase-4.md Q8): plain
``urllib`` REST with an injected HTTP callable, not the ``openai`` SDK --
one fewer runtime dependency in an image already shared by five Lambdas, and
it reuses the injected-transport test shape that keeps the whole suite off
the network.

Retries are ours: exactly one, on 429/5xx, and ONLY before the first token is
read from the response body. After the first byte the client has already
seen output on screen; a silent replay would duplicate it (PLANS/phase-7.md
§5.4).

The api_key lives in this instance and nowhere else: never in ``Settings``,
never in a Lambda environment variable, never logged. ``__repr__``/``__str__``
are overridden so neither can ever leak it. What IS logged is
``(model, status, x-request-id, duration_ms)`` -- never the request body
(the reader's book) and never the response body (PLANS/phase-7.md §5.5).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

from src.contexts.library.domain.chat import (
    ChatContext,
    ChatDelta,
    ChatUsage,
    FinishReason,
)
from src.contexts.library.infrastructure.openai_prompt import render_messages
from src.shared_kernel.domain.errors import ChatStreamError

logger = logging.getLogger("bookloud.chat.openai")

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
_MAX_ATTEMPTS = 2  # exactly one retry, and only before the first token
_RETRY_SLEEP_SECONDS = 1.0

# Fixed, never the upstream error body -- PLANS/phase-7.md §5.5 rule 5.
LLM_UNAVAILABLE_MESSAGE = "The answer stopped early. Try asking again."

_FINISH_REASON_MAP = {
    "stop": FinishReason.END_TURN,
    "length": FinishReason.MAX_TOKENS,
    "content_filter": FinishReason.REFUSAL,
}


def _default_http(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 -- fixed https endpoint


def _iter_frames(response: Any) -> Iterator[dict]:
    """OpenAI SSE: ``data: {json}`` lines, terminated by the literal
    ``data: [DONE]``. Comment (``:``) and blank lines are skipped. A frame
    that does not parse as JSON is skipped with a WARNING and does NOT abort
    the stream -- a single malformed keepalive should not lose an answer
    already half-rendered."""
    for raw_line in response:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            return
        try:
            yield json.loads(data_str)
        except json.JSONDecodeError:
            logger.warning("OpenAI stream: skipping a malformed frame")
            continue


class OpenAiChatModel:
    name: str
    enabled = True
    reason = None

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int,
        http: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self.name = model
        self._max_output_tokens = max_output_tokens
        # Defaults to a tiny urllib.request implementation and is injected in
        # tests -- zero network in the whole test suite (§10).
        self._http = http if http is not None else _default_http

    def __repr__(self) -> str:
        return f"OpenAiChatModel(model={self.name!r})"

    __str__ = __repr__

    def _request(self, context: ChatContext) -> urllib.request.Request:
        payload = {
            "model": self.name,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": self._max_output_tokens,
            "temperature": 0.2,
            "messages": render_messages(context),
        }
        return urllib.request.Request(
            OPENAI_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

    def stream(self, context: ChatContext) -> Iterator[ChatDelta]:
        response = self._open(context)
        try:
            with response:
                for frame in _iter_frames(response):
                    yield from self._frame_to_deltas(frame)
        except ChatStreamError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a dropped connection mid-stream
            raise ChatStreamError("Network failure mid-stream", "STREAM_ERROR") from exc

    def _open(self, context: ChatContext) -> Any:
        """Issues the request, retrying exactly once on a retryable HTTP
        status. Only reached before any token has left this generator, so a
        retry here can never duplicate on-screen text."""
        request = self._request(context)
        last_status: int | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            start = time.monotonic()
            try:
                response = self._http(request, 10.0)
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                request_id = exc.headers.get("x-request-id") if exc.headers else None
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.warning(
                    "OpenAI request failed (model=%s status=%s x-request-id=%s duration_ms=%d)",
                    self.name, last_status, request_id, duration_ms,
                )
                if last_status in _RETRYABLE_STATUSES and attempt < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                    continue
                raise ChatStreamError(LLM_UNAVAILABLE_MESSAGE, "LLM_UNAVAILABLE") from exc
            except Exception as exc:
                logger.warning(
                    "OpenAI connection failed (model=%s duration_ms=%d)",
                    self.name, int((time.monotonic() - start) * 1000),
                )
                raise ChatStreamError("Failed to connect to model", "CONNECT_ERROR") from exc
            else:
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.info(
                    "OpenAI request accepted (model=%s status=200 duration_ms=%d)",
                    self.name, duration_ms,
                )
                return response
        # Unreachable: the loop above always returns or raises.
        raise ChatStreamError(LLM_UNAVAILABLE_MESSAGE, "LLM_UNAVAILABLE")  # pragma: no cover

    def _frame_to_deltas(self, frame: dict) -> Iterator[ChatDelta]:
        choices = frame.get("choices") or []
        if choices:
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                yield ChatDelta(text=content)
            finish_reason_raw = choice.get("finish_reason")
            if finish_reason_raw is not None:
                mapped = _FINISH_REASON_MAP.get(finish_reason_raw, FinishReason.END_TURN)
                yield ChatDelta(text="", finish_reason=mapped)
        usage_raw = frame.get("usage")
        if usage_raw:
            cached = (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            logger.info(
                "OpenAI usage (model=%s input=%s output=%s cached=%s)",
                self.name,
                usage_raw.get("prompt_tokens"),
                usage_raw.get("completion_tokens"),
                cached,
            )
            yield ChatDelta(
                text="",
                usage=ChatUsage(
                    input_tokens=usage_raw.get("prompt_tokens", 0),
                    output_tokens=usage_raw.get("completion_tokens", 0),
                    cached_input_tokens=cached,
                ),
            )
