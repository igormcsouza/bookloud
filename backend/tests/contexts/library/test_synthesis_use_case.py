from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.contexts.library.application.synthesis import SynthesizeChunk, SynthesizeChunkCommand
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.repository import ChunkCounters
from src.contexts.library.domain.synthesis import SynthesisUnavailable, SynthesizedAudio, UnsynthesizableText
from src.contexts.library.domain.value_objects import (
    NON_TERMINAL_CHUNK_STATUSES,
    BookStatus,
    ChunkStatus,
    MarksTiming,
    SynthesisFailure,
    SynthesisSource,
)
from src.contexts.library.infrastructure.s3_keys import chunk_audio_key, chunk_marks_key
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.fakes import FakeStitchQueue, FakeSynthesizer, RecordingObjectStorage

USER_ID = "user-1"
BOOK_ID = "book-1"
CHUNK_INDEX = 7
FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


class FakeChunkRepository:
    """In-memory fake enforcing the same expected_statuses/ConflictError/
    NotFoundError semantics as the real DynamoDB adapter."""

    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events if events is not None else []
        self._store: dict[tuple[str, int], Chunk] = {}

    def seed(self, chunk: Chunk) -> None:
        self._store[(chunk.book_id, chunk.index)] = chunk

    def save(self, chunk: Chunk) -> None:
        self._store[(chunk.book_id, chunk.index)] = chunk

    def save_all(self, chunks) -> None:
        for chunk in chunks:
            self.save(chunk)

    def get(self, book_id: str, index: int) -> Chunk | None:
        return self._store.get((book_id, index))

    def list_for_book(self, book_id: str) -> list[Chunk]:
        return [c for (b, _), c in self._store.items() if b == book_id]

    def update_status(
        self,
        book_id,
        index,
        status,
        *,
        expected_statuses=None,
        audio_key=None,
        marks_key=None,
        duration_ms=None,
        synthesis_source=None,
        failure_reason=None,
        clear_failure_reason=False,
    ) -> None:
        self._events.append(f"update_status:{status.value}")
        if failure_reason is not None and clear_failure_reason:
            raise ValueError("failure_reason and clear_failure_reason are mutually exclusive")
        chunk = self._store.get((book_id, index))
        if chunk is None:
            raise NotFoundError("Chunk not found")
        if expected_statuses is not None and chunk.status not in expected_statuses:
            raise ConflictError("Chunk is not in an expected status")
        chunk.status = status
        if audio_key is not None:
            chunk.audio_key = audio_key
        if marks_key is not None:
            chunk.marks_key = marks_key
        if duration_ms is not None:
            chunk.duration_ms = duration_ms
        if synthesis_source is not None:
            chunk.synthesis_source = synthesis_source
        if failure_reason is not None:
            chunk.failure_reason = failure_reason
        if clear_failure_reason:
            chunk.failure_reason = None

    def delete_for_book(self, book_id: str) -> int:
        keys = [k for k in self._store if k[0] == book_id]
        for k in keys:
            del self._store[k]
        return len(keys)


class FakeBookRepository:
    """In-memory fake for BookRepository.increment_chunks_done -- the
    counter-correctness behaviour the use case depends on."""

    def __init__(
        self,
        *,
        chunks_total: int = 1,
        events: list[str] | None = None,
        missing: bool = False,
        book: Book | None = None,
    ) -> None:
        self._events = events if events is not None else []
        self.chunks_done = 0
        self.chunks_total = chunks_total
        self.chunks_failed = 0
        self._missing = missing
        # Only the STITCH_REQUEUED branch calls `get`; every other test
        # leaves this None so the branch is a no-op.
        self.book = book

    def save(self, book) -> None: ...
    def get(self, user_id, book_id):
        return self.book

    def list_for_user(self, user_id): ...
    def delete(self, user_id, book_id): ...
    def update_status(self, *args, **kwargs) -> None: ...

    def increment_chunks_done(self, user_id: str, book_id: str, *, failed: bool = False) -> ChunkCounters:
        self._events.append(f"increment_chunks_done:failed={failed}")
        if self._missing:
            raise NotFoundError("Book not found")
        self.chunks_done += 1
        if failed:
            self.chunks_failed += 1
        return ChunkCounters(
            chunks_done=self.chunks_done, chunks_total=self.chunks_total, chunks_failed=self.chunks_failed
        )


def _chunk(*, text: str = "Some chunk text.", status: ChunkStatus = ChunkStatus.PENDING, user_id: str = USER_ID) -> Chunk:
    return Chunk(
        book_id=BOOK_ID,
        index=CHUNK_INDEX,
        user_id=user_id,
        text=text,
        char_start=0,
        char_end=len(text),
        audio_key=None,
        marks_key=None,
        status=status,
    )


def _audio(*, duration_ms: int = 1000, source: SynthesisSource = SynthesisSource.EDGE_TTS) -> SynthesizedAudio:
    return SynthesizedAudio(
        audio=b"fake-mp3-bytes",
        content_type="audio/mpeg",
        duration_ms=duration_ms,
        marks=(),
        voice="en-US-AriaNeural",
        source=source,
        timing=MarksTiming.MEASURED,
    )


@pytest.fixture
def chunk_repo() -> FakeChunkRepository:
    return FakeChunkRepository()


@pytest.fixture
def book_repo() -> FakeBookRepository:
    return FakeBookRepository()


@pytest.fixture
def audio_storage() -> RecordingObjectStorage:
    return RecordingObjectStorage()


@pytest.fixture
def marks_storage() -> RecordingObjectStorage:
    return RecordingObjectStorage()


@pytest.fixture
def stitch_queue() -> FakeStitchQueue:
    return FakeStitchQueue()


def _use_case(
    book_repo,
    chunk_repo,
    synthesizer,
    audio_storage,
    marks_storage,
    max_attempts=5,
    stitch_queue=None,
) -> SynthesizeChunk:
    return SynthesizeChunk(
        book_repo,
        chunk_repo,
        synthesizer,
        audio_storage,
        marks_storage,
        stitch_queue if stitch_queue is not None else FakeStitchQueue(),
        max_attempts=max_attempts,
    )


def _book(*, status: BookStatus = BookStatus.EXTRACTED, chunks_total: int = 1, chunks_done: int = 1) -> Book:
    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="A Book", now=FIXED_NOW)
    book.status = status
    book.chunks_total = chunks_total
    book.chunks_done = chunks_done
    return book


# --- happy path ------------------------------------------------------------


def test_happy_path_writes_audio_then_marks_then_flips_done(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "DONE"
    assert result.duration_ms == 1000
    assert result.source == "edge-tts"
    assert result.chunks_done == 1
    assert result.chunks_total == 1

    expected_audio_key = chunk_audio_key(USER_ID, BOOK_ID, CHUNK_INDEX)
    expected_marks_key = chunk_marks_key(USER_ID, BOOK_ID, CHUNK_INDEX)
    # Ordering: audio before marks.
    assert audio_storage.put_calls == [expected_audio_key]
    assert marks_storage.put_calls == [expected_marks_key]

    chunk = chunk_repo.get(BOOK_ID, CHUNK_INDEX)
    assert chunk.status == ChunkStatus.DONE
    assert chunk.audio_key == expected_audio_key
    assert chunk.marks_key == expected_marks_key
    assert chunk.duration_ms == 1000
    assert chunk.synthesis_source == "edge-tts"

    marks_document = json.loads(marks_storage.objects[expected_marks_key])
    assert marks_document["audioKey"] == expected_audio_key
    assert marks_document["chunkIndex"] == CHUNK_INDEX


def test_happy_path_clears_previous_failure_reason(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk = _chunk(status=ChunkStatus.FAILED)
    chunk.failure_reason = "ALL_ENGINES_FAILED"
    chunk_repo.seed(chunk)
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert chunk_repo.get(BOOK_ID, CHUNK_INDEX).failure_reason is None


# --- SKIPPED branches --------------------------------------------------------


def test_chunk_not_found_is_skipped(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"
    assert synthesizer.calls == []


def test_already_done_is_skipped(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.DONE))
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_DONE"
    assert synthesizer.calls == []


def test_user_mismatch_is_skipped(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk(user_id="someone-else"))
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "USER_MISMATCH"
    assert synthesizer.calls == []


def test_claim_conflict_is_skipped_already_done(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.PENDING))
    real_update_status = chunk_repo.update_status

    def racing_update_status(book_id, index, status, **kwargs):
        if status == ChunkStatus.SYNTHESIZING:
            raise ConflictError("raced")
        return real_update_status(book_id, index, status, **kwargs)

    chunk_repo.update_status = racing_update_status  # type: ignore[method-assign]
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_DONE"
    assert synthesizer.calls == []


# --- permanent branches: EMPTY_TEXT / TEXT_TOO_LONG -------------------------


def test_empty_text_fails_permanently_without_engine_call(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk(text="   "))
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "FAILED"
    assert result.reason == SynthesisFailure.EMPTY_TEXT.value
    assert synthesizer.calls == []
    chunk = chunk_repo.get(BOOK_ID, CHUNK_INDEX)
    assert chunk.status == ChunkStatus.FAILED
    assert chunk.failure_reason == SynthesisFailure.EMPTY_TEXT.value
    assert book_repo.chunks_done == 1
    assert book_repo.chunks_failed == 1


def test_text_too_long_fails_permanently(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk(text="x" * 4001))
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "FAILED"
    assert result.reason == SynthesisFailure.TEXT_TOO_LONG.value
    assert synthesizer.calls == []


def test_engine_raises_unsynthesizable_text_fails_permanently(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(error=UnsynthesizableText(SynthesisFailure.EMPTY_TEXT, "empty"))
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "FAILED"
    assert result.reason == SynthesisFailure.EMPTY_TEXT.value


# --- transient: released to PENDING, then retried ---------------------------


def test_transient_engine_failure_releases_claim_and_reraises(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(error=SynthesisUnavailable("both engines failed"))
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage, max_attempts=5)

    with pytest.raises(SynthesisUnavailable):
        use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX, attempt=1))

    chunk = chunk_repo.get(BOOK_ID, CHUNK_INDEX)
    assert chunk.status == ChunkStatus.PENDING
    assert book_repo.chunks_done == 0  # not counted -- still retryable


def test_transient_storage_failure_releases_claim_and_reraises(chunk_repo, book_repo, marks_storage) -> None:
    class BoomStorage:
        def put_bytes(self, *, key, data, content_type):
            raise RuntimeError("S3 is down")

        def get_bytes(self, *, key):
            raise NotImplementedError

    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, BoomStorage(), marks_storage, max_attempts=5)

    with pytest.raises(RuntimeError):
        use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX, attempt=2))

    assert chunk_repo.get(BOOK_ID, CHUNK_INDEX).status == ChunkStatus.PENDING


# --- last-attempt conversion: transient -> permanent -----------------------


def test_last_attempt_both_engines_failed_converts_to_failed_all_engines(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(error=SynthesisUnavailable("both down"))
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage, max_attempts=5)

    result = use_case.execute(
        SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX, attempt=5)
    )

    assert result.outcome == "FAILED"
    assert result.reason == SynthesisFailure.ALL_ENGINES_FAILED.value
    chunk = chunk_repo.get(BOOK_ID, CHUNK_INDEX)
    assert chunk.status == ChunkStatus.FAILED
    assert chunk.failure_reason == SynthesisFailure.ALL_ENGINES_FAILED.value
    assert book_repo.chunks_done == 1
    assert book_repo.chunks_failed == 1


def test_last_attempt_unexpected_bug_converts_to_failed_unknown(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(error=ValueError("a genuine bug"))
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage, max_attempts=5)

    result = use_case.execute(
        SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX, attempt=5)
    )

    assert result.outcome == "FAILED"
    assert result.reason == SynthesisFailure.UNKNOWN.value


def test_transient_release_race_lost_to_concurrent_done_is_skipped(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    """A concurrent duplicate invocation reaches DONE while this one is
    about to release its claim to PENDING on a transient failure -- the
    conditional release must lose gracefully, not clobber the DONE status."""
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(error=SynthesisUnavailable("boom"))
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage, max_attempts=5)

    real_update_status = chunk_repo.update_status

    def racing_update_status(book_id, index, status, **kwargs):
        if status == ChunkStatus.PENDING:
            # Simulate a concurrent invocation finishing first.
            chunk_repo._store[(book_id, index)].status = ChunkStatus.DONE
        return real_update_status(book_id, index, status, **kwargs)

    chunk_repo.update_status = racing_update_status  # type: ignore[method-assign]

    result = use_case.execute(
        SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX, attempt=1)
    )

    assert result.outcome == "SKIPPED"
    assert result.reason == "ALREADY_DONE"


# --- exactly-once counter: double delivery ----------------------------------


def test_double_delivery_increments_chunks_done_exactly_once(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    first = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))
    second = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert first.outcome == "DONE"
    assert second.outcome == "SKIPPED"
    assert second.reason == "ALREADY_DONE"
    assert book_repo.chunks_done == 1  # NOT 2


def test_claim_not_found_race_is_skipped_chunk_not_found(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    """A chunk deleted between get() and the claim attempt -- update_status
    itself raising NotFoundError must resolve to SKIPPED, not propagate."""
    chunk_repo.seed(_chunk())
    real_update_status = chunk_repo.update_status

    def racing_update_status(book_id, index, status, **kwargs):
        if status == ChunkStatus.SYNTHESIZING:
            del chunk_repo._store[(book_id, index)]
        return real_update_status(book_id, index, status, **kwargs)

    chunk_repo.update_status = racing_update_status  # type: ignore[method-assign]
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"


def test_transient_release_not_found_is_skipped_chunk_not_found(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    synthesizer = FakeSynthesizer(error=SynthesisUnavailable("boom"))
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage, max_attempts=5)

    real_update_status = chunk_repo.update_status

    def racing_update_status(book_id, index, status, **kwargs):
        if status == ChunkStatus.PENDING:
            del chunk_repo._store[(book_id, index)]
        return real_update_status(book_id, index, status, **kwargs)

    chunk_repo.update_status = racing_update_status  # type: ignore[method-assign]

    result = use_case.execute(
        SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX, attempt=1)
    )

    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"


def test_finish_failed_not_found_race_is_skipped(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk(text="   "))  # EMPTY_TEXT -> goes straight to _finish_failed
    real_update_status = chunk_repo.update_status

    def racing_update_status(book_id, index, status, **kwargs):
        if status == ChunkStatus.FAILED:
            del chunk_repo._store[(book_id, index)]
        return real_update_status(book_id, index, status, **kwargs)

    chunk_repo.update_status = racing_update_status  # type: ignore[method-assign]
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"


def test_finish_done_not_found_race_is_skipped(chunk_repo, book_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    real_update_status = chunk_repo.update_status

    def racing_update_status(book_id, index, status, **kwargs):
        if status == ChunkStatus.DONE:
            del chunk_repo._store[(book_id, index)]
        return real_update_status(book_id, index, status, **kwargs)

    chunk_repo.update_status = racing_update_status  # type: ignore[method-assign]
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "CHUNK_NOT_FOUND"


def test_book_not_found_during_increment_is_handled_gracefully(chunk_repo, audio_storage, marks_storage) -> None:
    chunk_repo.seed(_chunk())
    book_repo = FakeBookRepository(missing=True)
    synthesizer = FakeSynthesizer(result=_audio())
    use_case = _use_case(book_repo, chunk_repo, synthesizer, audio_storage, marks_storage)

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "DONE"  # the chunk itself did complete
    assert result.chunks_done == 0  # counters unavailable, defaulted


# --- the fan-in edge: publishing the stitch (PLANS/phase-5.md §4.2) ---------


def test_completing_done_increment_publishes_exactly_one_stitch_message(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk())
    book_repo = FakeBookRepository(chunks_total=1)
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert stitch_queue.calls == [{"user_id": USER_ID, "book_id": BOOK_ID}]


def test_completing_failed_increment_also_publishes(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    """THE local/PR path (PLANS/phase-5.md §4.2): with a StubSynthesizer every
    chunk fails, so the completing increment is always a *failed* one.
    Publishing only from the success path would mean the stitcher never fires
    anywhere CI can see it."""
    chunk_repo.seed(_chunk(text="   "))  # EMPTY_TEXT -> _finish_failed
    book_repo = FakeBookRepository(chunks_total=1)
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "FAILED"
    assert stitch_queue.calls == [{"user_id": USER_ID, "book_id": BOOK_ID}]


def test_non_final_increment_publishes_nothing(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk())
    book_repo = FakeBookRepository(chunks_total=3)
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert stitch_queue.calls == []


def test_missing_book_during_increment_publishes_nothing(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk())
    book_repo = FakeBookRepository(missing=True)
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert stitch_queue.calls == []


def test_stitch_publish_failure_propagates_so_sqs_redelivers(
    chunk_repo, audio_storage, marks_storage
) -> None:
    """Deliberately NOT swallowed: the SQS message stays undeleted and the
    redelivery hits the STITCH_REQUEUED branch."""
    chunk_repo.seed(_chunk())
    book_repo = FakeBookRepository(chunks_total=1)
    queue = FakeStitchQueue(error=RuntimeError("SQS is down"))
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=queue,
    )

    with pytest.raises(RuntimeError):
        use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    # The chunk itself is DONE and counted -- only the publish failed.
    assert chunk_repo.get(BOOK_ID, CHUNK_INDEX).status == ChunkStatus.DONE
    assert book_repo.chunks_done == 1


# --- STITCH_REQUEUED re-entrancy (PLANS/phase-5.md §4.3) ---------------------


def test_redelivered_done_chunk_republishes_stitch_for_complete_extracted_book(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(book=_book(status=BookStatus.EXTRACTED, chunks_total=1, chunks_done=1))
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.outcome == "SKIPPED"
    assert result.reason == "STITCH_REQUEUED"
    assert stitch_queue.calls == [{"user_id": USER_ID, "book_id": BOOK_ID}]


@pytest.mark.parametrize(
    "status", [BookStatus.STITCHING, BookStatus.READY, BookStatus.PARTIAL, BookStatus.UPLOADED]
)
def test_redelivered_done_chunk_does_not_republish_for_non_extracted_book(
    chunk_repo, audio_storage, marks_storage, stitch_queue, status: BookStatus
) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(book=_book(status=status, chunks_total=1, chunks_done=1))
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.reason == "ALREADY_DONE"
    assert stitch_queue.calls == []


def test_redelivered_done_chunk_does_not_republish_for_incomplete_book(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(book=_book(chunks_total=5, chunks_done=2))
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    result = use_case.execute(SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX))

    assert result.reason == "ALREADY_DONE"
    assert stitch_queue.calls == []


def test_redelivered_done_chunk_with_zero_chunks_total_does_not_republish(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(book=_book(chunks_total=0, chunks_done=0))
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    assert use_case.execute(
        SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX)
    ).reason == "ALREADY_DONE"
    assert stitch_queue.calls == []


def test_redelivered_done_chunk_with_deleted_book_does_not_republish(
    chunk_repo, audio_storage, marks_storage, stitch_queue
) -> None:
    chunk_repo.seed(_chunk(status=ChunkStatus.DONE))
    book_repo = FakeBookRepository(book=None)
    use_case = _use_case(
        book_repo, chunk_repo, FakeSynthesizer(result=_audio()), audio_storage, marks_storage,
        stitch_queue=stitch_queue,
    )

    assert use_case.execute(
        SynthesizeChunkCommand(user_id=USER_ID, book_id=BOOK_ID, chunk_index=CHUNK_INDEX)
    ).reason == "ALREADY_DONE"
    assert stitch_queue.calls == []
