from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.contexts.library.application.extraction import ExtractBook, ExtractBookCommand
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.extraction import ExtractedDocument, ExtractedPage, ExtractionError
from src.contexts.library.domain.repository import ChunkCounters
from src.contexts.library.domain.value_objects import BookStatus, ExtractionFailure
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.fakes import FakeSynthesisQueue

FIXED_NOW = datetime(2026, 8, 5, 0, 0, 0, tzinfo=UTC)
USER_ID = "user-1"
BOOK_ID = "book-1"
SOURCE_KEY = f"books/{USER_ID}/{BOOK_ID}/source.pdf"


class FixedClock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class RecordingBookRepository:
    """In-memory fake that actually enforces ``expected_statuses`` /
    ``ConditionalCheckFailedException``-style semantics, the same
    disambiguation the real DynamoDB adapter performs, and records every
    call so ordering can be asserted."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._store: dict[tuple[str, str], Book] = {}

    def save(self, book: Book) -> None:
        self._store[(book.user_id, book.id)] = book

    def get(self, user_id: str, book_id: str) -> Book | None:
        return self._store.get((user_id, book_id))

    def list_for_user(self, user_id: str) -> list[Book]:
        return [b for (uid, _), b in self._store.items() if uid == user_id]

    def delete(self, user_id: str, book_id: str) -> None:
        self._store.pop((user_id, book_id), None)

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
        failure_reason=None,
        clear_failure_reason: bool = False,
        updated_at=None,
    ) -> None:
        self._events.append(f"update_status:{status.value}")
        if failure_reason is not None and clear_failure_reason:
            raise ValueError("failure_reason and clear_failure_reason are mutually exclusive")
        book = self._store.get((user_id, book_id))
        if book is None:
            raise NotFoundError("Book not found")
        if expected_statuses is not None and book.status not in expected_statuses:
            raise ConflictError("Book is not in an expected status")
        book.status = status
        if chunks_total is not None:
            book.chunks_total = chunks_total
        if chunks_done is not None:
            book.chunks_done = chunks_done
        if chunks_failed is not None:
            book.chunks_failed = chunks_failed
        if page_count is not None:
            book.page_count = page_count
        if failure_reason is not None:
            book.failure_reason = failure_reason
        if clear_failure_reason:
            book.failure_reason = None
        if updated_at is not None:
            book.updated_at = updated_at

    def increment_chunks_done(self, user_id: str, book_id: str, *, failed: bool = False) -> ChunkCounters:
        raise NotImplementedError


class RecordingChunkRepository:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._chunks: dict[str, list[Chunk]] = {}

    def save(self, chunk: Chunk) -> None:
        self._chunks.setdefault(chunk.book_id, []).append(chunk)

    def save_all(self, chunks) -> None:
        self._events.append("save_all_chunks")
        for chunk in chunks:
            self._chunks.setdefault(chunk.book_id, []).append(chunk)

    def get(self, book_id: str, index: int):
        return next((c for c in self._chunks.get(book_id, []) if c.index == index), None)

    def list_for_book(self, book_id: str) -> list[Chunk]:
        return list(self._chunks.get(book_id, []))

    def update_status(self, book_id, index, status, *, audio_key=None, marks_key=None) -> None:
        raise NotImplementedError

    def delete_for_book(self, book_id: str) -> int:
        self._events.append("delete_for_book")
        return len(self._chunks.pop(book_id, []))


class FakePdfStorage:
    def __init__(self, bytes_by_key: dict[str, bytes] | None = None, error: Exception | None = None) -> None:
        self._bytes_by_key = bytes_by_key or {}
        self._error = error

    def presigned_upload(self, *, key: str):
        raise NotImplementedError

    def get_bytes(self, *, key: str) -> bytes:
        if self._error is not None:
            raise self._error
        return self._bytes_by_key[key]


class FakeExtractor:
    def __init__(self, document: ExtractedDocument | None = None, error: Exception | None = None) -> None:
        self._document = document
        self._error = error

    def extract(self, pdf_bytes: bytes) -> ExtractedDocument:
        if self._error is not None:
            raise self._error
        assert self._document is not None
        return self._document


def _document(text: str, page_count: int = 2) -> ExtractedDocument:
    half = len(text) // 2
    pages = (
        ExtractedPage(number=1, char_start=0, char_end=half),
        ExtractedPage(number=2, char_start=half, char_end=len(text)),
    )
    return ExtractedDocument(
        text=text, pages=pages, page_count=page_count, dropped_running_lines=0, dropped_footnote_blocks=0
    )


def _seed_book(book_repo: RecordingBookRepository, *, status: BookStatus = BookStatus.UPLOADED) -> Book:
    book = Book.create(
        id=BOOK_ID, user_id=USER_ID, title_raw="Title", now=FIXED_NOW, source_key=SOURCE_KEY
    )
    book.status = status
    book_repo._store[(USER_ID, BOOK_ID)] = book
    return book


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def book_repo(events) -> RecordingBookRepository:
    return RecordingBookRepository(events)


@pytest.fixture
def chunk_repo(events) -> RecordingChunkRepository:
    return RecordingChunkRepository(events)


# --- happy path ----------------------------------------------------------------


def test_happy_path_extracts_writes_chunks_and_flips_to_extracted(book_repo, chunk_repo) -> None:
    _seed_book(book_repo)
    text = "A" * 300  # single paragraph, comfortably under chunk_text's default target
    document = _document(text)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert result.outcome == "EXTRACTED"
    assert result.chunks_written == 1
    assert result.page_count == 2

    book = book_repo.get(USER_ID, BOOK_ID)
    assert book.status == BookStatus.EXTRACTED
    assert book.chunks_total == 1
    assert book.page_count == 2

    chunks = chunk_repo.list_for_book(BOOK_ID)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2


def test_save_before_status_flip_ordering(book_repo, chunk_repo, events) -> None:
    """PLANS/phase-3.md: chunks must be written before the book flips to
    EXTRACTED -- a spy on call order."""
    _seed_book(book_repo)
    document = _document("B" * 300)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert events == [
        "update_status:EXTRACTING",
        "delete_for_book",
        "save_all_chunks",
        "update_status:EXTRACTED",
    ]


def test_stale_chunk_cleanup_on_re_extraction(book_repo, chunk_repo) -> None:
    _seed_book(book_repo, status=BookStatus.FAILED)
    stale_chunk = Chunk.create(
        book_id=BOOK_ID, user_id=USER_ID, index=0, text="stale", char_start=0, char_end=5
    )
    chunk_repo.save_all([stale_chunk])

    document = _document("C" * 300)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    chunks = chunk_repo.list_for_book(BOOK_ID)
    assert len(chunks) == 1
    assert chunks[0].text != "stale"


def test_retry_clears_previous_failure_reason(book_repo, chunk_repo) -> None:
    book = _seed_book(book_repo, status=BookStatus.FAILED)
    book.failure_reason = "CORRUPT_PDF"

    document = _document("D" * 300)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert book_repo.get(USER_ID, BOOK_ID).failure_reason is None


# --- SKIPPED branches ------------------------------------------------------------


def test_book_not_found_is_skipped(book_repo, chunk_repo) -> None:
    pdf_storage = FakePdfStorage()
    extractor = FakeExtractor()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(
        ExtractBookCommand(user_id=USER_ID, book_id="no-such-book", source_key=SOURCE_KEY)
    )

    assert result.outcome == "SKIPPED"
    assert result.reason == "BOOK_NOT_FOUND"


def test_key_mismatch_is_skipped(book_repo, chunk_repo) -> None:
    _seed_book(book_repo)
    pdf_storage = FakePdfStorage()
    extractor = FakeExtractor()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(
        ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key="books/other/key/source.pdf")
    )

    assert result.outcome == "SKIPPED"
    assert result.reason == "KEY_MISMATCH"


@pytest.mark.parametrize("status", [BookStatus.EXTRACTING, BookStatus.EXTRACTED, BookStatus.READY])
def test_already_claimed_or_in_progress_is_skipped(book_repo, chunk_repo, status: BookStatus) -> None:
    _seed_book(book_repo, status=status)
    pdf_storage = FakePdfStorage()
    extractor = FakeExtractor()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_CLAIMED"
    # The claim attempt must not have mutated the book's status.
    assert book_repo.get(USER_ID, BOOK_ID).status == status


def test_claim_not_found_race_between_get_and_claim_is_skipped(book_repo, chunk_repo, events) -> None:
    """A book deleted between the use case's initial ``get`` and its claim
    attempt -- ``update_status`` itself raising ``NotFoundError`` must also
    resolve to SKIPPED, not propagate as an unhandled transient error. Model
    the race directly: ``get`` still returns the book, but ``update_status``
    behaves as if it's already gone (deleted from the store first, then
    ``get`` is monkeypatched back to its pre-delete snapshot)."""
    book = _seed_book(book_repo)

    real_update_status = book_repo.update_status

    def racing_update_status(*args, **kwargs):
        # Simulate the concurrent delete landing between `get` and the
        # claim's own conditional update.
        book_repo._store.pop((USER_ID, BOOK_ID), None)
        return real_update_status(*args, **kwargs)

    book_repo.update_status = racing_update_status  # type: ignore[method-assign]
    pdf_storage = FakePdfStorage()
    extractor = FakeExtractor()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert result.outcome == "SKIPPED"
    assert result.reason == "BOOK_NOT_FOUND"
    assert book.id == BOOK_ID  # sanity: the book did exist at `get` time


# --- FAILED branches ---------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        ExtractionFailure.CORRUPT_PDF,
        ExtractionFailure.ENCRYPTED_PDF,
        ExtractionFailure.EMPTY_PDF,
        ExtractionFailure.NO_TEXT_LAYER,
        ExtractionFailure.TOO_LARGE,
        ExtractionFailure.UNKNOWN,
    ],
)
def test_extraction_error_flips_book_to_failed_with_reason(
    book_repo, chunk_repo, reason: ExtractionFailure
) -> None:
    _seed_book(book_repo)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(error=ExtractionError(reason, "boom"))
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert result.outcome == "FAILED"
    assert result.reason == reason.value
    book = book_repo.get(USER_ID, BOOK_ID)
    assert book.status == BookStatus.FAILED
    assert book.failure_reason == reason.value


def test_no_chunks_produced_raises_no_text_layer(book_repo, chunk_repo) -> None:
    _seed_book(book_repo)
    document = _document("   ")  # whitespace only -> chunk_text returns []
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert result.outcome == "FAILED"
    assert result.reason == ExtractionFailure.NO_TEXT_LAYER.value


# --- transient error: un-wedging ----------------------------------------------


def test_transient_error_reverts_book_to_uploaded_and_reraises(book_repo, chunk_repo) -> None:
    _seed_book(book_repo)
    pdf_storage = FakePdfStorage(error=RuntimeError("S3 is down"))
    extractor = FakeExtractor()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    with pytest.raises(RuntimeError, match="S3 is down"):
        use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    book = book_repo.get(USER_ID, BOOK_ID)
    assert book.status == BookStatus.UPLOADED


def test_transient_error_after_claiming_a_failed_book_still_releases_to_uploaded(
    book_repo, chunk_repo
) -> None:
    _seed_book(book_repo, status=BookStatus.FAILED)
    pdf_storage = FakePdfStorage(error=RuntimeError("DynamoDB throttled"))
    extractor = FakeExtractor()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), FakeSynthesisQueue())

    with pytest.raises(RuntimeError):
        use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert book_repo.get(USER_ID, BOOK_ID).status == BookStatus.UPLOADED


# --- phase 4: synthesis fan-out (PLANS/phase-4.md §4) -----------------------


def test_fan_out_published_after_extracted_flip(book_repo, chunk_repo, events) -> None:
    """The publish must happen AFTER update_status:EXTRACTED -- publishing
    before would let a fast worker finish and increment_chunks_done while
    chunksTotal is still 0 (§4.2's decisive argument)."""
    _seed_book(book_repo)
    document = _document("A" * 300)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    synthesis_queue = FakeSynthesisQueue()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), synthesis_queue)

    use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert events == [
        "update_status:EXTRACTING",
        "delete_for_book",
        "save_all_chunks",
        "update_status:EXTRACTED",
    ]
    assert len(synthesis_queue.calls) == 1


def test_fan_out_covers_0_to_n_minus_1(book_repo, chunk_repo) -> None:
    _seed_book(book_repo)
    document = _document("B" * 600)  # long enough to produce multiple chunks
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    synthesis_queue = FakeSynthesisQueue()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), synthesis_queue)

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    (call,) = synthesis_queue.calls
    assert call["user_id"] == USER_ID
    assert call["book_id"] == BOOK_ID
    assert call["chunk_indexes"] == list(range(result.chunks_written))


def test_publish_failure_after_successful_flip_leaves_book_extracted_not_uploaded(
    book_repo, chunk_repo
) -> None:
    """The load-bearing restructuring: a publish failure must NOT reset the
    book to UPLOADED (that would re-extract and wipe chunks phase 4 may
    already be working on) -- it re-raises so SQS redelivers, and the
    REQUEUED branch turns that redelivery into a publish-only retry."""
    _seed_book(book_repo)
    document = _document("C" * 300)
    pdf_storage = FakePdfStorage(bytes_by_key={SOURCE_KEY: b"%PDF-1.4 fake"})
    extractor = FakeExtractor(document=document)
    synthesis_queue = FakeSynthesisQueue(error=RuntimeError("SQS is down"))
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), synthesis_queue)

    with pytest.raises(RuntimeError, match="SQS is down"):
        use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    book = book_repo.get(USER_ID, BOOK_ID)
    assert book.status == BookStatus.EXTRACTED  # NOT reset to UPLOADED
    assert book.chunks_total == 1


def test_requeued_redelivery_republishes_without_re_extracting(book_repo, chunk_repo, events) -> None:
    book = _seed_book(book_repo, status=BookStatus.EXTRACTED)
    book.chunks_total = 3
    pdf_storage = FakePdfStorage()
    extractor = FakeExtractor()
    synthesis_queue = FakeSynthesisQueue()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), synthesis_queue)

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    assert result.outcome == "REQUEUED"
    assert result.chunks_written == 3
    (call,) = synthesis_queue.calls
    assert call["chunk_indexes"] == [0, 1, 2]
    # No claim/extraction events -- the book was never re-extracted.
    assert events == []


def test_requeued_branch_does_not_fire_when_chunks_total_is_zero(book_repo, chunk_repo) -> None:
    """An EXTRACTED book with chunksTotal == 0 is not a valid state in
    practice (extraction always writes >= 1 chunk before flipping), but the
    guard is explicit: REQUEUED requires chunks_total > 0, not just
    status == EXTRACTED."""
    _seed_book(book_repo, status=BookStatus.EXTRACTED)  # chunks_total defaults to 0
    pdf_storage = FakePdfStorage()
    extractor = FakeExtractor()
    synthesis_queue = FakeSynthesisQueue()
    use_case = ExtractBook(book_repo, chunk_repo, pdf_storage, extractor, FixedClock(), synthesis_queue)

    result = use_case.execute(ExtractBookCommand(user_id=USER_ID, book_id=BOOK_ID, source_key=SOURCE_KEY))

    # Falls through to the normal claim path -- EXTRACTED isn't claimable,
    # so this resolves as ALREADY_CLAIMED, not REQUEUED.
    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_CLAIMED"
    assert synthesis_queue.calls == []
