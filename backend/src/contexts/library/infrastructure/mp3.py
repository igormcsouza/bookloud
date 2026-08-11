"""Pure MP3 frame-header duration parser (PLANS/phase-4.md §7.6). No deps,
no I/O -- sums per-frame durations straight out of MPEG audio frame headers.

Why this exists rather than a ``mutagen``/``pydub`` dependency: it's ~100
lines of table lookups, it's needed on the Google path *before* any timing
can be computed at all (``google_tts_synthesizer.py``'s proportional
estimation needs a duration to interpolate against), and phase 5 needs exact
durations for its stitching offset math. Adding an audio library (and, for
pydub, an ffmpeg binary) to the Lambda image for a byte-counting exercise
isn't justified.

**The MPEG-2-vs-MPEG-1 ``samples_per_frame`` distinction is load-bearing.**
Layer III carries 1152 samples/frame for MPEG-1 but only 576 for MPEG-2/2.5.
edge-tts's default output format is ``audio-24khz-48kbitrate-mono-mp3`` --
24 kHz is an MPEG-2 sample rate, so its frames are MPEG-2 Layer III. Using
the MPEG-1 constant unconditionally yields exactly 2x the true duration on
the primary synthesis path. Both branches get a dedicated test
(``test_mp3.py``).
"""

from __future__ import annotations

# --- ISO/IEC 11172-3 / 13818-3 tables ---------------------------------------

# Bitrate index -> kbps, keyed by (version_group, layer). index 0 ("free") and
# 15 ("bad") are both represented as None -- neither is supported here (a
# real TTS-engine MP3 never uses a free bitrate).
_BITRATES_KBPS: dict[tuple[str, int], list[int | None]] = {
    ("V1", 1): [None, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, None],
    ("V1", 2): [None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, None],
    ("V1", 3): [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None],
    ("V2", 1): [None, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, None],
    ("V2", 2): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    ("V2", 3): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
}

# Sample-rate index -> Hz, keyed by MPEG version. Index 3 ("reserved") is None.
_SAMPLE_RATES_HZ: dict[str, list[int | None]] = {
    "MPEG1": [44100, 48000, 32000, None],
    "MPEG2": [22050, 24000, 16000, None],
    "MPEG2.5": [11025, 12000, 8000, None],
}

# version_bits (2 bits of byte 2) -> MPEG version name.
_VERSION_BY_BITS = {0b00: "MPEG2.5", 0b10: "MPEG2", 0b11: "MPEG1"}
# layer_bits (2 bits of byte 2) -> layer number (I/II/III).
_LAYER_BY_BITS = {0b01: 3, 0b10: 2, 0b11: 1}


def _samples_per_frame(version: str, layer: int) -> int:
    if layer == 1:
        return 384
    if layer == 2:
        return 1152
    # Layer III: THE load-bearing distinction (see module docstring).
    return 1152 if version == "MPEG1" else 576


def _parse_frame_header(b0: int, b1: int, b2: int) -> tuple[int, int, int, str] | None:
    """Parse the 3 bytes following the ``0xFF`` sync byte (``b0`` is that
    sync byte itself, checked by the caller). Returns
    ``(frame_length_bytes, samples_per_frame, sample_rate_hz, version)`` or
    ``None`` if this isn't a valid frame header (a false-positive sync match
    in arbitrary bytes, or a reserved/free/bad field).

    ``version`` is returned (phase 5) because ``strip_container_headers``
    needs it to locate the side-information block a Xing/Info tag hides
    behind -- its offset differs between MPEG-1 and MPEG-2/2.5."""
    version_bits = (b1 >> 3) & 0x3
    layer_bits = (b1 >> 1) & 0x3
    if version_bits not in _VERSION_BY_BITS or layer_bits not in _LAYER_BY_BITS:
        return None
    version = _VERSION_BY_BITS[version_bits]
    layer = _LAYER_BY_BITS[layer_bits]

    bitrate_idx = (b2 >> 4) & 0xF
    sample_rate_idx = (b2 >> 2) & 0x3
    padding = (b2 >> 1) & 0x1

    version_group = "V1" if version == "MPEG1" else "V2"
    bitrate_kbps = _BITRATES_KBPS[(version_group, layer)][bitrate_idx]
    if bitrate_kbps is None:
        return None
    sample_rate = _SAMPLE_RATES_HZ[version][sample_rate_idx]
    if sample_rate is None:
        return None

    bitrate_bps = bitrate_kbps * 1000
    samples_per_frame = _samples_per_frame(version, layer)
    if layer == 1:
        frame_len = (12 * bitrate_bps // sample_rate + padding) * 4
    else:
        # Layer II: 144 = 1152 // 8. Layer III: 144 (MPEG1) or 72 (MPEG2/2.5).
        frame_len = (samples_per_frame // 8) * bitrate_bps // sample_rate + padding

    if frame_len <= 0:
        return None
    return frame_len, samples_per_frame, sample_rate, version


def _skip_id3v2(data: bytes) -> int:
    """Return the byte offset just past an ID3v2 header, or 0 if absent."""
    if len(data) < 10 or data[0:3] != b"ID3":
        return 0
    # Syncsafe size: 4 bytes, high bit of each clear, 7 significant bits each.
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size


def mp3_duration_seconds(data: bytes) -> float:
    """Sum frame durations from MPEG audio frame headers, in **float
    seconds** -- deliberately unrounded (PLANS/phase-5.md §7.3/Q8).

    Phase 5's stitcher accumulates these and rounds **once per segment
    boundary**; rounding each segment to whole milliseconds *before* summing
    would accumulate up to 0.5 ms of error per segment (~165 ms over a
    330-chunk book -- a visible, monotonically growing highlight lag).

    Returns ``0.0`` when no valid frame is found (the caller treats that as a
    synthesis failure -- the bytes aren't a decodable MP3 stream)."""
    pos = _skip_id3v2(data)
    n = len(data)
    total_seconds = 0.0

    while pos + 4 <= n:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        parsed = _parse_frame_header(data[pos], data[pos + 1], data[pos + 2])
        if parsed is None:
            pos += 1
            continue
        frame_len, samples_per_frame, sample_rate, _version = parsed
        if pos + frame_len > n:
            # A truncated final frame: we can't confirm it's complete, so it
            # contributes nothing -- only fully-present frames count.
            break
        total_seconds += samples_per_frame / sample_rate
        pos += frame_len

    return total_seconds


def mp3_duration_ms(data: bytes) -> int:
    """Whole-millisecond duration. Behaviour is **unchanged** from phase 4 --
    ``mp3_duration_seconds`` is the identical scan without the final round,
    and every phase-4 caller (``EdgeTtsSynthesizer``/``GoogleTtsSynthesizer``)
    keeps its exact previous values."""
    return round(mp3_duration_seconds(data) * 1000)


# --- Container-header stripping (PLANS/phase-5.md §7.1) ---------------------

# A Xing/Info tag lives immediately after the frame header + the side
# information block, whose size depends on the MPEG version and whether the
# stream is mono. (4 + side_info): MPEG-1 stereo -> 36, MPEG-1 mono -> 21,
# MPEG-2/2.5 stereo -> 21, MPEG-2/2.5 mono -> 13. A VBRI tag is always at a
# fixed offset of 36 regardless of format.
_XING_TAGS = (b"Xing", b"Info")
_VBRI_TAG = b"VBRI"
_VBRI_OFFSET = 36
_MONO_CHANNEL_MODE = 0b11


def _side_info_bytes(version: str, channel_mode: int) -> int:
    mono = channel_mode == _MONO_CHANNEL_MODE
    if version == "MPEG1":
        return 17 if mono else 32
    return 9 if mono else 17


def _is_metadata_frame(frame: bytes, version: str, b3: int) -> bool:
    """True when this frame carries a Xing/Info/VBRI header instead of
    audio. Concatenating N of those embeds N bogus frames, and the *first*
    one's claimed frame count would describe only chunk 0 while a browser
    applies it to the whole stitched file -- wrong ``duration``, wrong
    seek."""
    offset = 4 + _side_info_bytes(version, (b3 >> 6) & 0x3)
    if frame[offset : offset + 4] in _XING_TAGS:
        return True
    return frame[_VBRI_OFFSET : _VBRI_OFFSET + 4] == _VBRI_TAG


def strip_container_headers(data: bytes) -> tuple[bytes, int | None]:
    """Drop a leading ID3v2 block and any leading Xing/Info/VBRI metadata
    frames, returning ``(audio_frames, sample_rate_hz_of_first_real_frame)``.

    This is what makes raw MPEG frame concatenation (PLANS/phase-5.md §7.1,
    no re-encoding, no ffmpeg in the image) produce one clean frame stream
    rather than N streams each with its own container preamble. Returns
    ``(b"", None)`` when no valid audio frame is found."""
    pos = _skip_id3v2(data)
    n = len(data)

    while pos + 4 <= n:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        parsed = _parse_frame_header(data[pos], data[pos + 1], data[pos + 2])
        if parsed is None:
            pos += 1
            continue
        frame_len, _samples, sample_rate, version = parsed
        if pos + frame_len > n:
            # Only a truncated frame left -- nothing usable follows it.
            break
        if _is_metadata_frame(data[pos : pos + frame_len], version, data[pos + 3]):
            pos += frame_len
            continue
        return data[pos:], sample_rate

    return b"", None
