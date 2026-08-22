from __future__ import annotations

import logging

import pytest

from src.contexts.library.domain.synthesis import SynthesisUnavailable, UnsynthesizableText
from src.contexts.library.domain.value_objects import MarksTiming, SynthesisFailure, SynthesisSource
from src.contexts.library.infrastructure.fallback_synthesizer import FallbackSynthesizer
from tests.contexts.library.fakes import FakeSynthesizer


def _audio(source: SynthesisSource) -> object:
    from src.contexts.library.domain.synthesis import SynthesizedAudio

    return SynthesizedAudio(
        audio=b"mp3-bytes",
        content_type="audio/mpeg",
        duration_ms=1000,
        marks=(),
        voice="voice",
        source=source,
        timing=MarksTiming.MEASURED,
    )


def test_primary_succeeds_fallback_never_called() -> None:
    primary = FakeSynthesizer(name="edge-tts", result=_audio(SynthesisSource.EDGE_TTS))
    fallback = FakeSynthesizer(name="google-tts", result=_audio(SynthesisSource.GOOGLE_TTS))
    combo = FallbackSynthesizer(primary, fallback)

    result = combo.synthesize("hello")

    assert result.source == SynthesisSource.EDGE_TTS
    assert primary.calls == ["hello"]
    assert fallback.calls == []


def test_primary_raises_fallback_result_returned_and_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    primary = FakeSynthesizer(name="edge-tts", error=SynthesisUnavailable("edge-tts down"))
    fallback = FakeSynthesizer(name="google-tts", result=_audio(SynthesisSource.GOOGLE_TTS))
    combo = FallbackSynthesizer(primary, fallback)

    with caplog.at_level(logging.WARNING, logger="bookloud.synthesis.fallback"):
        result = combo.synthesize("hello")

    assert result.source == SynthesisSource.GOOGLE_TTS
    assert primary.calls == ["hello"]
    assert fallback.calls == ["hello"]
    assert any("falling back" in record.message for record in caplog.records)
    # PLANS/phase-8 (deferred phase-4 OQ-E): the literal token a CloudWatch
    # Logs metric filter matches (pipeline_stack.py) -- a real regression
    # guard, not incidental phrasing.
    assert any("TTS_FALLBACK_TRIGGERED" in record.message for record in caplog.records)


def test_unsynthesizable_text_from_primary_reraised_without_touching_fallback() -> None:
    primary = FakeSynthesizer(
        name="edge-tts", error=UnsynthesizableText(SynthesisFailure.EMPTY_TEXT, "empty text")
    )
    fallback = FakeSynthesizer(name="google-tts", result=_audio(SynthesisSource.GOOGLE_TTS))
    combo = FallbackSynthesizer(primary, fallback)

    with pytest.raises(UnsynthesizableText):
        combo.synthesize("")

    assert fallback.calls == []


def test_both_fail_fallback_exception_propagates() -> None:
    primary = FakeSynthesizer(name="edge-tts", error=SynthesisUnavailable("primary down"))
    fallback = FakeSynthesizer(name="google-tts", error=SynthesisUnavailable("fallback also down"))
    combo = FallbackSynthesizer(primary, fallback)

    with pytest.raises(SynthesisUnavailable, match="fallback also down"):
        combo.synthesize("hello")


def test_name_is_fallback() -> None:
    primary = FakeSynthesizer()
    fallback = FakeSynthesizer()
    assert FallbackSynthesizer(primary, fallback).name == "fallback"
