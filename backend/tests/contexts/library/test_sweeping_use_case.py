from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.application.sweeping import (
    SweepDlq,
    SweepDlqResult,
    SweepExtractDlqCommand,
    SweepSynthesizeDlqCommand,
)
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.repository import ChunkCounters
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus, ExtractionFailure, SynthesisFailure
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.fakes import FakeStitchQueue

USER_ID = "user-1"
BOOK_ID = "book-1"
FIXED_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def _book(*, status: BookStatus, chunks_total: int = 1, chunks_done: int = 0, chunks_failed: int = 0) -> Book:
    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="A Book", now=FIXED_NOW)
    book.status = status
    book.chunks_total = chunks_total
    book.chunks_done = chunks_done
    book.chunks_failed = chunks_failed
    return book


class FakeBookRepository:
    """In-memory fake enforcing the same expected_statuses/ConflictError/
    NotFoundError semantics as the real DynamoDB adapter (mirrors
    test_synthesis_use_case.py's/test_extraction_use_case.py's fakes)."""

    def __init__(self, book: Book | None = None, *, missing_on_increment: bool = False) -> None:
        self.book = book
        self._missing_on_increment = missing_on_increment
        self.update_status_calls: list[dict] = []
        self.increment_calls: list[dict] = []

    def save(self, book: Book) -> None:
        self.book = book

    def get(self, user_id: str, book_id: str) -> Book | None:
        return self.book

    def list_for_user(self, user_id: str) -> list[Book]:
        return [self.book] if self.book else []

    def delete(self, user_id: str, book_id: str) -> None: ...
    def get_manifest_key(self, book_id: str) -> str | None: ...
    def get_audio_key(self, book_id: str) -> str | None: ...

    def update_status(
        self,
        user_id: str,
        book_id: str,
        status: BookStatus,
        *,
        expected_statuses=None,
        chunks_total=None,
        chunks_done=None,
        chunks_failed=None,
        page_count=None,
        audio_key=None,
        manifest_key=None,
        audio_duration_ms=None,
        clear_stitch_outputs: bool = False,
        failure_reason=None,
        clear_failure_reason: bool = False,
        updated_at=None,
    ) -> None:
        self.update_status_calls.append({"status": status, "failure_reason": failure_reason})
        if self.book is None:
            raise NotFoundError("Book not found")
        if expected_statuses is not None and self.book.status not in expected_statuses:
            raise ConflictError("Book is not in an expected status")
        self.book.status = status
        if failure_reason is not None:
            self.book.failure_reason = failure_reason

    def increment_chunks_done(self, user_id: str, book_id: str, *, failed: bool = False) -> ChunkCounters:
        self.increment_calls.append({"failed": failed})
        if self._missing_on_increment or self.book is None:
            raise NotFoundError("Book not found")
        self.book.chunks_done += 1
        if failed:
            self.book.chunks_failed += 1
        return ChunkCounters(
            chunks_done=self.book.chunks_done,
            chunks_total=self.book.chunks_total,
            chunks_failed=self.book.chunks_failed,
        )


class FakeChunkRepository:
    def __init__(self, chunk: Chunk | None = None, *, update_status_error: Exception | None = None) -> None:
        self.chunk = chunk
        self._update_status_error = update_status_error
        self.update_status_calls: list[dict] = []

    def save(self, chunk: Chunk) -> None:
        self.chunk = chunk

    def save_all(self, chunks) -> None: ...
    def get(self, book_id: str, index: int) -> Chunk | None:
        return self.chunk

    def list_for_book(self, book_id: str) -> list[Chunk]:
        return [self.chunk] if self.chunk else []

    def update_status(
        self,
        book_id: str,
        index: int,
        status: ChunkStatus,
        *,
        expected_statuses=None,
        audio_key=None,
        marks_key=None,
        duration_ms=None,
        synthesis_source=None,
        failure_reason=None,
        clear_failure_reason: bool = False,
    ) -> None:
        self.update_status_calls.append({"status": status, "failure_reason": failure_reason})
        if self._update_status_error is not None:
            raise self._update_status_error
        if self.chunk is None:
            raise NotFoundError("Chunk not found")
        if expected_statuses is not None and self.chunk.status not in expected_statuses:
            raise ConflictError("Chunk is not in an expected status")
        self.chunk.status = status
        if failure_reason is not None:
            self.chunk.failure_reason = failure_reason

    def delete_for_book(self, book_id: str) -> int:
        return 0


def _chunk(*, status: ChunkStatus) -> Chunk:
    return Chunk(
        book_id=BOOK_ID,
        index=0,
        user_id=USER_ID,
        text="chunk text",
        char_start=0,
        char_end=10,
        audio_key=None,
        marks_key=None,
        status=status,
    )


# --- sweep_extract -----------------------------------------------------------


@pytest.mark.parametrize("status", [BookStatus.UPLOADED, BookStatus.EXTRACTING])
def test_sweep_extract_marks_stranded_book_failed(status: BookStatus) -> None:
    book_repo = FakeBookRepository(_book(status=status))
    use_case = SweepDlq(book_repo, FakeChunkRepository(), FakeStitchQueue())

    result = use_case.sweep_extract(SweepExtractDlqCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "FAILED"
    assert result.reason == ExtractionFailure.DLQ_EXHAUSTED.value
    assert book_repo.book.status is BookStatus.FAILED
    assert book_repo.book.failure_reason == ExtractionFailure.DLQ_EXHAUSTED.value


def test_sweep_extract_book_not_found_is_skipped() -> None:
    use_case = SweepDlq(FakeBookRepository(None), FakeChunkRepository(), FakeStitchQueue())
    result = use_case.sweep_extract(SweepExtractDlqCommand(user_id=USER_ID, book_id=BOOK_ID))
    assert result.outcome == "SKIPPED"
    assert result.reason == "BOOK_NOT_FOUND"


@pytest.mark.parametrize(
    "status", [BookStatus.EXTRACTED, BookStatus.STITCHING, BookStatus.READY, BookStatus.PARTIAL, BookStatus.FAILED]
)
def test_sweep_extract_already_resolved_book_is_not_touched(status: BookStatus) -> None:
    book_repo = FakeBookRepository(_book(status=status))
    use_case = SweepDlq(book_repo, FakeChunkRepository(), FakeStitchQueue())

    result = use_case.sweep_extract(SweepExtractDlqCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_RESOLVED"
    assert book_repo.book.status is status  # untouched
    assert book_repo.update_status_calls == []


def test_sweep_extract_conflict_error_is_skipped() -> None:
    """A race: the book moved on between our `get` and the conditional
    `update_status` (e.g. a concurrent successful redelivery)."""

    class RacyBookRepository(FakeBookRepository):
        def update_status(self, *args, **kwargs) -> None:
            raise ConflictError("raced")

    book_repo = RacyBookRepository(_book(status=BookStatus.UPLOADED))
    use_case = SweepDlq(book_repo, FakeChunkRepository(), FakeStitchQueue())

    result = use_case.sweep_extract(SweepExtractDlqCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result == SweepDlqResult("SKIPPED", "ALREADY_RESOLVED")


def test_sweep_extract_not_found_error_is_skipped() -> None:
    """The book was deleted between our `get` and the conditional
    `update_status`."""

    class VanishingBookRepository(FakeBookRepository):
        def update_status(self, *args, **kwargs) -> None:
            raise NotFoundError("gone")

    book_repo = VanishingBookRepository(_book(status=BookStatus.UPLOADED))
    use_case = SweepDlq(book_repo, FakeChunkRepository(), FakeStitchQueue())

    result = use_case.sweep_extract(SweepExtractDlqCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result == SweepDlqResult("SKIPPED", "BOOK_NOT_FOUND")


# --- sweep_synthesize --------------------------------------------------------


def test_sweep_synthesize_chunk_not_found_is_skipped() -> None:
    use_case = SweepDlq(FakeBookRepository(None), FakeChunkRepository(None), FakeStitchQueue())
    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))
    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"


@pytest.mark.parametrize("status", [ChunkStatus.PENDING, ChunkStatus.SYNTHESIZING])
def test_sweep_synthesize_finalizes_stranded_non_terminal_chunk(status: ChunkStatus) -> None:
    """Chunk never got finalized by SynthesizeChunk's own last-attempt rule
    (e.g. a composition-root crash) -- the sweeper finalizes it itself,
    exactly as that rule would have."""
    chunk_repo = FakeChunkRepository(_chunk(status=status))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=2, chunks_done=0))
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "FAILED"
    assert result.reason == SynthesisFailure.UNKNOWN.value
    assert chunk_repo.chunk.status is ChunkStatus.FAILED
    assert chunk_repo.chunk.failure_reason == SynthesisFailure.UNKNOWN.value
    assert book_repo.increment_calls == [{"failed": True}]
    assert stitch_queue.calls == []  # book not yet complete (chunks_total=2)


def test_sweep_synthesize_finalizing_the_completing_chunk_publishes_stitch() -> None:
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.PENDING))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=1, chunks_done=0))
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "STITCH_REQUEUED"
    assert stitch_queue.calls == [{"user_id": USER_ID, "book_id": BOOK_ID}]


def test_sweep_synthesize_finalize_book_deleted_mid_flight_returns_failed_without_stitch() -> None:
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.PENDING))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=1), missing_on_increment=True)
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "FAILED"
    assert stitch_queue.calls == []


def test_sweep_synthesize_terminal_chunk_complete_book_republishes_stitch() -> None:
    """PLANS/phase-5.md §4.3's named residual hole: the chunk itself is
    already DONE/counted, but the stitch enqueue kept failing until the
    message DLQ'd."""
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=1, chunks_done=1))
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "STITCH_REQUEUED"
    assert stitch_queue.calls == [{"user_id": USER_ID, "book_id": BOOK_ID}]
    assert book_repo.increment_calls == []  # never re-increments an already-terminal chunk


@pytest.mark.parametrize("status", [BookStatus.STITCHING, BookStatus.READY, BookStatus.PARTIAL])
def test_sweep_synthesize_terminal_chunk_book_already_moved_on_is_skipped(status: BookStatus) -> None:
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(_book(status=status, chunks_total=1, chunks_done=1))
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_TERMINAL"
    assert stitch_queue.calls == []


def test_sweep_synthesize_terminal_chunk_book_not_yet_complete_is_skipped() -> None:
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.FAILED))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=2, chunks_done=1))
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_TERMINAL"
    assert stitch_queue.calls == []


def test_sweep_synthesize_finalize_conflict_error_falls_through_to_stitch_check() -> None:
    """Another invocation finalized the chunk (DONE/FAILED, already counted)
    between our `get` and the conditional `update_status` -- do not
    double-increment, but still check for stitch completion."""
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.PENDING), update_status_error=ConflictError("raced"))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=1, chunks_done=1))
    stitch_queue = FakeStitchQueue()
    use_case = SweepDlq(book_repo, chunk_repo, stitch_queue)

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "STITCH_REQUEUED"
    assert book_repo.increment_calls == []


def test_sweep_synthesize_finalize_conflict_error_book_not_yet_complete_is_skipped() -> None:
    """Same race as above, but the book isn't complete yet -- no stitch to
    republish either."""
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.PENDING), update_status_error=ConflictError("raced"))
    book_repo = FakeBookRepository(_book(status=BookStatus.EXTRACTED, chunks_total=2, chunks_done=1))
    use_case = SweepDlq(book_repo, chunk_repo, FakeStitchQueue())

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result == SweepDlqResult("SKIPPED", "ALREADY_TERMINAL")


def test_sweep_synthesize_finalize_not_found_error_is_skipped() -> None:
    chunk_repo = FakeChunkRepository(_chunk(status=ChunkStatus.PENDING), update_status_error=NotFoundError("gone"))
    use_case = SweepDlq(FakeBookRepository(None), chunk_repo, FakeStitchQueue())

    result = use_case.sweep_synthesize(SweepSynthesizeDlqCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=0))

    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"
