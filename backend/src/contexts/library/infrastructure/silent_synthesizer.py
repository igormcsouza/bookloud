"""A **local, network-free** speech synthesizer that emits real, silent
MPEG-2 Layer III frames (PLANS/phase-5.md OQ-1).

Why this exists: `get_speech_synthesizer()` gates on `ENVIRONMENT != "prod"`
first and unconditionally (phase-4 §0), so *no* environment any automated
check can reach ever produces audio -- which means nothing in CI ever
exercises frame concatenation, byte offsets, the multipart upload or the book
manifest against a real S3 API. This closes that gap **locally** without
re-litigating that rule: phase-4 §0 is about *external services*, and this is
a pure-Python generator with zero third-party dependency, zero network calls
and no configuration beyond a chunk of text.

Opt-in via ``SYNTHESIS_STUB_MODE=silent``, which docker-compose sets only on
the ``synthesize-worker`` service. Every deployed environment (including
every ephemeral PR stack) leaves it at ``disabled`` and keeps the phase-4
``StubSynthesizer``'s deterministic ``EXTERNAL_TTS_DISABLED`` failure.

The frames match edge-tts's real output format --
``audio-24khz-48kbitrate-mono-mp3``, i.e. MPEG-2 Layer III, 24 kHz mono,
48 kbps CBR -- so the bytes the stitcher concatenates locally have exactly
the shape prod's will.
"""

from __future__ import annotations

from src.contexts.library.domain.marks import estimate_word_marks
from src.contexts.library.domain.synthesis import SynthesizedAudio
from src.contexts.library.domain.value_objects import MarksTiming, SynthesisSource
from src.contexts.library.infrastructure.mp3 import mp3_duration_ms

# Roughly a natural reading pace -- only used to size the generated audio so
# a book's chunks have plausibly different durations.
CHARS_PER_SECOND = 15.0

# MPEG-2 Layer III @ 24 kHz mono, 48 kbps, no padding:
#   version_bits = 0b10 (MPEG2), layer_bits = 0b01 (Layer III),
#   bitrate_idx  = 6    (48 kbps for MPEG2 Layer III),
#   sample_rate_idx = 1 (24000 Hz), channel_mode = 0b11 (mono).
# frame_len = (576 // 8) * 48000 // 24000 = 144 bytes; 576/24000 s = 24 ms.
_SAMPLES_PER_FRAME = 576
_SAMPLE_RATE_HZ = 24000
_FRAME_BYTES = 144
_SECONDS_PER_FRAME = _SAMPLES_PER_FRAME / _SAMPLE_RATE_HZ
_VOICE = "silent-24khz-mono"


def _silent_frame() -> bytes:
    b1 = 0xE0 | (0b10 << 3) | (0b01 << 1) | 0x1  # sync tail + version + layer + no CRC
    b2 = (6 << 4) | (1 << 2) | (0 << 1)  # bitrate idx, sample-rate idx, no padding
    b3 = 0b11 << 6  # mono
    # Zero-filled payload: not a decodable granule, but every real decoder
    # resynchronizes on the next header and renders silence, which is
    # exactly what a local dev loop wants.
    return bytes([0xFF, b1, b2, b3]) + bytes(_FRAME_BYTES - 4)


class SilentSynthesizer:
    name = "silent"

    def synthesize(self, text: str) -> SynthesizedAudio:
        seconds = max(len(text) / CHARS_PER_SECOND, _SECONDS_PER_FRAME)
        frame_count = max(1, round(seconds / _SECONDS_PER_FRAME))
        audio = _silent_frame() * frame_count
        # Measured off the bytes, not the estimate, so duration_ms is always
        # self-consistent with what the stitcher will re-measure.
        duration_ms = mp3_duration_ms(audio)
        marks = estimate_word_marks(
            text, duration_ms=duration_ms, anchors=[(0, 0), (len(text), duration_ms)]
        )
        return SynthesizedAudio(
            audio=audio,
            content_type="audio/mpeg",
            duration_ms=duration_ms,
            marks=tuple(marks),
            voice=_VOICE,
            source=SynthesisSource.SILENT,
            timing=MarksTiming.ESTIMATED,
        )
