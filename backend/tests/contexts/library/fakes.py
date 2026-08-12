"""Test doubles for the synthesis pipeline (PLANS/phase-4.md §10.1) -- the
same fake-adapter pattern phase 3 used for ``PdfTextExtractor``, applied here
to a network dependency. Zero real network/asyncio anywhere in the test
suite that uses these.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.contexts.library.domain.storage import AUDIO_URL_EXPIRES_IN, PresignedDownload
from src.contexts.library.domain.synthesis import SynthesizedAudio
from src.shared_kernel.domain.errors import NotFoundError


class FakeAudioDelivery:
    """Satisfies ``AudioDelivery`` (PLANS/phase-6.md §4.2). Records every
    ``(key, expires_in)`` so a test can assert *which* key was presigned --
    the book's stitched ``book.mp3`` versus a chunk's own file -- without
    parsing a signature."""

    def __init__(self, *, base_url: str = "https://s3.example/signed") -> None:
        self._base_url = base_url
        self.calls: list[dict] = []

    def presigned_download(
        self, *, key: str, expires_in: int = AUDIO_URL_EXPIRES_IN
    ) -> PresignedDownload:
        self.calls.append({"key": key, "expires_in": expires_in})
        return PresignedDownload(
            url=f"{self._base_url}/{key}?X-Amz-Signature=fake", expires_in=expires_in
        )


class FakeSynthesizer:
    """Satisfies ``SpeechSynthesizer``: returns a canned ``SynthesizedAudio``
    or raises a programmable exception; records every call."""

    def __init__(
        self,
        *,
        name: str = "fake",
        result: SynthesizedAudio | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def synthesize(self, text: str) -> SynthesizedAudio:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        assert self._result is not None, "FakeSynthesizer needs either result= or error="
        return self._result


class FakeCommunicate:
    """Mimics ``edge_tts.Communicate``'s constructor/``stream()`` shape.
    Constructed with a scripted ``chunks``/``error``; each real construction
    (by ``EdgeTtsSynthesizer``) records its own ``(text, voice, boundary,
    ...)`` args on the instance so a test can inspect the *last* instance a
    ``FakeCommunicateFactory`` produced."""

    def __init__(
        self,
        text: str,
        voice: str,
        *,
        boundary: str = "SentenceBoundary",
        connect_timeout: int | None = None,
        receive_timeout: int | None = None,
        chunks: list[dict] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.text = text
        self.voice = voice
        self.boundary = boundary
        self.connect_timeout = connect_timeout
        self.receive_timeout = receive_timeout
        self._chunks = chunks or []
        self._error = error

    async def stream(self):
        if self._error is not None:
            raise self._error
        for chunk in self._chunks:
            yield chunk


class FakeCommunicateFactory:
    """A callable, drop-in replacement for the ``edge_tts.Communicate``
    *class* itself -- ``EdgeTtsSynthesizer`` calls
    ``self._communicate_cls(text, voice, boundary=..., ...)`` positionally/
    by keyword exactly as it would the real class. Injecting an instance of
    this (via ``communicate_cls=``) means no monkeypatching of the
    ``edge_tts`` module's globals is ever needed. ``instances`` records every
    ``FakeCommunicate`` actually constructed, so a test can assert the exact
    args ``EdgeTtsSynthesizer`` passed."""

    def __init__(
        self,
        *,
        chunks: list[dict] | None = None,
        error: Exception | None = None,
        errors: list[Exception | None] | None = None,
    ) -> None:
        self._chunks = chunks
        self._error = error
        # `errors`, when given, scripts a DIFFERENT error (or None -> use
        # `chunks`) per successive construction -- lets a test express
        # "first attempt fails, second attempt succeeds".
        self._errors = list(errors) if errors is not None else None
        self.instances: list[FakeCommunicate] = []

    def __call__(self, text: str, voice: str, **kwargs) -> FakeCommunicate:
        error = self._error
        if self._errors is not None:
            error = self._errors.pop(0) if self._errors else None
        instance = FakeCommunicate(text, voice, chunks=self._chunks, error=error, **kwargs)
        self.instances.append(instance)
        return instance


class FakeHttpPost:
    """``(url, body, timeout) -> bytes``; injected via ``http_post=`` into
    ``GoogleTtsSynthesizer``. Each call pops the next scripted response;
    a scripted ``Exception`` is raised instead of returned."""

    def __init__(self, responses: list[bytes | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, bytes, float]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> bytes:
        self.calls.append((url, body, timeout))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSynthesisQueue:
    """Records ``enqueue_chunks`` calls; programmable failure."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict] = []

    def enqueue_chunks(self, *, user_id: str, book_id: str, chunk_indexes: Iterable[int]) -> int:
        indexes = list(chunk_indexes)
        self.calls.append({"user_id": user_id, "book_id": book_id, "chunk_indexes": indexes})
        if self._error is not None:
            raise self._error
        return len(indexes)


class FakeStitchQueue:
    """Records ``enqueue_book`` calls; programmable failure. Mirrors
    ``FakeSynthesisQueue`` (PLANS/phase-5.md §10.1)."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict] = []

    def enqueue_book(self, *, user_id: str, book_id: str) -> None:
        self.calls.append({"user_id": user_id, "book_id": book_id})
        if self._error is not None:
            raise self._error


class RecordingMultipartWriter:
    """In-memory ``MultipartWriter``: concatenates on ``write`` and stores
    the result on a clean ``__exit__``. An exception inside the ``with``
    leaves **nothing** stored, exactly like ``S3MultipartWriter``'s abort --
    which is what lets a test assert the exact stitched bytes *and* that the
    failure path writes no half-formed object."""

    def __init__(self, storage: RecordingObjectStorage, key: str, content_type: str) -> None:
        self._storage = storage
        self._key = key
        self._content_type = content_type
        self._buffer = bytearray()
        self.aborted = False

    @property
    def bytes_written(self) -> int:
        return len(self._buffer)

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)

    def __enter__(self) -> RecordingMultipartWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.aborted = True
            return None
        self._storage.objects[self._key] = bytes(self._buffer)
        self._storage.content_types[self._key] = self._content_type
        self._storage.put_calls.append(self._key)
        self._storage.record_event("write")
        return None


class RecordingObjectStorage:
    """In-memory ``put_bytes``/``get_bytes``/``open_multipart`` -- lets a
    test assert the exact key written and decode the marks JSON without
    touching S3/moto.

    Pass a shared ``events`` list plus a ``label`` to make this storage
    participate in a cross-adapter ordering spy (``"write:audio"`` etc.),
    which is how the stitcher's audio -> manifest -> DynamoDB ordering rule
    is asserted."""

    def __init__(self, *, events: list[str] | None = None, label: str = "storage") -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []
        self.multipart_writers: list[RecordingMultipartWriter] = []
        self.events = events
        self.label = label

    def record_event(self, name: str) -> None:
        if self.events is not None:
            self.events.append(f"{name}:{self.label}")

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type
        self.put_calls.append(key)
        self.record_event("write")

    def get_bytes(self, *, key: str) -> bytes:
        self.get_calls.append(key)
        if key not in self.objects:
            raise NotFoundError(f"No object at key: {key}")
        return self.objects[key]

    def open_multipart(self, *, key: str, content_type: str) -> RecordingMultipartWriter:
        writer = RecordingMultipartWriter(self, key, content_type)
        self.multipart_writers.append(writer)
        return writer


# --- MPEG frame builders (PLANS/phase-5.md §10.1) ---------------------------
# Promoted from test_mp3.py's `_frame_bytes` helper: the one fixture that
# makes the whole byte path testable without committing a binary blob. The
# ISO 11172-3/13818-3 formula is duplicated here independently of mp3.py's
# private tables, so these tests aren't merely checking the implementation
# against itself.

_MPEG1_LAYER3_BITRATES = [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
_MPEG2_LAYER3_BITRATES = [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None]
_MPEG1_SAMPLE_RATES = [44100, 48000, 32000]
_MPEG2_SAMPLE_RATES = [22050, 24000, 16000]

MONO = 0b11
STEREO = 0b00


def mpeg_frame(
    *,
    version_bits: int = 0b10,
    layer_bits: int = 0b01,
    bitrate_idx: int = 6,
    sample_rate_idx: int = 1,
    padding: int = 0,
    channel_mode: int = MONO,
    payload: bytes | None = None,
) -> bytes:
    """One syntactically valid MPEG audio frame. Defaults to MPEG-2 Layer III
    @ 24 kHz mono 48 kbps -- edge-tts's real output format
    (``audio-24khz-48kbitrate-mono-mp3``)."""
    is_v1 = version_bits == 0b11
    bitrate_kbps = (_MPEG1_LAYER3_BITRATES if is_v1 else _MPEG2_LAYER3_BITRATES)[bitrate_idx]
    sample_rate = (_MPEG1_SAMPLE_RATES if is_v1 else _MPEG2_SAMPLE_RATES)[sample_rate_idx]
    samples_per_frame = 1152 if is_v1 else 576
    frame_len = (samples_per_frame // 8) * (bitrate_kbps * 1000) // sample_rate + padding

    b0 = 0xFF
    b1 = 0xE0 | (version_bits << 3) | (layer_bits << 1) | 0x1  # protection bit set (no CRC)
    b2 = (bitrate_idx << 4) | (sample_rate_idx << 2) | (padding << 1)
    b3 = channel_mode << 6
    header = bytes([b0, b1, b2, b3])
    body = bytes(frame_len - 4) if payload is None else payload[: frame_len - 4].ljust(frame_len - 4, b"\x00")
    return header + body


def mpeg2_frames(count: int, *, sample_rate_idx: int = 1) -> bytes:
    """``count`` identical MPEG-2 Layer III mono frames (24 ms each at
    24 kHz)."""
    return mpeg_frame(sample_rate_idx=sample_rate_idx) * count


def xing_frame(*, tag: bytes = b"Xing", channel_mode: int = MONO) -> bytes:
    """An MPEG-2 mono frame whose payload carries a Xing/Info tag at the
    standard post-side-info offset (4 + 9 == 13 for MPEG-2 mono)."""
    offset = 4 + (9 if channel_mode == MONO else 17)
    payload = bytearray(200)
    payload[offset - 4 : offset - 4 + 4] = tag
    return mpeg_frame(channel_mode=channel_mode, payload=bytes(payload))


def vbri_frame() -> bytes:
    """An MPEG-2 mono frame carrying a VBRI tag at its fixed offset 36."""
    payload = bytearray(200)
    payload[36 - 4 : 36 - 4 + 4] = b"VBRI"
    return mpeg_frame(payload=bytes(payload))


def id3v2_header(body_size: int) -> bytes:
    """``ID3`` + version + flags + syncsafe size, followed by ``body_size``
    zero bytes."""
    syncsafe = bytes(
        [
            (body_size >> 21) & 0x7F,
            (body_size >> 14) & 0x7F,
            (body_size >> 7) & 0x7F,
            body_size & 0x7F,
        ]
    )
    return b"ID3" + b"\x03\x00" + b"\x00" + syncsafe + bytes(body_size)
