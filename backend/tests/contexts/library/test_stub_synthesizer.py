from __future__ import annotations

import pytest

from src.contexts.library.domain.synthesis import SynthesisDisabled
from src.contexts.library.domain.value_objects import SynthesisFailure
from src.contexts.library.infrastructure.stub_synthesizer import StubSynthesizer


def test_synthesize_always_raises_synthesis_disabled() -> None:
    synthesizer = StubSynthesizer()
    with pytest.raises(SynthesisDisabled):
        synthesizer.synthesize("some chunk text")


def test_synthesis_disabled_reason_is_external_tts_disabled() -> None:
    synthesizer = StubSynthesizer()
    with pytest.raises(SynthesisDisabled) as exc_info:
        synthesizer.synthesize("text")
    assert exc_info.value.reason == SynthesisFailure.EXTERNAL_TTS_DISABLED


def test_name_is_stub() -> None:
    assert StubSynthesizer().name == "stub"


def test_synthesize_never_touches_asyncio_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check that this is a pure raise -- no asyncio.run, no import
    of edge_tts/urllib triggered by calling synthesize()."""
    import asyncio

    def _boom(*args, **kwargs):
        raise AssertionError("StubSynthesizer must never call asyncio.run")

    monkeypatch.setattr(asyncio, "run", _boom)
    synthesizer = StubSynthesizer()
    with pytest.raises(SynthesisDisabled):
        synthesizer.synthesize("text")
