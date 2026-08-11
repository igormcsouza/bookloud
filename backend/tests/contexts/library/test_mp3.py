from __future__ import annotations

import pytest

from src.contexts.library.infrastructure.mp3 import (
    mp3_duration_ms,
    mp3_duration_seconds,
    strip_container_headers,
)
from tests.contexts.library.fakes import (
    STEREO,
    id3v2_header,
    mpeg2_frames,
    mpeg_frame,
    vbri_frame,
    xing_frame,
)

# --- Frame construction helpers (duplicate the ISO 11172-3/13818-3 formula
# independently from mp3.py's private tables, so these tests aren't merely
# checking the implementation against itself) --------------------------------

_MPEG1_LAYER3_BITRATES = [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
_MPEG2_LAYER3_BITRATES = [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None]

_MPEG1_SAMPLE_RATES = [44100, 48000, 32000]
_MPEG2_SAMPLE_RATES = [22050, 24000, 16000]


def _frame_bytes(
    *, version_bits: int, layer_bits: int, bitrate_idx: int, sample_rate_idx: int, padding: int
) -> bytes:
    """Build one syntactically valid MPEG audio frame (header + zero-filled
    payload of the exact computed length -- payload content is irrelevant to
    duration parsing)."""
    is_v1 = version_bits == 0b11
    bitrate_kbps = (_MPEG1_LAYER3_BITRATES if is_v1 else _MPEG2_LAYER3_BITRATES)[bitrate_idx]
    sample_rate = (_MPEG1_SAMPLE_RATES if is_v1 else _MPEG2_SAMPLE_RATES)[sample_rate_idx]
    samples_per_frame = 1152 if is_v1 else 576
    frame_len = (samples_per_frame // 8) * (bitrate_kbps * 1000) // sample_rate + padding

    b0 = 0xFF
    b1 = 0xE0 | (version_bits << 3) | (layer_bits << 1) | 0x1  # protection bit set (no CRC)
    b2 = (bitrate_idx << 4) | (sample_rate_idx << 2) | (padding << 1)
    b3 = 0x00
    header = bytes([b0, b1, b2, b3])
    return header + bytes(frame_len - 4)


def _frame_duration_ms(*, is_v1: bool, sample_rate: int) -> float:
    samples_per_frame = 1152 if is_v1 else 576
    return samples_per_frame / sample_rate * 1000


# --- MPEG-1 Layer III (44.1 kHz) -- e.g. a generic MP3 encoder's output ------


def test_mpeg1_layer3_44100_single_frame_duration() -> None:
    frame = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    expected = round(_frame_duration_ms(is_v1=True, sample_rate=44100))
    assert mp3_duration_ms(frame) == expected


def test_mpeg1_layer3_44100_multiple_frames_sum() -> None:
    frame = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    data = frame * 5
    expected = round(5 * _frame_duration_ms(is_v1=True, sample_rate=44100))
    assert mp3_duration_ms(data) == expected


# --- MPEG-2 Layer III (24 kHz) -- edge-tts's actual output format:
# audio-24khz-48kbitrate-mono-mp3. THE regression guard for the 576-vs-1152
# samples-per-frame distinction: using the MPEG-1 constant here would yield
# exactly 2x the true duration. ------------------------------------------


def test_mpeg2_layer3_24000_single_frame_duration() -> None:
    frame = _frame_bytes(version_bits=0b10, layer_bits=0b01, bitrate_idx=6, sample_rate_idx=1, padding=0)
    expected = round(_frame_duration_ms(is_v1=False, sample_rate=24000))
    assert mp3_duration_ms(frame) == expected


def test_mpeg2_layer3_24000_multiple_frames_sum_not_double_mpeg1_value() -> None:
    frame = _frame_bytes(version_bits=0b10, layer_bits=0b01, bitrate_idx=6, sample_rate_idx=1, padding=0)
    data = frame * 10
    expected_mpeg2 = round(10 * _frame_duration_ms(is_v1=False, sample_rate=24000))
    wrong_mpeg1_value = round(10 * (1152 / 24000 * 1000))
    assert mp3_duration_ms(data) == expected_mpeg2
    assert mp3_duration_ms(data) != wrong_mpeg1_value


# --- Padding bit ------------------------------------------------------------


def test_padding_bit_changes_frame_length_but_not_reported_duration() -> None:
    """Padding adds one byte to the frame's *size* (bitrate matching), not to
    its sample count -- duration per frame is constant regardless of the
    padding bit."""
    no_pad = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    padded = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=1)
    assert len(padded) == len(no_pad) + 1
    assert mp3_duration_ms(no_pad) == mp3_duration_ms(padded)


def test_padding_bit_frames_concatenated_sum_correctly() -> None:
    no_pad = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    padded = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=1)
    data = no_pad + padded
    expected = round(2 * _frame_duration_ms(is_v1=True, sample_rate=44100))
    assert mp3_duration_ms(data) == expected


# --- ID3v2 header skipped -----------------------------------------------


def test_id3v2_header_is_skipped() -> None:
    frame = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    # A minimal ID3v2 header: "ID3" + version(2) + flags(1) + syncsafe size(4).
    # Declare a 20-byte tag body (arbitrary junk) preceding the real frame.
    tag_body = b"\x00" * 20
    size = len(tag_body)
    syncsafe = bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )
    id3_header = b"ID3" + b"\x03\x00" + b"\x00" + syncsafe
    data = id3_header + tag_body + frame

    expected = round(_frame_duration_ms(is_v1=True, sample_rate=44100))
    assert mp3_duration_ms(data) == expected


# --- Garbage / truncation ----------------------------------------------


def test_garbage_bytes_return_zero() -> None:
    assert mp3_duration_ms(b"not an mp3 file at all, just random bytes") == 0


def test_empty_bytes_return_zero() -> None:
    assert mp3_duration_ms(b"") == 0


def test_truncated_final_frame_is_not_counted() -> None:
    full_frame = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    # A second frame's header is present but its payload is cut short.
    truncated = full_frame + full_frame[:6]
    expected = round(_frame_duration_ms(is_v1=True, sample_rate=44100))  # only the first, complete frame
    assert mp3_duration_ms(truncated) == expected


def test_invalid_bitrate_index_is_skipped_as_false_positive_sync() -> None:
    # bitrate_idx == 0b1111 ("bad") for MPEG1 Layer III must be rejected.
    b1 = 0xE0 | (0b11 << 3) | (0b01 << 1) | 0x1
    b2 = (0b1111 << 4) | (0 << 2) | 0
    data = bytes([0xFF, b1, b2, 0x00]) + b"\x00" * 10
    assert mp3_duration_ms(data) == 0


def test_free_bitrate_index_zero_is_skipped() -> None:
    b1 = 0xE0 | (0b11 << 3) | (0b01 << 1) | 0x1
    b2 = (0b0000 << 4) | (0 << 2) | 0
    data = bytes([0xFF, b1, b2, 0x00]) + b"\x00" * 10
    assert mp3_duration_ms(data) == 0


def test_reserved_sample_rate_index_is_skipped() -> None:
    b1 = 0xE0 | (0b11 << 3) | (0b01 << 1) | 0x1
    b2 = (9 << 4) | (0b11 << 2) | 0  # sample_rate_idx == 0b11 is reserved
    data = bytes([0xFF, b1, b2, 0x00]) + b"\x00" * 10
    assert mp3_duration_ms(data) == 0


def test_reserved_layer_bits_are_skipped_as_false_positive_sync() -> None:
    # layer_bits == 0b00 is reserved.
    data = bytes([0xFF, 0xE0 | (0b11 << 3) | (0b00 << 1), 0x00, 0x00]) + b"\x00" * 10
    assert mp3_duration_ms(data) == 0


def test_reserved_version_bits_are_skipped_as_false_positive_sync() -> None:
    # version_bits == 0b01 is reserved; the sync-looking bytes must be
    # skipped (advance one byte) rather than raising.
    data = bytes([0xFF, 0xE0 | (0b01 << 3) | (0b01 << 1), 0x00, 0x00]) + b"\x00" * 10
    assert mp3_duration_ms(data) == 0


# --- mp3_duration_seconds: the unrounded float (PLANS/phase-5.md §7.3) ------


def test_mp3_duration_seconds_returns_unrounded_float() -> None:
    """One MPEG-2 24 kHz frame is exactly 576/24000 == 0.024 s. The whole
    point of exposing seconds is that this value is NOT pre-rounded."""
    frame = mpeg_frame()
    assert mp3_duration_seconds(frame) == pytest.approx(576 / 24000, abs=1e-12)


def test_mp3_duration_seconds_is_not_a_whole_number_of_milliseconds() -> None:
    """44.1 kHz MPEG-1 frames are 1152/44100 s == 26.1224...ms -- the exact
    case where rounding per segment before summing accumulates drift."""
    frame = _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0)
    seconds = mp3_duration_seconds(frame)
    assert seconds * 1000 != round(seconds * 1000)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an mp3 file at all, just random bytes",
        _frame_bytes(version_bits=0b11, layer_bits=0b01, bitrate_idx=9, sample_rate_idx=0, padding=0),
        _frame_bytes(version_bits=0b10, layer_bits=0b01, bitrate_idx=6, sample_rate_idx=1, padding=0) * 10,
    ],
)
def test_mp3_duration_ms_is_exactly_the_rounded_seconds(data: bytes) -> None:
    """Regression guard on the refactor: mp3_duration_ms's behaviour is
    frozen -- every phase-4 caller keeps its exact previous values."""
    assert mp3_duration_ms(data) == round(mp3_duration_seconds(data) * 1000)


def test_mp3_duration_seconds_returns_zero_for_garbage() -> None:
    assert mp3_duration_seconds(b"garbage bytes here") == 0.0


# --- strip_container_headers (PLANS/phase-5.md §7.1) ------------------------


def test_strip_leaves_a_clean_stream_untouched() -> None:
    data = mpeg2_frames(3)
    stripped, sample_rate = strip_container_headers(data)
    assert stripped == data
    assert sample_rate == 24000


def test_strip_removes_an_id3v2_block() -> None:
    frames = mpeg2_frames(2)
    stripped, sample_rate = strip_container_headers(id3v2_header(20) + frames)
    assert stripped == frames
    assert sample_rate == 24000


@pytest.mark.parametrize("tag", [b"Xing", b"Info"])
def test_strip_removes_a_leading_xing_or_info_frame(tag: bytes) -> None:
    frames = mpeg2_frames(2)
    stripped, _ = strip_container_headers(xing_frame(tag=tag) + frames)
    assert stripped == frames


def test_strip_removes_a_leading_vbri_frame() -> None:
    frames = mpeg2_frames(2)
    stripped, _ = strip_container_headers(vbri_frame() + frames)
    assert stripped == frames


def test_strip_removes_id3_and_xing_and_vbri_together() -> None:
    frames = mpeg2_frames(4)
    data = id3v2_header(32) + xing_frame() + vbri_frame() + frames
    stripped, sample_rate = strip_container_headers(data)
    assert stripped == frames
    assert sample_rate == 24000


def test_strip_finds_the_xing_tag_at_the_mpeg1_stereo_offset() -> None:
    """The side-info block (and therefore the tag's offset) is 32 bytes for
    MPEG-1 stereo vs 9 for MPEG-2 mono -- getting the table wrong would leave
    a bogus metadata frame in the concatenated stream."""
    header = mpeg_frame(
        version_bits=0b11, bitrate_idx=9, sample_rate_idx=0, channel_mode=STEREO,
        payload=b"\x00" * 32 + b"Xing" + b"\x00" * 200,
    )
    frames = mpeg_frame(version_bits=0b11, bitrate_idx=9, sample_rate_idx=0, channel_mode=STEREO) * 2
    stripped, sample_rate = strip_container_headers(header + frames)
    assert stripped == frames
    assert sample_rate == 44100


def test_strip_returns_empty_and_none_for_garbage() -> None:
    assert strip_container_headers(b"nothing resembling an mp3 here") == (b"", None)


def test_strip_returns_empty_and_none_for_a_truncated_only_frame() -> None:
    assert strip_container_headers(mpeg_frame()[:20]) == (b"", None)


def test_strip_reports_the_first_real_frames_sample_rate() -> None:
    _, sample_rate = strip_container_headers(mpeg2_frames(1, sample_rate_idx=0))
    assert sample_rate == 22050


# --- concatenation round trip: the property the stitcher rests on ----------


def test_concatenating_stripped_segments_sums_durations_exactly() -> None:
    parts = [mpeg2_frames(n) for n in (3, 7, 11)]
    stripped = [strip_container_headers(id3v2_header(16) + xing_frame() + part)[0] for part in parts]
    concatenated = b"".join(stripped)

    assert concatenated == b"".join(parts)
    assert mp3_duration_seconds(concatenated) == pytest.approx(
        sum(mp3_duration_seconds(part) for part in stripped), abs=1e-6
    )
