"""``ExtractBook`` -- the extract Lambda's use case (PLANS/phase-3.md §8.2).
The pipeline's "controller" (``interface/extract_handler.py``) builds one of
these per invocation and calls ``execute`` once per S3 event record.

Ordering is load-bearing and has a dedicated test
(``test_extraction_use_case.py``): chunks are written **before** the book
flips to ``EXTRACTED``, so any consumer that observes ``EXTRACTED`` is
guaranteed to find all ``chunksTotal`` chunks already in place -- phase 4's
fan-out depends on this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.chunking import chunk_text
from src.contexts.library.domain.extraction import ExtractionError, PdfTextExtractor, page_range_for
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.storage import PdfStorage
from src.contexts.library.domain.value_objects import BookStatus, ExtractionFailure
from src.shared_kernel.application.ports import Clock
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

# The claim only succeeds when the book is currently UPLOADED (first attempt)
# or FAILED (retry). EXTRACTED/READY are deliberately excluded -- claiming an
# already-extracted book would wipe chunks phase 4 may already be working on.
_CLAIMABLE_STATUSES = (BookStatus.UPLOADED, BookStatus.FAILED)

Outcome = Literal["EXTRACTED", "FAILED", "SKIPPED"]


@dataclass(frozen=True)
class ExtractBookCommand:
    user_id: str
    book_id: str
    source_key: str
    size_bytes: int | None = None


@dataclass(frozen=True)
class ExtractBookResult:
    outcome: Outcome
    chunks_written: int = 0
    page_count: int = 0
    reason: str | None = None


class ExtractBook:
    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        pdf_storage: PdfStorage,
        extractor: PdfTextExtractor,
        clock: Clock,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._pdf_storage = pdf_storage
        self._extractor = extractor
        self._clock = clock

    def execute(self, command: ExtractBookCommand) -> ExtractBookResult:
        book = self._book_repository.get(command.user_id, command.book_id)
        if book is None:
            return ExtractBookResult("SKIPPED", reason="BOOK_NOT_FOUND")
        if book.source_key != command.source_key:
            # Re-validates what the S3 key's own user/book ids already imply
            # (PLANS/phase-3.md §5.1) -- guards against a stale/duplicated
            # event pointing at a key the book no longer owns.
            return ExtractBookResult("SKIPPED", reason="KEY_MISMATCH")

        now = self._clock.now().isoformat()
        try:
            self._book_repository.update_status(
                command.user_id,
                command.book_id,
                BookStatus.EXTRACTING,
                expected_statuses=_CLAIMABLE_STATUSES,
                updated_at=now,
            )
        except ConflictError:
            # Duplicate-delivery guard: without this, a redelivery after
            # phase 4 has begun would save_all fresh PENDING chunks and wipe
            # audioKeys already written.
            return ExtractBookResult("SKIPPED", reason="ALREADY_CLAIMED")
        except NotFoundError:
            return ExtractBookResult("SKIPPED", reason="BOOK_NOT_FOUND")

        try:
            return self._extract_and_persist(command, now)
        except ExtractionError as exc:
            self._book_repository.update_status(
                command.user_id,
                command.book_id,
                BookStatus.FAILED,
                failure_reason=exc.reason.value,
                updated_at=self._clock.now().isoformat(),
            )
            return ExtractBookResult("FAILED", reason=exc.reason.value)
        except Exception:
            # Release the claim on any transient failure (S3 5xx, DynamoDB
            # throttle, timeout, ...) -- without this the book is wedged in
            # EXTRACTING forever, since every SQS retry hits the same claim
            # condition. Re-raise so SQS actually retries.
            self._book_repository.update_status(
                command.user_id,
                command.book_id,
                BookStatus.UPLOADED,
                updated_at=self._clock.now().isoformat(),
            )
            raise

    def _extract_and_persist(self, command: ExtractBookCommand, claimed_at: str) -> ExtractBookResult:
        pdf_bytes = self._pdf_storage.get_bytes(key=command.source_key)
        document = self._extractor.extract(pdf_bytes)

        boundaries = chunk_text(document.text)
        if not boundaries:
            raise ExtractionError(ExtractionFailure.NO_TEXT_LAYER, "No chunks produced from extracted text")

        chunks = []
        for index, boundary in enumerate(boundaries):
            page_start, page_end = page_range_for(document.pages, boundary.char_start, boundary.char_end)
            chunks.append(
                Chunk.create(
                    book_id=command.book_id,
                    user_id=command.user_id,
                    index=index,
                    text=document.text[boundary.char_start : boundary.char_end],
                    char_start=boundary.char_start,
                    char_end=boundary.char_end,
                    page_start=page_start,
                    page_end=page_end,
                )
            )

        # Stale chunks from a previous (failed/retried) extraction attempt
        # must not linger past a new chunk count -- delete before writing
        # the fresh set.
        self._chunk_repository.delete_for_book(command.book_id)
        self._chunk_repository.save_all(chunks)

        # Chunks are written before the status flip -- see this module's
        # docstring; a consumer observing EXTRACTED is guaranteed to find
        # every chunk already present.
        self._book_repository.update_status(
            command.user_id,
            command.book_id,
            BookStatus.EXTRACTED,
            chunks_total=len(chunks),
            page_count=document.page_count,
            clear_failure_reason=True,
            updated_at=self._clock.now().isoformat(),
        )
        return ExtractBookResult("EXTRACTED", chunks_written=len(chunks), page_count=document.page_count)
