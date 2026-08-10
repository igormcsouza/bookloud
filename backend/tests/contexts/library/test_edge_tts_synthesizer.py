from __future__ import annotations

import edge_tts.exceptions
import pytest

from src.contexts.library.domain.synthesis import SynthesisUnavailable
from src.contexts.library.infrastructure.edge_tts_synthesizer import (
    DEFAULT_VOICE,
    TICKS_PER_MS,
    EdgeTtsSynthesizer,
)
from tests.contexts.library.fakes import FakeCommunicateFactory
from tests.contexts.library.test_mp3 import _frame_bytes

_TEXT = "Chapter one begins here"
_MP3_FRAME = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)


def _word_boundary_chunks() -> list[dict]:
    return [
        {"type": "audio", "data": _MP3_FRAME},
        {"type": "WordBoundary", "text": "Chapter", "offset": 0, "duration": 3_000_000},
        {"type": "WordBoundary", "text": "one", "offset": 3_400_000, "duration": 1_000_000},
        {"type": "WordBoundary", "text": "begins", "offset": 4_500_000, "duration": 1_800_000},
        {"type": "WordBoundary", "text": "here", "offset": 6_400_000, "duration": 1_000_000},
    ]


# --- happy path ----------------------------------------------------------------


def test_synthesize_happy_path_returns_measured_audio() -> None:
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    result = synthesizer.synthesize(_TEXT)

    assert result.audio == _MP3_FRAME
    assert result.content_type == "audio/mpeg"
    assert result.duration_ms > 0
    assert result.source.value == "edge-tts"
    assert result.timing.value == "measured"
    assert result.voice == DEFAULT_VOICE
    assert len(result.marks) == 4


def test_boundary_word_boundary_is_passed_explicitly() -> None:
    """LOAD-BEARING: edge-tts defaults boundary to SentenceBoundary --
    omitting this kwarg silently breaks word-level highlighting."""
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    synthesizer.synthesize(_TEXT)

    assert len(factory.instances) == 1
    assert factory.instances[0].boundary == "WordBoundary"


def test_voice_and_text_are_passed_to_communicate() -> None:
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(voice="en-GB-SoniaNeural", communicate_cls=factory, attempts=1)

    synthesizer.synthesize(_TEXT)

    instance = factory.instances[0]
    assert instance.text == _TEXT
    assert instance.voice == "en-GB-SoniaNeural"


def test_tick_to_ms_conversion() -> None:
    factory = FakeCommunicateFactory(
        chunks=[
            {"type": "audio", "data": _MP3_FRAME},
            {"type": "WordBoundary", "text": "Chapter", "offset": 0, "duration": 3_000_000},
        ]
    )
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    result = synthesizer.synthesize("Chapter")

    assert result.marks[0].offset_ms == 0 // TICKS_PER_MS
    assert result.marks[0].duration_ms == 3_000_000 // TICKS_PER_MS
    assert result.marks[0].duration_ms == 300


def test_audio_chunks_concatenated_in_order() -> None:
    frame_a = _MP3_FRAME
    frame_b = _MP3_FRAME
    factory = FakeCommunicateFactory(
        chunks=[
            {"type": "audio", "data": frame_a},
            {"type": "audio", "data": frame_b},
            {"type": "WordBoundary", "text": "Chapter", "offset": 0, "duration": 3_000_000},
        ]
    )
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    result = synthesizer.synthesize("Chapter")

    assert result.audio == frame_a + frame_b


def test_char_start_end_populated_via_alignment() -> None:
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    result = synthesizer.synthesize(_TEXT)

    assert result.marks[0].char_start == 0
    assert result.marks[0].char_end == 7  # "Chapter"
    assert _TEXT[result.marks[1].char_start : result.marks[1].char_end] == "one"


# --- failure/retry branches -----------------------------------------------


def test_empty_audio_raises_synthesis_unavailable() -> None:
    factory = FakeCommunicateFactory(
        chunks=[{"type": "WordBoundary", "text": "Chapter", "offset": 0, "duration": 100000}]
    )
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize(_TEXT)


def test_retry_then_succeed() -> None:
    factory = FakeCommunicateFactory(errors=[RuntimeError("boom"), None], chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=2)

    result = synthesizer.synthesize(_TEXT)

    assert result.duration_ms > 0
    assert len(factory.instances) == 2


def test_retry_exhausted_raises_synthesis_unavailable_with_chained_cause() -> None:
    factory = FakeCommunicateFactory(error=RuntimeError("persistent failure"))
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=2)

    with pytest.raises(SynthesisUnavailable) as exc_info:
        synthesizer.synthesize(_TEXT)

    assert exc_info.value.__cause__ is not None
    assert len(factory.instances) == 2


def test_edge_tts_exception_subclass_is_caught_and_retried() -> None:
    factory = FakeCommunicateFactory(
        errors=[edge_tts.exceptions.NoAudioReceived("no audio"), None], chunks=_word_boundary_chunks()
    )
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=2)

    result = synthesizer.synthesize(_TEXT)

    assert result.duration_ms > 0


def test_bare_exception_is_caught_and_retried() -> None:
    factory = FakeCommunicateFactory(errors=[ValueError("weird"), None], chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=2)

    result = synthesizer.synthesize(_TEXT)

    assert result.duration_ms > 0


def test_unparseable_audio_bytes_raise_synthesis_unavailable() -> None:
    factory = FakeCommunicateFactory(
        chunks=[
            {"type": "audio", "data": b"not a valid mp3 frame at all"},
            {"type": "WordBoundary", "text": "Chapter", "offset": 0, "duration": 3_000_000},
        ]
    )
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)

    with pytest.raises(SynthesisUnavailable):
        synthesizer.synthesize(_TEXT)


def test_default_voice_used_when_not_specified() -> None:
    factory = FakeCommunicateFactory(chunks=_word_boundary_chunks())
    synthesizer = EdgeTtsSynthesizer(communicate_cls=factory, attempts=1)
    synthesizer.synthesize(_TEXT)
    assert factory.instances[0].voice == DEFAULT_VOICE


def test_name_is_edge_tts() -> None:
    assert EdgeTtsSynthesizer().name == "edge-tts"
