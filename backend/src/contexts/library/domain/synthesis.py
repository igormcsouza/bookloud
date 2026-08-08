"""Ports and value objects for TTS synthesis (PLANS/phase-4.md §7.2, §0).

No ``edge_tts``, no ``boto3``, no ``urllib`` import here -- ``SpeechSynthesizer``/
``SynthesisQueue`` are ``Protocol``s (jgautocar's convention, matching
``domain/extraction.py``'s ``PdfTextExtractor``); ``infrastructure/
edge_tts_synthesizer.py``, ``infrastructure/google_tts_synthesizer.py``,
``infrastructure/fallback_synthesizer.py``, ``infrastructure/
stub_synthesizer.py`` and ``infrastructure/sqs_synthesis_queue.py`` are the
only adapters.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from src.contexts.library.domain.value_objects import (
    MarksTiming,
    SynthesisFailure,
    SynthesisSource,
)
from src.shared_kernel.domain.errors import DomainError

# Comfortably above chunking.DEFAULT_MAX_CHARS (2600) -- this is a defensive
# pre-flight guard in SynthesizeChunk, not a limit any current chunker output
# should ever actually hit (PLANS/phase-4.md §8.2 step 4).
MAX_SYNTHESIS_CHARS = 4000


@dataclass(frozen=True)
class WordMark:
    text: str
    offset_ms: int  # from the start of THIS chunk's audio
    duration_ms: int
    char_start: int  # into the chunk's text; filled by align/estimate, not the engine
    char_end: int


@dataclass(frozen=True)
class SynthesizedAudio:
    audio: bytes
    content_type: str  # "audio/mpeg"
    duration_ms: int
    marks: tuple[WordMark, ...]
    voice: str
    source: SynthesisSource
    timing: MarksTiming


class SpeechSynthesizer(Protocol):
    @property
    def name(self) -> str: ...  # pragma: no cover

    def synthesize(self, text: str) -> SynthesizedAudio: ...  # pragma: no cover


class SynthesisQueue(Protocol):
    """The fan-out producer port -- ``ExtractBook`` depends on this, not on
    boto3/SQS (PLANS/phase-4.md §4.4). Returns the number of messages
    actually published."""

    def enqueue_chunks(
        self, *, user_id: str, book_id: str, chunk_indexes: Iterable[int]
    ) -> int: ...  # pragma: no cover


class SynthesisError(DomainError):
    """Base class for synthesis failures -- same posture as
    ``domain/extraction.py``'s ``ExtractionError``: a 422, not a 5xx,
    because the *caller* (the synthesize Lambda) decides whether the
    specific subclass is permanent or transient."""

    status_code = 422


class UnsynthesizableText(SynthesisError):
    """PERMANENT -- a property of the chunk's text (empty or oversized),
    not of the engine. Carries ``.reason`` (a ``SynthesisFailure`` value) so
    ``SynthesizeChunk`` doesn't have to re-derive it from the message."""

    def __init__(self, reason: SynthesisFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SynthesisUnavailable(SynthesisError):
    """TRANSIENT -- an engine or network failure. ``SynthesizeChunk``
    releases the claim to ``PENDING`` and lets SQS retry, unless this is the
    last attempt (PLANS/phase-4.md §8.3), in which case it becomes
    permanent (``ALL_ENGINES_FAILED``)."""


class SynthesisDisabled(SynthesisError):
    """Raised by ``StubSynthesizer``. Always PERMANENT -- retrying a config
    state five times across two hours of SQS backoff proves nothing and
    only slows down local/PR development (PLANS/phase-4.md §0)."""

    reason = SynthesisFailure.EXTERNAL_TTS_DISABLED

    def __init__(self, message: str = "Real TTS engines are disabled outside prod") -> None:
        super().__init__(message)
