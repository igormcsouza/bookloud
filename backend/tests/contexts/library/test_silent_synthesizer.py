"""``SilentSynthesizer`` (PLANS/phase-5.md OQ-1) -- the local, network-free
generator that lets ``make up`` + ``make smoke`` exercise real frame
concatenation, byte offsets, the multipart upload and the book manifest
against LocalStack's real S3.
"""

from __future__ import annotations

import pytest

from src.contexts.library.domain.value_objects import MarksTiming, SynthesisSource
from src.contexts.library.infrastructure.mp3 import (
    mp3_duration_ms,
    strip_container_headers,
)
from src.contexts.library.infrastructure.silent_synthesizer import SilentSynthesizer


def test_produces_a_decodable_mpeg2_24khz_mono_stream() -> None:
    audio = SilentSynthesizer().synthesize("Some chunk text to read aloud.")

    frames, sample_rate = strip_container_headers(audio.audio)
    assert frames == audio.audio  # no ID3, no Xing -- already a clean stream
    assert sample_rate == 24000
    assert audio.content_type == "audio/mpeg"


def test_duration_is_measured_off_the_bytes_not_estimated() -> None:
    audio = SilentSynthesizer().synthesize("x" * 300)
    assert audio.duration_ms == mp3_duration_ms(audio.audio)
    assert audio.duration_ms > 0


def test_longer_text_produces_longer_audio() -> None:
    synthesizer = SilentSynthesizer()
    short = synthesizer.synthesize("short")
    long = synthesizer.synthesize("a much longer chunk of text " * 20)
    assert long.duration_ms > short.duration_ms


def test_frame_count_is_a_whole_number_of_24ms_frames() -> None:
    audio = SilentSynthesizer().synthesize("x" * 150)
    assert len(audio.audio) % 144 == 0
    assert audio.duration_ms == (len(audio.audio) // 144) * 24


def test_marks_cover_every_word_and_stay_within_the_duration() -> None:
    text = "One two three four five."
    audio = SilentSynthesizer().synthesize(text)

    assert [m.text for m in audio.marks] == text.split()
    assert all(0 <= m.offset_ms <= audio.duration_ms for m in audio.marks)
    assert [m.offset_ms for m in audio.marks] == sorted(m.offset_ms for m in audio.marks)


def test_reports_the_silent_source_and_estimated_timing() -> None:
    audio = SilentSynthesizer().synthesize("hello")
    assert audio.source is SynthesisSource.SILENT
    assert audio.timing is MarksTiming.ESTIMATED
    assert SilentSynthesizer.name == "silent"


@pytest.mark.parametrize("text", ["", " ", "a"])
def test_degenerate_text_still_produces_at_least_one_valid_frame(text: str) -> None:
    audio = SilentSynthesizer().synthesize(text)
    assert len(audio.audio) >= 144
    assert audio.duration_ms >= 24


def test_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that keeps this from re-litigating phase-4 §0: it is a
    local generator, not an external service."""
    import socket

    def _boom(*args, **kwargs):  # pragma: no cover -- only runs on a regression
        raise AssertionError("SilentSynthesizer must never open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    assert SilentSynthesizer().synthesize("some text").duration_ms > 0
