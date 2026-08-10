from __future__ import annotations

import base64
import io
import json
import urllib.error

import pytest

from src.contexts.library.domain.synthesis import SynthesisUnavailable
from src.contexts.library.infrastructure.google_tts_synthesizer import (
    DEFAULT_VOICE,
    GOOGLE_MAX_SSML_BYTES,
    GoogleTtsSynthesizer,
    _escape_ssml,
)
from tests.contexts.library.fakes import FakeHttpPost
from tests.contexts.library.test_mp3 import _frame_bytes

_MP3_FRAME = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
_MP3_B64 = base64.b64encode(_MP3_FRAME).decode("ascii")


def _http_error(status: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {"error": {"message": "boom"}}).encode("utf-8")
    return urllib.error.HTTPError(
        url="https://texttospeech.googleapis.com/v1beta1/text:synthesize",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(payload),
    )


def _response(*, timepoints: list[dict] | None = None) -> bytes:
    body: dict = {"audioContent": _MP3_B64}
    if timepoints is not None:
        body["timepoints"] = timepoints
    return json.dumps(body).encode("utf-8")


# --- request body shape --------------------------------------------------------


def test_request_body_shape_with_marks() -> None:
    text = "First sentence. Second sentence."
    http_post = FakeHttpPost([_response(timepoints=[{"markName": "s0", "timeSeconds": 0.0}, {"markName": "s1", "timeSeconds": 1.0}])])
    synthesizer = GoogleTtsSynthesizer(api_key="test-key", http_post=http_post)

    synthesizer.synthesize(text)

    url, body, timeout = http_post.calls[0]
    assert "key=test-key" in url
    payload = json.loads(body)
    assert "ssml" in payload["input"]
    assert payload["voice"]["name"] == DEFAULT_VOICE
    assert payload["voice"]["languageCode"] == "en-US"
    assert payload["audioConfig"]["audioEncoding"] == "MP3"
    assert payload["enableTimePointing"] == ["SSML_MARK"]


def test_one_mark_per_sentence_none_consecutive() -> None:
    text = "First sentence. Second sentence. Third one."
    http_post = FakeHttpPost([_response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)

    synthesizer.synthesize(text)

    _, body, _ = http_post.calls[0]
    ssml = json.loads(body)["input"]["ssml"]
    assert ssml.count("<mark") == 3
    assert "<mark" * 2 not in ssml.replace(" ", "")  # no two marks with nothing between


def test_language_code_derived_from_voice_name() -> None:
    http_post = FakeHttpPost([_response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", voice="fr-FR-Neural2-A", http_post=http_post)
    synthesizer.synthesize("Une phrase.")
    _, body, _ = http_post.calls[0]
    assert json.loads(body)["voice"]["languageCode"] == "fr-FR"


# --- SSML escaping --------------------------------------------------------------


def test_escape_ssml_ampersand_lt_gt_quote() -> None:
    assert _escape_ssml('A & B < C > D "quoted"') == "A &amp; B &lt; C &gt; D &quot;quoted&quot;"


def test_escape_ssml_no_special_chars_passthrough() -> None:
    assert _escape_ssml("plain text") == "plain text"


def test_unescaped_ampersand_in_source_text_is_escaped_in_ssml() -> None:
    text = "Smith & Sons opened. It was 1990."
    http_post = FakeHttpPost([_response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    synthesizer.synthesize(text)
    _, body, _ = http_post.calls[0]
    ssml = json.loads(body)["input"]["ssml"]
    assert "Smith &amp; Sons" in ssml
    assert "Smith & Sons" not in ssml


# --- over-budget SSML / degradation ---------------------------------------------


def test_over_budget_ssml_degrades_to_plain_text_with_two_anchors() -> None:
    # Many short "sentences" to blow the SSML markup budget while text stays
    # short -- actually simplest: one very long chunk forces size over cap.
    text = ("This is sentence number %d. " % 0) * 400  # long enough with repeats to exceed the cap
    http_post = FakeHttpPost([_response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)

    result = synthesizer.synthesize(text)

    _, body, _ = http_post.calls[0]
    payload = json.loads(body)
    assert "ssml" not in payload["input"]
    assert payload["input"]["text"] == text
    assert "enableTimePointing" not in payload
    assert result.timing.value == "estimated"


def test_empty_timepoints_degrades_to_proportional() -> None:
    text = "First sentence. Second sentence."
    http_post = FakeHttpPost([_response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)

    result = synthesizer.synthesize(text)

    assert result.timing.value == "estimated"
    assert len(result.marks) > 0


def test_non_monotonic_timepoints_discarded() -> None:
    text = "First sentence. Second sentence. Third sentence."
    # Third entry's time regresses -- must be discarded, not corrupt the
    # interpolation.
    http_post = FakeHttpPost(
        [
            _response(
                timepoints=[
                    {"markName": "s0", "timeSeconds": 0.0},
                    {"markName": "s1", "timeSeconds": 1.0},
                    {"markName": "s2", "timeSeconds": 0.5},
                ]
            )
        ]
    )
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    result = synthesizer.synthesize(text)
    assert result.timing.value == "estimated"


def test_unparseable_timepoint_entry_discarded() -> None:
    text = "First sentence. Second sentence."
    http_post = FakeHttpPost(
        [_response(timepoints=[{"markName": "s0", "timeSeconds": "not-a-number"}, {"markName": "s1", "timeSeconds": 1.0}])]
    )
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    result = synthesizer.synthesize(text)
    assert len(result.marks) > 0


def test_timepoint_with_unknown_mark_name_ignored() -> None:
    text = "First sentence. Second sentence."
    http_post = FakeHttpPost(
        [_response(timepoints=[{"markName": "unknown-mark", "timeSeconds": 0.5}])]
    )
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    result = synthesizer.synthesize(text)
    assert result.timing.value == "estimated"


def test_generic_exception_during_http_post_is_retried() -> None:
    http_post = FakeHttpPost([TimeoutError("connection timed out"), _response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=2)
    result = synthesizer.synthesize("One sentence.")
    assert result.duration_ms > 0


def test_403_error_body_not_json_still_raises_synthesis_unavailable() -> None:
    error = urllib.error.HTTPError(
        url="https://texttospeech.googleapis.com/v1beta1/text:synthesize",
        code=403,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"not json at all"),
    )
    http_post = FakeHttpPost([error])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize("One sentence.")


def test_truncated_timepoints_single_entry_degrades_to_proportional() -> None:
    text = "First sentence. Second sentence. Third sentence."
    http_post = FakeHttpPost([_response(timepoints=[{"markName": "s0", "timeSeconds": 0.0}])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)

    result = synthesizer.synthesize(text)

    assert result.timing.value == "estimated"
    assert len(result.marks) > 0


# --- base64 audio decoding -------------------------------------------------------


def test_base64_audio_decoded_to_raw_mp3_bytes() -> None:
    http_post = FakeHttpPost([_response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    result = synthesizer.synthesize("One sentence.")
    assert result.audio == _MP3_FRAME
    assert result.duration_ms > 0


# --- error handling ---------------------------------------------------------


def test_429_retried_then_succeeds() -> None:
    http_post = FakeHttpPost([_http_error(429), _response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=2)

    result = synthesizer.synthesize("One sentence.")

    assert result.duration_ms > 0
    assert len(http_post.calls) == 2


def test_429_retry_exhausted_raises_synthesis_unavailable() -> None:
    http_post = FakeHttpPost([_http_error(429), _http_error(429)])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=2)

    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize("One sentence.")


def test_5xx_retried() -> None:
    http_post = FakeHttpPost([_http_error(503), _response(timepoints=[])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=2)
    result = synthesizer.synthesize("One sentence.")
    assert result.duration_ms > 0


def test_403_not_retried() -> None:
    http_post = FakeHttpPost([_http_error(403, {"error": {"message": "API not enabled"}})])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=3)

    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize("One sentence.")

    assert len(http_post.calls) == 1  # no retry


def test_400_not_retried() -> None:
    http_post = FakeHttpPost([_http_error(400, {"error": {"message": "bad request"}})])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=3)

    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize("One sentence.")

    assert len(http_post.calls) == 1


def test_missing_audio_content_retried_then_raises() -> None:
    http_post = FakeHttpPost([json.dumps({}).encode("utf-8"), json.dumps({}).encode("utf-8")])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post, attempts=2)
    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize("One sentence.")


# --- timing marker + name -----------------------------------------------------


def test_timing_is_estimated() -> None:
    http_post = FakeHttpPost([_response(timepoints=[{"markName": "s0", "timeSeconds": 0.0}, {"markName": "s1", "timeSeconds": 0.2}])])
    synthesizer = GoogleTtsSynthesizer(api_key="key", http_post=http_post)
    result = synthesizer.synthesize("First. Second.")
    assert result.timing.value == "estimated"
    assert result.source.value == "google-tts"


def test_name_is_google_tts() -> None:
    assert GoogleTtsSynthesizer(api_key="k").name == "google-tts"


def test_default_voice() -> None:
    assert DEFAULT_VOICE == "en-US-Neural2-C"


def test_google_max_ssml_bytes_is_below_the_5000_byte_cap() -> None:
    assert GOOGLE_MAX_SSML_BYTES < 5000
