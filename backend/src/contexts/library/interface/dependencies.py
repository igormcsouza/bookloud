"""FastAPI ``Depends`` providers -- the composition root for the Library
context. Each provider constructs **per request**, never at import time: the
FastAPI equivalent of jgautocar's DI container resolved fresh on every
request. This is also what lets the moto fixture work with zero
``app.dependency_overrides`` -- ``DynamoDbBookRepository()``/
``DynamoDbChunkRepository()`` call ``library_table()`` at call time, by
which point a test's ``mock_aws()``/``monkeypatch`` is already active.

``get_pdf_storage``/``get_pdf_extractor`` are plain factory functions (not
only FastAPI ``Depends`` providers): ``interface/extract_handler.py``'s
Lambda composition root calls them directly too, so the Library context has
exactly one place that knows how to build each adapter.
"""

from __future__ import annotations

from src.config import settings
from src.contexts.library.domain.extraction import PdfTextExtractor
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.storage import ObjectStorage, PdfStorage
from src.contexts.library.domain.synthesis import SpeechSynthesizer, SynthesisQueue
from src.contexts.library.infrastructure.dynamodb_book_repository import (
    DynamoDbBookRepository,
)
from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
    DynamoDbChunkRepository,
)
from src.contexts.library.infrastructure.edge_tts_synthesizer import EdgeTtsSynthesizer
from src.contexts.library.infrastructure.fallback_synthesizer import FallbackSynthesizer
from src.contexts.library.infrastructure.google_tts_synthesizer import GoogleTtsSynthesizer
from src.contexts.library.infrastructure.pymupdf_extractor import PyMuPdfTextExtractor
from src.contexts.library.infrastructure.s3_object_storage import S3ObjectStorage
from src.contexts.library.infrastructure.s3_pdf_storage import S3PdfStorage
from src.contexts.library.infrastructure.secrets import get_secret
from src.contexts.library.infrastructure.sqs_synthesis_queue import SqsSynthesisQueue
from src.contexts.library.infrastructure.stub_synthesizer import StubSynthesizer
from src.infrastructure.clock import SystemClock
from src.infrastructure.ids import Uuid4IdGenerator
from src.shared_kernel.application.ports import Clock, IdGenerator


def get_book_repository() -> BookRepository:
    return DynamoDbBookRepository()


def get_chunk_repository() -> ChunkRepository:
    return DynamoDbChunkRepository()


def get_clock() -> Clock:
    return SystemClock()


def get_id_generator() -> IdGenerator:
    return Uuid4IdGenerator()


def get_pdf_storage() -> PdfStorage:
    return S3PdfStorage(bucket=settings.pdf_bucket)


def get_pdf_extractor() -> PdfTextExtractor:
    return PyMuPdfTextExtractor()


def get_audio_storage() -> ObjectStorage:
    return S3ObjectStorage(bucket=settings.audio_bucket)


def get_marks_storage() -> ObjectStorage:
    return S3ObjectStorage(bucket=settings.marks_bucket)


def get_synthesis_queue() -> SynthesisQueue:
    return SqsSynthesisQueue(queue_url=settings.synthesize_queue_url)


def get_speech_synthesizer() -> SpeechSynthesizer:
    """PLANS/phase-4.md §0: the environment gate is checked FIRST and
    UNCONDITIONALLY -- local dev and every ephemeral PR stack always get a
    StubSynthesizer, no exceptions, no opt-in flag. A `google_tts_secret_name`
    accidentally set on a PR stack still can't turn on real calls outside
    prod, because FallbackSynthesizer/GoogleTtsSynthesizer are simply never
    constructed for non-prod environments."""
    if settings.environment != "prod":
        return StubSynthesizer()
    primary = EdgeTtsSynthesizer(voice=settings.edge_tts_voice)
    if not settings.google_tts_secret_name:
        return primary
    return FallbackSynthesizer(
        primary,
        GoogleTtsSynthesizer(
            api_key=get_secret(settings.google_tts_secret_name), voice=settings.google_tts_voice
        ),
    )
