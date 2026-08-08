"""Test doubles for the synthesis pipeline (PLANS/phase-4.md §10.1) -- the
same fake-adapter pattern phase 3 used for ``PdfTextExtractor``, applied here
to a network dependency. Zero real network/asyncio anywhere in the test
suite that uses these.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.contexts.library.domain.synthesis import SynthesizedAudio
from src.shared_kernel.domain.errors import NotFoundError


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


class RecordingObjectStorage:
    """In-memory ``put_bytes``/``get_bytes`` -- lets a test assert the exact
    key written and decode the marks JSON without touching S3/moto."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.put_calls: list[str] = []

    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type
        self.put_calls.append(key)

    def get_bytes(self, *, key: str) -> bytes:
        if key not in self.objects:
            raise NotFoundError(f"No object at key: {key}")
        return self.objects[key]
