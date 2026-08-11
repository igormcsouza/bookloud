from __future__ import annotations

import pytest

import src.config as config
from src.contexts.library.infrastructure.dynamodb_book_repository import (
    DynamoDbBookRepository,
)
from src.contexts.library.infrastructure.dynamodb_chunk_repository import (
    DynamoDbChunkRepository,
)
from src.contexts.library.infrastructure.edge_tts_synthesizer import EdgeTtsSynthesizer
from src.contexts.library.infrastructure.fallback_synthesizer import FallbackSynthesizer
from src.contexts.library.infrastructure.pymupdf_extractor import PyMuPdfTextExtractor
from src.contexts.library.infrastructure.s3_object_storage import S3ObjectStorage
from src.contexts.library.infrastructure.s3_pdf_storage import S3PdfStorage
from src.contexts.library.infrastructure.silent_synthesizer import SilentSynthesizer
from src.contexts.library.infrastructure.sqs_stitch_queue import SqsStitchQueue
from src.contexts.library.infrastructure.sqs_synthesis_queue import SqsSynthesisQueue
from src.contexts.library.infrastructure.stub_synthesizer import StubSynthesizer
from src.contexts.library.interface.dependencies import (
    get_audio_storage,
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_id_generator,
    get_marks_storage,
    get_pdf_extractor,
    get_pdf_storage,
    get_speech_synthesizer,
    get_stitch_queue,
    get_synthesis_queue,
)
from src.infrastructure.clock import SystemClock
from src.infrastructure.ids import Uuid4IdGenerator


def test_get_book_repository_returns_dynamodb_adapter(dynamodb_table) -> None:
    assert isinstance(get_book_repository(), DynamoDbBookRepository)


def test_get_chunk_repository_returns_dynamodb_adapter(dynamodb_table) -> None:
    assert isinstance(get_chunk_repository(), DynamoDbChunkRepository)


def test_get_clock_returns_system_clock() -> None:
    assert isinstance(get_clock(), SystemClock)


def test_get_id_generator_returns_uuid4_generator() -> None:
    assert isinstance(get_id_generator(), Uuid4IdGenerator)


def test_get_pdf_storage_returns_s3_adapter() -> None:
    assert isinstance(get_pdf_storage(), S3PdfStorage)


def test_get_pdf_extractor_returns_pymupdf_adapter() -> None:
    assert isinstance(get_pdf_extractor(), PyMuPdfTextExtractor)


def test_get_audio_storage_returns_s3_adapter() -> None:
    assert isinstance(get_audio_storage(), S3ObjectStorage)


def test_get_marks_storage_returns_s3_adapter() -> None:
    assert isinstance(get_marks_storage(), S3ObjectStorage)


def test_get_synthesis_queue_returns_sqs_adapter() -> None:
    assert isinstance(get_synthesis_queue(), SqsSynthesisQueue)


# --- get_speech_synthesizer: environment gating (PLANS/phase-4.md §0) ------


@pytest.mark.parametrize("environment", ["local", "pr-1", "pr-42", "staging", "dev"])
def test_get_speech_synthesizer_returns_stub_outside_prod(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setattr(config.settings, "environment", environment)
    assert isinstance(get_speech_synthesizer(), StubSynthesizer)


def test_get_speech_synthesizer_returns_stub_even_with_google_secret_set_outside_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings, "environment", "pr-1")
    monkeypatch.setattr(config.settings, "google_tts_secret_name", "bookloud/google-tts-api-key")
    assert isinstance(get_speech_synthesizer(), StubSynthesizer)


def test_get_speech_synthesizer_returns_bare_edge_tts_in_prod_without_google_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings, "environment", "prod")
    monkeypatch.setattr(config.settings, "google_tts_secret_name", "")
    synthesizer = get_speech_synthesizer()
    assert isinstance(synthesizer, EdgeTtsSynthesizer)


def test_get_speech_synthesizer_returns_fallback_wrapper_in_prod_with_google_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings, "environment", "prod")
    monkeypatch.setattr(config.settings, "google_tts_secret_name", "bookloud/google-tts-api-key")
    monkeypatch.setattr(
        "src.contexts.library.interface.dependencies.get_secret", lambda name: "fake-api-key"
    )
    synthesizer = get_speech_synthesizer()
    assert isinstance(synthesizer, FallbackSynthesizer)


# --- phase 5 -----------------------------------------------------------------


def test_get_stitch_queue_returns_sqs_adapter_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing this is pure DI wiring: it must not resolve a region or
    credentials, because the backend CI job has neither."""
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config.settings, "stitch_queue_url", "https://sqs.example/stitch")

    queue = get_stitch_queue()

    assert isinstance(queue, SqsStitchQueue)
    assert queue._queue_url == "https://sqs.example/stitch"  # noqa: SLF001


def test_get_speech_synthesizer_returns_silent_when_stub_mode_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings, "environment", "local")
    monkeypatch.setattr(config.settings, "synthesis_stub_mode", "silent")
    assert isinstance(get_speech_synthesizer(), SilentSynthesizer)


@pytest.mark.parametrize("mode", ["disabled", "", "SILENT", "anything-else"])
def test_get_speech_synthesizer_returns_the_raise_only_stub_for_any_other_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(config.settings, "environment", "local")
    monkeypatch.setattr(config.settings, "synthesis_stub_mode", mode)
    assert isinstance(get_speech_synthesizer(), StubSynthesizer)


def test_synthesis_stub_mode_cannot_enable_a_real_engine_outside_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OQ-1 flag is checked strictly INSIDE the non-prod branch, so it
    changes which offline stand-in runs -- never whether an external service
    is reachable (PLANS/phase-4.md §0)."""
    monkeypatch.setattr(config.settings, "environment", "pr-42")
    monkeypatch.setattr(config.settings, "synthesis_stub_mode", "silent")
    monkeypatch.setattr(config.settings, "google_tts_secret_name", "bookloud/google-tts-api-key")

    synthesizer = get_speech_synthesizer()

    assert isinstance(synthesizer, SilentSynthesizer)
    assert not isinstance(synthesizer, (EdgeTtsSynthesizer, FallbackSynthesizer))


def test_synthesis_stub_mode_is_ignored_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "environment", "prod")
    monkeypatch.setattr(config.settings, "synthesis_stub_mode", "silent")
    monkeypatch.setattr(config.settings, "google_tts_secret_name", "")
    assert isinstance(get_speech_synthesizer(), EdgeTtsSynthesizer)
