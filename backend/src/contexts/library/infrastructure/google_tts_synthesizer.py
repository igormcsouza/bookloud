"""``SpeechSynthesizer`` fallback adapter over Google Cloud TTS's REST
v1beta1 endpoint (PLANS/phase-4.md §7.4). **The only module in this
codebase that talks to Google** -- plain ``urllib`` REST, not
``google-cloud-texttospeech`` (no gRPC, no 30 MB of deps for one HTTP POST).

Google Cloud TTS does not emit per-word boundary events (edge-tts's real
timing signal). The only timing mechanism is ``enableTimePointing:
["SSML_MARK"]``, which reports the audio time of each ``<mark>`` placed in
the SSML. Two hard constraints kill a naive per-word mark: the input is
capped at 5000 bytes *including* markup, and Google's own guidance warns
against consecutive marks ("marks in rapid succession might not generate
events"). So: one ``<mark>`` per *sentence* (reusing ``chunking.split_
sentences`` -- already in the domain, already tested, and the same splitter
the chunker itself uses), and ``domain/marks.py``'s ``estimate_word_marks``
interpolates each word's timing proportionally within its sentence. Exact
at every sentence boundary, linearly interpolated in between --
``MarksTiming.ESTIMATED`` records this honestly in the marks JSON.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from src.contexts.library.domain.chunking import split_sentences
from src.contexts.library.domain.marks import estimate_word_marks
from src.contexts.library.domain.synthesis import SynthesizedAudio, SynthesisUnavailable
from src.contexts.library.domain.value_objects import MarksTiming, SynthesisSource
from src.contexts.library.infrastructure.mp3 import mp3_duration_ms

logger = logging.getLogger("bookloud.synthesis.google_tts")

DEFAULT_VOICE = "en-US-Neural2-C"
_ENDPOINT = "https://texttospeech.googleapis.com/v1beta1/text:synthesize"
# The SSML input cap is 5000 bytes including markup (Google's published
# limit) -- 4800 leaves headroom rather than sitting exactly on the edge.
GOOGLE_MAX_SSML_BYTES = 4800
_RETRY_SLEEP_SECONDS = 2.0
# HTTP statuses that mean "this call will never succeed" (bad key, API not
# enabled, quota exhausted) -- not retried, and not a reason to permanently
# fail the *chunk* (fix the key, redrive, done).
_PERMANENT_HTTP_STATUSES = (400, 403)


def _escape_ssml(text: str) -> str:
    """``&``/``<``/``>``/``"`` -- an unescaped ampersand in a book's text
    would otherwise produce a 400 from Google on exactly the chunks that
    need the fallback most."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _language_code_from_voice(voice: str) -> str:
    parts = voice.split("-")
    return f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else voice


def _default_http_post(url: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, method="POST", data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- fixed https endpoint
        return resp.read()


def _build_ssml(text: str) -> tuple[str | None, list[tuple[str, int]]]:
    """Returns ``(ssml_or_None, [(mark_name, char_offset), ...])``.
    ``char_offset`` is each sentence's char_start in ``text``'s own
    coordinate space. ``None`` for the ssml means "degrade to plain text,
    no marks" -- either no sentences were found, or the assembled SSML blew
    the byte cap."""
    sentences = split_sentences(text)
    if not sentences:
        return None, []

    parts = ["<speak>"]
    mark_offsets: list[tuple[str, int]] = []
    cursor = 0
    for i, (start, end) in enumerate(sentences):
        mark_name = f"s{i}"
        parts.append(_escape_ssml(text[cursor:start]))  # verbatim gap (whitespace etc.)
        parts.append(f'<mark name="{mark_name}"/>')
        parts.append(_escape_ssml(text[start:end]))
        mark_offsets.append((mark_name, start))
        cursor = end
    parts.append(_escape_ssml(text[cursor:]))
    parts.append("</speak>")
    ssml = "".join(parts)

    if len(ssml.encode("utf-8")) > GOOGLE_MAX_SSML_BYTES:
        # Degrading the timing beats failing the chunk.
        return None, []
    return ssml, mark_offsets


def _anchors_from_timepoints(
    text: str, mark_offsets: list[tuple[str, int]], timepoints: list[dict], duration_ms: int
) -> list[tuple[int, int]]:
    """Sorted, monotonic ``(char_offset, time_ms)`` anchors, always
    including ``(0, 0)`` and ``(len(text), duration_ms)``. Marks whose
    ``markName`` doesn't parse, or whose times aren't monotonic, are
    discarded. Falls back to the whole-chunk 2-anchor degenerate case when
    fewer than 2 usable timepoints survive (empty/truncated response --
    there's a reported v1beta1 bug where timepoints truncate after the
    first sentence)."""
    offsets_by_name = dict(mark_offsets)
    candidates: list[tuple[int, int]] = []
    for tp in timepoints:
        name = tp.get("markName")
        if name not in offsets_by_name:
            continue
        try:
            time_ms = round(float(tp["timeSeconds"]) * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        candidates.append((offsets_by_name[name], time_ms))

    candidates.sort(key=lambda pair: pair[0])
    filtered: list[tuple[int, int]] = []
    for char_off, time_ms in candidates:
        if filtered and (char_off <= filtered[-1][0] or time_ms < filtered[-1][1]):
            continue  # non-monotonic -- discard rather than corrupt the interpolation
        filtered.append((char_off, time_ms))

    if len(filtered) < 2:
        return [(0, 0), (len(text), duration_ms)]

    anchors = list(filtered)
    if anchors[0][0] != 0:
        anchors.insert(0, (0, 0))
    if anchors[-1][0] != len(text):
        anchors.append((len(text), duration_ms))
    return anchors


class GoogleTtsSynthesizer:
    name = SynthesisSource.GOOGLE_TTS.value

    def __init__(
        self,
        *,
        api_key: str,
        voice: str = DEFAULT_VOICE,
        attempts: int = 2,
        timeout_seconds: float = 45.0,
        http_post: Callable[[str, bytes, float], bytes] | None = None,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._attempts = attempts
        self._timeout_seconds = timeout_seconds
        # http_post defaults to a tiny urllib.request implementation and is
        # injected in tests -- zero network in the whole test suite (§10).
        self._http_post = http_post if http_post is not None else _default_http_post

    def synthesize(self, text: str) -> SynthesizedAudio:
        ssml, mark_offsets = _build_ssml(text)
        use_marks = ssml is not None

        body: dict = {
            "input": {"ssml": ssml} if use_marks else {"text": text},
            "voice": {"languageCode": _language_code_from_voice(self._voice), "name": self._voice},
            "audioConfig": {"audioEncoding": "MP3", "sampleRateHertz": 24000},
        }
        if use_marks:
            body["enableTimePointing"] = ["SSML_MARK"]
        data = json.dumps(body).encode("utf-8")
        url = f"{_ENDPOINT}?key={self._api_key}"

        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                raw = self._http_post(url, data, self._timeout_seconds)
            except urllib.error.HTTPError as exc:
                status = exc.code
                detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
                if status in _PERMANENT_HTTP_STATUSES:
                    logger.error("Google TTS request failed permanently (status=%s): %s", status, _error_message(detail))
                    raise SynthesisUnavailable(f"Google TTS returned {status}") from exc
                last_error = exc
                logger.warning("Google TTS attempt %d/%d failed (status=%s): %s", attempt, self._attempts, status, detail)
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue
            except Exception as exc:  # noqa: BLE001 -- network/timeout errors, same posture as edge-tts
                last_error = exc
                logger.warning("Google TTS attempt %d/%d raised: %s", attempt, self._attempts, exc)
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue

            response = json.loads(raw)
            audio_b64 = response.get("audioContent")
            if not audio_b64:
                last_error = SynthesisUnavailable("Google TTS response missing audioContent")
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue

            audio_bytes = base64.b64decode(audio_b64)
            duration_ms = mp3_duration_ms(audio_bytes)
            if duration_ms <= 0:
                last_error = SynthesisUnavailable("Google TTS audio bytes did not parse as a valid MP3 stream")
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue

            timepoints = response.get("timepoints", []) if use_marks else []
            anchors = _anchors_from_timepoints(text, mark_offsets, timepoints, duration_ms)
            marks = estimate_word_marks(text, duration_ms=duration_ms, anchors=anchors)

            return SynthesizedAudio(
                audio=audio_bytes,
                content_type="audio/mpeg",
                duration_ms=duration_ms,
                marks=tuple(marks),
                voice=self._voice,
                source=SynthesisSource.GOOGLE_TTS,
                timing=MarksTiming.ESTIMATED,
            )

        raise SynthesisUnavailable(f"Google TTS failed after {self._attempts} attempt(s)") from last_error


def _error_message(body_text: str) -> str:
    try:
        return json.loads(body_text).get("error", {}).get("message", body_text)
    except (json.JSONDecodeError, AttributeError):
        return body_text
