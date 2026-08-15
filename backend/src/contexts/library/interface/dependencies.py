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
from src.contexts.library.domain.stitching import StitchQueue
from src.contexts.library.domain.storage import AudioDelivery, ObjectStorage, PdfStorage
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
from src.contexts.library.infrastructure.s3_audio_delivery import S3AudioDelivery
from src.contexts.library.infrastructure.s3_object_storage import S3ObjectStorage
from src.contexts.library.infrastructure.s3_pdf_storage import S3PdfStorage
from src.contexts.library.infrastructure.secrets import get_secret
from src.contexts.library.infrastructure.silent_synthesizer import SilentSynthesizer
from src.contexts.library.infrastructure.sqs_stitch_queue import SqsStitchQueue
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

def get_chat_repository():
    from src.contexts.library.infrastructure.dynamodb_chat_repository import DynamoDbChatRepository
    return DynamoDbChatRepository()

def get_chat_quota_repository():
    from src.contexts.library.infrastructure.dynamodb_chat_quota_repository import DynamoDbChatQuotaRepository
    return DynamoDbChatQuotaRepository()

def get_chat_model():
    """PLANS/phase-4.md §0's rule, applied to the LLM (PLANS/phase-7.md
    §5.2). The environment gate is checked FIRST and UNCONDITIONALLY: an
    OPENAI_SECRET_NAME accidentally set on a PR stack still cannot produce a
    network call, because OpenAiChatModel is never constructed outside prod.

    ``get_secret()`` is called EAGERLY here, while building the model -- as a
    FastAPI dependency this resolves before the route body runs and before
    any byte of the response (including the SSE `meta` frame) is sent, so a
    prod stack pointed at a secret that does not exist raises `ClientError`
    here and chat_app.py's handler maps it to one clean 503 before streaming
    ever starts (PLANS/phase-7.md §4.5 step 7, §6.4/OQ-A's correction)."""
    from src.contexts.library.infrastructure.openai_chat_model import OpenAiChatModel
    from src.contexts.library.infrastructure.stub_chat_model import StubChatModel
    from src.contexts.library.domain.chat import ChatDisabledReason

    if settings.environment != "prod":
        return StubChatModel(reason=ChatDisabledReason.NON_PROD)

    if not settings.openai_secret_name:
        return StubChatModel(reason=ChatDisabledReason.NOT_CONFIGURED)

    return OpenAiChatModel(
        api_key=get_secret(settings.openai_secret_name),
        model=settings.openai_model,
        max_output_tokens=settings.openai_max_output_tokens,
    )

def get_ask_book_question() -> AskBookQuestion:
    from src.contexts.library.application.chat import AskBookQuestion
    return AskBookQuestion(
        book_repository=get_book_repository(),
        chunk_repository=get_chunk_repository(),
        chat_repository=get_chat_repository(),
        chat_quota_repository=get_chat_quota_repository(),
        id_generator=get_id_generator(),
        clock=get_clock(),
        daily_limit=settings.chat_daily_limit,
    )

def get_list_book_chat():
    from src.contexts.library.application.chat import ListBookChat
    return ListBookChat(
        book_repository=get_book_repository(),
        chat_repository=get_chat_repository(),
    )

def get_clear_book_chat():
    from src.contexts.library.application.chat import ClearBookChat
    return ClearBookChat(
        book_repository=get_book_repository(),
        chat_repository=get_chat_repository(),
    )


def get_pdf_storage() -> PdfStorage:
    return S3PdfStorage(bucket=settings.pdf_bucket)


def get_pdf_extractor() -> PdfTextExtractor:
    return PyMuPdfTextExtractor()


def get_audio_storage() -> ObjectStorage:
    return S3ObjectStorage(bucket=settings.audio_bucket)


def get_marks_storage() -> ObjectStorage:
    return S3ObjectStorage(bucket=settings.marks_bucket)


def get_audio_delivery() -> AudioDelivery:
    """PLANS/phase-6.md §4.2. Note it builds ``public_s3_client()``, NOT
    ``src/infrastructure/aws.py``'s ``client("s3")``: boto3 signs presigned
    URLs against the client's own endpoint, so the latter would mint
    ``http://localstack:4566/...`` inside compose -- a well-formed, correctly
    signed URL that no browser can reach. Constructing it touches no network
    and needs no region (S3 has a global endpoint), so unlike the SQS
    adapters this needs no laziness."""
    return S3AudioDelivery(bucket=settings.audio_bucket)


def get_synthesis_queue() -> SynthesisQueue:
    return SqsSynthesisQueue(queue_url=settings.synthesize_queue_url)


def get_stitch_queue() -> StitchQueue:
    return SqsStitchQueue(queue_url=settings.stitch_queue_url)


def get_speech_synthesizer() -> SpeechSynthesizer:
    """PLANS/phase-4.md §0: the environment gate is checked FIRST and
    UNCONDITIONALLY -- local dev and every ephemeral PR stack never get a
    real engine, no exceptions, no opt-in flag. A `google_tts_secret_name`
    accidentally set on a PR stack still can't turn on real calls outside
    prod, because FallbackSynthesizer/GoogleTtsSynthesizer are simply never
    constructed for non-prod environments.

    ``SYNTHESIS_STUB_MODE=silent`` (PLANS/phase-5.md OQ-1) picks a *local,
    network-free* generator instead of the raise-only stub. It is checked
    strictly INSIDE the non-prod branch, so it cannot weaken the gate above:
    it changes which offline stand-in runs, never whether an external service
    is reachable."""
    if settings.environment != "prod":
        if settings.synthesis_stub_mode == "silent":
            return SilentSynthesizer()
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
