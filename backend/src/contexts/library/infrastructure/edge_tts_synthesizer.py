"""``SpeechSynthesizer`` adapter over Microsoft's free Edge Read Aloud
endpoint (PLANS/phase-4.md §7.3). **The only module in this codebase that
imports ``edge_tts``.**

Deliberately **sync**, not async: the rest of the codebase (FastAPI route
handlers, repositories, phase 3's handler) is synchronous, and a single
``asyncio.run(...)`` here keeps the async surface confined to this one file,
exactly where it belongs.

Reliability notes recorded so they aren't rediscovered in an incident: the
endpoint requires a clock-derived DRM token (Lambda clocks are NTP-accurate,
and edge-tts self-corrects skew on ``403``); Microsoft has been observed
rate-limiting and occasionally blocking datacenter IP ranges; there is no
SLA, no support, and no announcement channel. This is precisely why
``IMPLEMENTATION_PLAN.md`` specifies a fallback (``fallback_synthesizer.py``),
and why every failure here is treated as **transient** (``SynthesisUnavailable``)
rather than a permanently broken chunk -- the caller decides permanence only
after both engines have failed on the last SQS attempt (§8.3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import edge_tts

from src.contexts.library.domain.marks import align_words
from src.contexts.library.domain.synthesis import SynthesizedAudio, SynthesisUnavailable, WordMark
from src.contexts.library.domain.value_objects import MarksTiming, SynthesisSource
from src.contexts.library.infrastructure.mp3 import mp3_duration_ms

logger = logging.getLogger("bookloud.synthesis.edge_tts")

DEFAULT_VOICE = "en-US-AriaNeural"
# edge-tts reports offset/duration in 100-nanosecond "ticks"
# (TICKS_PER_SECOND = 10_000_000 in edge_tts.constants).
TICKS_PER_MS = 10_000
_RETRY_SLEEP_SECONDS = 1.5


class EdgeTtsSynthesizer:
    name = SynthesisSource.EDGE_TTS.value

    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        attempts: int = 2,
        timeout_seconds: float = 75.0,
        communicate_cls: Any | None = None,
    ) -> None:
        self._voice = voice
        self._attempts = attempts
        self._timeout_seconds = timeout_seconds
        # communicate_cls exists SOLELY so tests can inject a fake with no
        # network and no monkeypatching of edge_tts's own globals (§10).
        self._communicate_cls = communicate_cls if communicate_cls is not None else edge_tts.Communicate

    def synthesize(self, text: str) -> SynthesizedAudio:
        last_error: Exception | None = None

        for attempt in range(1, self._attempts + 1):
            try:
                audio_bytes, words = asyncio.run(asyncio.wait_for(self._stream(text), self._timeout_seconds))
            except Exception as exc:  # noqa: BLE001 -- see module docstring: broad catching is deliberate
                last_error = exc
                logger.warning("edge-tts attempt %d/%d raised: %s", attempt, self._attempts, exc)
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue

            if not audio_bytes:
                last_error = SynthesisUnavailable("edge-tts returned no audio bytes (NoAudioReceived-shaped)")
                logger.warning("edge-tts attempt %d/%d returned empty audio", attempt, self._attempts)
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue

            duration_ms = mp3_duration_ms(audio_bytes)
            if duration_ms <= 0:
                last_error = SynthesisUnavailable("edge-tts audio bytes did not parse as a valid MP3 stream")
                logger.warning("edge-tts attempt %d/%d produced unparseable audio", attempt, self._attempts)
                if attempt < self._attempts:
                    time.sleep(_RETRY_SLEEP_SECONDS)
                continue

            spans = align_words(text, [word_text for word_text, _, _ in words])
            marks = tuple(
                WordMark(text=word_text, offset_ms=offset_ms, duration_ms=word_duration_ms, char_start=start, char_end=end)
                for (word_text, offset_ms, word_duration_ms), (start, end) in zip(words, spans)
            )
            return SynthesizedAudio(
                audio=audio_bytes,
                content_type="audio/mpeg",
                duration_ms=duration_ms,
                marks=marks,
                voice=self._voice,
                source=SynthesisSource.EDGE_TTS,
                timing=MarksTiming.MEASURED,
            )

        raise SynthesisUnavailable(f"edge-tts failed after {self._attempts} attempt(s)") from last_error

    async def _stream(self, text: str) -> tuple[bytes, list[tuple[str, int, int]]]:
        communicate = self._communicate_cls(
            text,
            self._voice,
            # LOAD-BEARING: edge-tts >= 7.x defaults `boundary` to
            # "SentenceBoundary". Word-level highlighting needs WordBoundary
            # events, so this must be passed explicitly -- omitting it
            # silently produces one mark per sentence and a highlight that
            # lurches instead of tracking each word.
            boundary="WordBoundary",
            connect_timeout=10,
            receive_timeout=30,
        )
        audio = bytearray()
        words: list[tuple[str, int, int]] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append(
                    (
                        chunk["text"],
                        int(chunk["offset"]) // TICKS_PER_MS,
                        int(chunk["duration"]) // TICKS_PER_MS,
                    )
                )
        return bytes(audio), words
