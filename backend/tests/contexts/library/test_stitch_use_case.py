"""``StitchBook`` coverage (PLANS/phase-5.md §10.2).

The whole point of this file: no environment any automated check can reach
ever produces real audio (§3.1), so the *byte* path -- concatenation, offset
math, the manifest -- is carried entirely by unit tests over synthetic MPEG-2
frames. Everything here runs with zero AWS and zero network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.contexts.library.application.stitching import (
    DURATION_DRIFT_TOLERANCE_MS,
    StitchBook,
    StitchBookCommand,
)
from src.contexts.library.domain.book import Book
from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.value_objects import (
    BookStatus,
    ChunkStatus,
    StitchFailure,
)
from src.contexts.library.infrastructure.mp3 import mp3_duration_ms
from src.contexts.library.infrastructure.s3_keys import (
    book_audio_key,
    book_manifest_key,
    chunk_audio_key,
    chunk_marks_key,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.fakes import (
    RecordingObjectStorage,
    id3v2_header,
    mpeg2_frames,
    xing_frame,
)

USER_ID = "user-1"
BOOK_ID = "book-1"
FIXED_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)

BOOK_AUDIO_KEY = book_audio_key(USER_ID, BOOK_ID)
BOOK_MANIFEST_KEY = book_manifest_key(USER_ID, BOOK_ID)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class FakeBookRepository:
    """In-memory fake enforcing the same expected_statuses/ConflictError/
    NotFoundError semantics as the real DynamoDB adapter."""

    def __init__(self, book: Book | None = None, events: list[str] | None = None) -> None:
        self.book = book
        self.events = events if events is not None else []
        self.conflict_on: set[BookStatus] = set()
        self.not_found_on: set[BookStatus] = set()

    def save(self, book: Book) -> None:
        self.book = book

    def get(self, user_id: str, book_id: str) -> Book | None:
        if self.book is None or (user_id, book_id) != (self.book.user_id, self.book.id):
            return None
        return self.book

    def list_for_user(self, user_id): ...
    def delete(self, user_id, book_id): ...
    def increment_chunks_done(self, user_id, book_id, *, failed=False): ...

    def update_status(
        self,
        user_id,
        book_id,
        status,
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
        self.events.append(f"update_status:{status.value}")
        if failure_reason is not None and clear_failure_reason:
            raise ValueError("failure_reason and clear_failure_reason are mutually exclusive")
        if audio_key is not None and clear_stitch_outputs:
            raise ValueError("audio_key and clear_stitch_outputs are mutually exclusive")
        if status in self.not_found_on:
            raise NotFoundError("Book not found")
        if status in self.conflict_on:
            raise ConflictError("Book is not in an expected status")
        book = self.get(user_id, book_id)
        if book is None:
            raise NotFoundError("Book not found")
        if expected_statuses is not None and book.status not in expected_statuses:
            raise ConflictError("Book is not in an expected status")
        book.status = status
        if audio_key is not None:
            book.audio_key = audio_key
        if manifest_key is not None:
            book.manifest_key = manifest_key
        if audio_duration_ms is not None:
            book.audio_duration_ms = audio_duration_ms
        if failure_reason is not None:
            book.failure_reason = failure_reason
        if clear_failure_reason:
            book.failure_reason = None
        if clear_stitch_outputs:
            book.audio_key = None
            book.manifest_key = None
            book.audio_duration_ms = 0
        if updated_at is not None:
            book.updated_at = updated_at


class FakeChunkRepository:
    def __init__(self, chunks: list[Chunk] | None = None) -> None:
        self.chunks = chunks or []

    def save(self, chunk): ...
    def save_all(self, chunks): ...
    def get(self, book_id, index): ...
    def update_status(self, *args, **kwargs): ...
    def delete_for_book(self, book_id): ...

    def list_for_book(self, book_id: str) -> list[Chunk]:
        # Deliberately reversed: the use case must sort, not rely on the
        # adapter's ordering.
        return list(reversed([c for c in self.chunks if c.book_id == book_id]))


def _book(
    *,
    status: BookStatus = BookStatus.EXTRACTED,
    chunks_total: int = 2,
    chunks_done: int = 2,
    chunks_failed: int = 0,
) -> Book:
    book = Book.create(id=BOOK_ID, user_id=USER_ID, title_raw="A Book", now=FIXED_NOW)
    book.status = status
    book.chunks_total = chunks_total
    book.chunks_done = chunks_done
    book.chunks_failed = chunks_failed
    return book


def _chunk(
    index: int,
    *,
    status: ChunkStatus = ChunkStatus.DONE,
    duration_ms: int = 0,
    with_keys: bool = True,
) -> Chunk:
    return Chunk(
        book_id=BOOK_ID,
        index=index,
        user_id=USER_ID,
        text=f"chunk {index}",
        char_start=index * 100,
        char_end=(index + 1) * 100,
        audio_key=chunk_audio_key(USER_ID, BOOK_ID, index) if with_keys else None,
        marks_key=chunk_marks_key(USER_ID, BOOK_ID, index) if with_keys else None,
        status=status,
        duration_ms=duration_ms,
    )


def _build(book, chunks, *, audio_storage=None, marks_storage=None, events=None, max_attempts=3):
    events = events if events is not None else []
    audio_storage = audio_storage or RecordingObjectStorage(events=events, label="audio")
    marks_storage = marks_storage or RecordingObjectStorage(events=events, label="marks")
    book_repo = FakeBookRepository(book, events=events)
    use_case = StitchBook(
        book_repo,
        FakeChunkRepository(chunks),
        audio_storage,
        marks_storage,
        FixedClock(),
        max_attempts=max_attempts,
    )
    return use_case, book_repo, audio_storage, marks_storage, events


def _seed_audio(storage: RecordingObjectStorage, index: int, frame_count: int) -> bytes:
    """Store a realistic per-chunk MP3: an ID3v2 block + a Xing metadata
    frame in front of the real frames, exactly what an encoder emits."""
    frames = mpeg2_frames(frame_count)
    storage.objects[chunk_audio_key(USER_ID, BOOK_ID, index)] = (
        id3v2_header(16) + xing_frame() + frames
    )
    return frames


# --- happy path -------------------------------------------------------------


def test_two_done_chunks_stitch_to_ready() -> None:
    book = _book()
    chunks = [_chunk(0, duration_ms=72), _chunk(1, duration_ms=120)]
    use_case, book_repo, audio, marks, events = _build(book, chunks)
    frames_0 = _seed_audio(audio, 0, 3)
    frames_1 = _seed_audio(audio, 1, 5)

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "READY"
    assert result.reason is None
    assert result.segments == 2
    assert result.missing == 0
    assert book.status is BookStatus.READY
    assert book.audio_key == BOOK_AUDIO_KEY
    assert book.manifest_key == BOOK_MANIFEST_KEY
    assert book.failure_reason is None
    # The stitched object is EXACTLY the concatenation of the stripped
    # segments -- no ID3 block, no Xing frame, one clean frame stream.
    assert audio.objects[BOOK_AUDIO_KEY] == frames_0 + frames_1


def test_audio_duration_matches_the_sum_of_segment_durations() -> None:
    book = _book()
    use_case, _, audio, marks, _ = _build(book, [_chunk(0), _chunk(1)])
    _seed_audio(audio, 0, 3)
    _seed_audio(audio, 1, 5)

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert sum(s["d"] for s in document["segments"]) == document["durationMs"]
    assert book.audio_duration_ms == document["durationMs"]
    assert result.duration_ms == document["durationMs"]
    # 8 MPEG-2 24 kHz frames at 24 ms each.
    assert document["durationMs"] == mp3_duration_ms(audio.objects[BOOK_AUDIO_KEY]) == 192


def test_manifest_records_byte_ranges_and_sample_rate() -> None:
    book = _book()
    use_case, _, audio, marks, _ = _build(book, [_chunk(0), _chunk(1)])
    frames_0 = _seed_audio(audio, 0, 3)
    frames_1 = _seed_audio(audio, 1, 5)

    use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert document["sampleRateHz"] == 24000
    assert document["audioKey"] == BOOK_AUDIO_KEY
    assert [(s["b0"], s["b1"]) for s in document["segments"]] == [
        (0, len(frames_0)),
        (len(frames_0), len(frames_0) + len(frames_1)),
    ]
    assert [s["audioKey"] for s in document["segments"]] == [
        chunk_audio_key(USER_ID, BOOK_ID, 0),
        chunk_audio_key(USER_ID, BOOK_ID, 1),
    ]


def test_ordering_is_audio_then_manifest_then_dynamodb() -> None:
    """PLANS/phase-4.md §8.2 step 7's rule, at the book level: the book row is
    the only thing that ever advertises these keys, and it is written last, so
    a consumer that sees audioKey is guaranteed both objects exist."""
    book = _book()
    use_case, _, audio, _, events = _build(book, [_chunk(0)])
    _seed_audio(audio, 0, 3)

    use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert events == [
        "update_status:STITCHING",
        "write:audio",
        "write:marks",
        "update_status:READY",
    ]


def test_chunks_are_stitched_in_index_order_regardless_of_repository_order() -> None:
    book = _book(chunks_total=3, chunks_done=3)
    use_case, _, audio, marks, _ = _build(book, [_chunk(0), _chunk(1), _chunk(2)])
    _seed_audio(audio, 0, 1)
    _seed_audio(audio, 1, 2)
    _seed_audio(audio, 2, 3)

    use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert [s["i"] for s in document["segments"]] == [0, 1, 2]
    assert [s["d"] for s in document["segments"]] == [24, 48, 72]


# --- PARTIAL: some chunks failed --------------------------------------------


def test_mixed_done_and_failed_chunks_produce_partial_with_no_failure_reason() -> None:
    book = _book(chunks_total=2, chunks_done=2, chunks_failed=1)
    chunks = [_chunk(0), _chunk(1, status=ChunkStatus.FAILED, with_keys=False)]
    use_case, _, audio, marks, _ = _build(book, chunks)
    _seed_audio(audio, 0, 4)

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "PARTIAL"
    assert result.reason is None
    assert book.status is BookStatus.PARTIAL
    # failureReason is CLEARED: "some audio" is not a failure reason, and
    # chunksFailed already says exactly how much is missing.
    assert book.failure_reason is None
    assert book.audio_key == BOOK_AUDIO_KEY

    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert document["missing"] == [1]
    assert [s["i"] for s in document["segments"]] == [0]


def test_all_chunks_failed_produces_partial_no_audio_and_never_opens_a_multipart() -> None:
    """THE local/PR steady state (PLANS/phase-5.md §3.1/§3.2). Never FAILED:
    that status is re-claimable by ExtractBook and would wipe good text."""
    book = _book(chunks_total=2, chunks_done=2, chunks_failed=2)
    chunks = [
        _chunk(0, status=ChunkStatus.FAILED, with_keys=False),
        _chunk(1, status=ChunkStatus.FAILED, with_keys=False),
    ]
    use_case, _, audio, marks, _ = _build(book, chunks)

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "PARTIAL"
    assert result.reason == StitchFailure.NO_AUDIO.value
    assert book.status is BookStatus.PARTIAL
    assert book.failure_reason == StitchFailure.NO_AUDIO.value
    assert book.audio_key is None
    assert book.audio_duration_ms == 0
    assert audio.multipart_writers == []
    assert BOOK_AUDIO_KEY not in audio.objects

    # A real book.json is still written -- it is the load-bearing artefact
    # the smoke test asserts on.
    assert book.manifest_key == BOOK_MANIFEST_KEY
    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert document["segments"] == []
    assert document["missing"] == [0, 1]
    assert document["audioKey"] is None
    assert document["sampleRateHz"] is None
    assert document["status"] == "PARTIAL"


def test_a_done_chunk_whose_object_has_no_decodable_frames_is_treated_as_missing() -> None:
    book = _book()
    use_case, _, audio, marks, _ = _build(book, [_chunk(0), _chunk(1)])
    _seed_audio(audio, 0, 3)
    audio.objects[chunk_audio_key(USER_ID, BOOK_ID, 1)] = b"not an mp3 at all"

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "PARTIAL"
    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert document["missing"] == [1]
    assert [s["i"] for s in document["segments"]] == [0]


def test_duration_drift_beyond_the_tolerance_is_logged_but_not_fatal(caplog) -> None:
    book = _book(chunks_total=1, chunks_done=1)
    # 3 frames == 72 ms measured; the stored value is deliberately far off.
    use_case, _, audio, _, _ = _build(
        book, [_chunk(0, duration_ms=72 + DURATION_DRIFT_TOLERANCE_MS + 500)]
    )
    _seed_audio(audio, 0, 3)

    with caplog.at_level("WARNING"):
        result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "READY"
    assert "duration drift" in caplog.text


def test_a_sample_rate_mismatch_is_logged_but_not_fatal(caplog) -> None:
    book = _book()
    use_case, _, audio, marks, _ = _build(book, [_chunk(0), _chunk(1)])
    audio.objects[chunk_audio_key(USER_ID, BOOK_ID, 0)] = mpeg2_frames(2, sample_rate_idx=1)
    audio.objects[chunk_audio_key(USER_ID, BOOK_ID, 1)] = mpeg2_frames(2, sample_rate_idx=0)

    with caplog.at_level("WARNING"):
        result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "READY"
    assert "sample rate" in caplog.text
    # Recorded in the artefact so a mixed-rate book is diagnosable from S3,
    # not only from CloudWatch.
    assert json.loads(marks.objects[BOOK_MANIFEST_KEY])["sampleRateHz"] == 24000


# --- SKIPPED / DEFERRED branches --------------------------------------------


def test_missing_book_is_skipped() -> None:
    use_case, _, _, _, _ = _build(None, [])
    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))
    assert (result.outcome, result.reason) == ("SKIPPED", "BOOK_NOT_FOUND")


@pytest.mark.parametrize("status", [BookStatus.READY, BookStatus.PARTIAL, BookStatus.FAILED])
def test_an_already_terminal_book_is_skipped(status: BookStatus) -> None:
    """Duplicate stitch messages are harmless by construction -- this is the
    branch that makes §4.3's re-publish safe."""
    use_case, _, audio, marks, _ = _build(_book(status=status), [_chunk(0)])

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert (result.outcome, result.reason) == ("SKIPPED", "ALREADY_STITCHED")
    assert marks.put_calls == []


def test_incomplete_counters_are_deferred() -> None:
    use_case, _, _, _, events = _build(_book(chunks_total=5, chunks_done=2), [_chunk(0)])

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert (result.outcome, result.reason) == ("DEFERRED", "NOT_COMPLETE")
    assert events == []  # never even claimed


def test_zero_chunks_total_is_deferred() -> None:
    use_case, _, _, _, _ = _build(_book(chunks_total=0, chunks_done=0), [])
    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))
    assert (result.outcome, result.reason) == ("DEFERRED", "NOT_COMPLETE")


def test_claim_conflict_is_skipped_already_claimed() -> None:
    book = _book()
    use_case, book_repo, _, marks, _ = _build(book, [_chunk(0)])
    book_repo.conflict_on = {BookStatus.STITCHING}

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert (result.outcome, result.reason) == ("SKIPPED", "ALREADY_CLAIMED")
    assert marks.put_calls == []


def test_claim_not_found_is_skipped_book_not_found() -> None:
    book = _book()
    use_case, book_repo, _, _, _ = _build(book, [_chunk(0)])
    book_repo.not_found_on = {BookStatus.STITCHING}

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert (result.outcome, result.reason) == ("SKIPPED", "BOOK_NOT_FOUND")


def test_a_book_in_stitching_can_be_reclaimed() -> None:
    """A stitcher that died hard leaves STITCHING with nothing to release it
    and no lease to expire -- excluding it would wedge the book permanently
    (PLANS/phase-5.md §6.4)."""
    book = _book(status=BookStatus.STITCHING)
    use_case, _, audio, _, _ = _build(book, [_chunk(0)])
    _seed_audio(audio, 0, 2)

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert result.outcome == "READY"


# --- transient failure: claim released, exception re-raised -----------------


class _BoomStorage(RecordingObjectStorage):
    def get_bytes(self, *, key: str) -> bytes:
        raise RuntimeError("S3 is down")


def test_transient_failure_releases_the_claim_to_extracted_and_reraises() -> None:
    book = _book(chunks_total=1, chunks_done=1)
    events: list[str] = []
    use_case, _, _, _, _ = _build(
        book, [_chunk(0)], audio_storage=_BoomStorage(events=events, label="audio"), events=events
    )

    with pytest.raises(RuntimeError, match="S3 is down"):
        use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID, attempt=1))

    assert book.status is BookStatus.EXTRACTED
    assert events == ["update_status:STITCHING", "update_status:EXTRACTED"]


def test_claim_release_losing_a_race_is_logged_not_raised(caplog) -> None:
    book = _book(chunks_total=1, chunks_done=1)
    events: list[str] = []
    use_case, book_repo, _, _, _ = _build(
        book, [_chunk(0)], audio_storage=_BoomStorage(events=events, label="audio"), events=events
    )
    book_repo.conflict_on = {BookStatus.EXTRACTED}

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID, attempt=1))

    assert "could not release the stitch claim" in caplog.text


def test_last_attempt_degrades_to_partial_stitch_failed_with_per_chunk_segments() -> None:
    """No book.mp3, but the manifest still lists every DONE chunk with its own
    audioKey and a t derived from the stored durationMs -- so phase 6 can play
    the book chunk by chunk (§1 invariant 3)."""
    book = _book(chunks_total=3, chunks_done=3, chunks_failed=1)
    chunks = [
        _chunk(0, duration_ms=1500),
        _chunk(1, duration_ms=2500),
        _chunk(2, status=ChunkStatus.FAILED, with_keys=False),
    ]
    use_case, _, _, marks, _ = _build(
        book, chunks, audio_storage=_BoomStorage(label="audio"), max_attempts=3
    )

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID, attempt=3))

    assert result.outcome == "PARTIAL"
    assert result.reason == StitchFailure.STITCH_FAILED.value
    assert book.status is BookStatus.PARTIAL
    assert book.failure_reason == StitchFailure.STITCH_FAILED.value
    assert book.audio_key is None

    document = json.loads(marks.objects[BOOK_MANIFEST_KEY])
    assert document["audioKey"] is None
    assert document["missing"] == [2]
    assert [(s["i"], s["t"], s["d"]) for s in document["segments"]] == [(0, 0, 1500), (1, 1500, 2500)]
    assert all("b0" not in s and "b1" not in s for s in document["segments"])
    assert document["durationMs"] == 4000


def test_last_attempt_with_an_unwritable_manifest_still_reaches_a_terminal_status(caplog) -> None:
    """Raising here would DLQ the message and leave the book stuck in
    STITCHING, which is strictly worse than a terminal book with no
    manifest."""

    class _BoomMarks(RecordingObjectStorage):
        def put_bytes(self, *, key, data, content_type):
            raise RuntimeError("marks bucket is down")

    book = _book(chunks_total=1, chunks_done=1)
    use_case, _, _, _, _ = _build(
        book,
        [_chunk(0, duration_ms=1000)],
        audio_storage=_BoomStorage(label="audio"),
        marks_storage=_BoomMarks(label="marks"),
    )

    with caplog.at_level("ERROR"):
        result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID, attempt=3))

    assert result.outcome == "PARTIAL"
    assert result.manifest_key is None
    assert book.status is BookStatus.PARTIAL


def test_a_manifest_write_failure_on_a_normal_attempt_propagates() -> None:
    class _BoomMarks(RecordingObjectStorage):
        def put_bytes(self, *, key, data, content_type):
            raise RuntimeError("marks bucket is down")

    book = _book(chunks_total=1, chunks_done=1)
    use_case, _, audio, _, _ = _build(
        book, [_chunk(0)], marks_storage=_BoomMarks(label="marks"), max_attempts=3
    )
    _seed_audio(audio, 0, 2)

    with pytest.raises(RuntimeError, match="marks bucket is down"):
        use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID, attempt=1))


def test_a_failed_multipart_leaves_no_half_written_book_audio() -> None:
    class _HalfBoomStorage(RecordingObjectStorage):
        def get_bytes(self, *, key: str) -> bytes:
            if key.endswith("000001.mp3"):
                raise RuntimeError("S3 is down")
            return super().get_bytes(key=key)

    book = _book()
    audio = _HalfBoomStorage(label="audio")
    use_case, _, _, _, _ = _build(book, [_chunk(0), _chunk(1)], audio_storage=audio)
    _seed_audio(audio, 0, 3)

    with pytest.raises(RuntimeError):
        use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID, attempt=1))

    assert BOOK_AUDIO_KEY not in audio.objects
    assert audio.multipart_writers[0].aborted is True


# --- the terminal transition is the exactly-once gate -----------------------


def test_terminal_conflict_is_skipped_already_stitched() -> None:
    book = _book(chunks_total=1, chunks_done=1)
    use_case, book_repo, audio, _, _ = _build(book, [_chunk(0)])
    _seed_audio(audio, 0, 2)
    book_repo.conflict_on = {BookStatus.READY}

    result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert (result.outcome, result.reason) == ("SKIPPED", "ALREADY_STITCHED")


def test_book_deleted_mid_stitch_is_logged_and_skipped(caplog) -> None:
    book = _book(chunks_total=1, chunks_done=1)
    use_case, book_repo, audio, _, _ = _build(book, [_chunk(0)])
    _seed_audio(audio, 0, 2)
    book_repo.not_found_on = {BookStatus.READY}

    with caplog.at_level("WARNING"):
        result = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert (result.outcome, result.reason) == ("SKIPPED", "BOOK_NOT_FOUND")
    assert "deleted mid-stitch" in caplog.text


def test_double_delivery_produces_one_ready_and_one_skipped_with_identical_bytes() -> None:
    book = _book()
    use_case, _, audio, marks, _ = _build(book, [_chunk(0), _chunk(1)])
    _seed_audio(audio, 0, 3)
    _seed_audio(audio, 1, 5)

    first = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))
    stitched = audio.objects[BOOK_AUDIO_KEY]
    manifest = marks.objects[BOOK_MANIFEST_KEY]

    second = use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert first.outcome == "READY"
    assert (second.outcome, second.reason) == ("SKIPPED", "ALREADY_STITCHED")
    # Every S3 key is a pure function of (user_id, book_id), so a duplicate
    # overwrites with byte-identical content and there is nothing to undo.
    assert audio.objects[BOOK_AUDIO_KEY] == stitched
    assert marks.objects[BOOK_MANIFEST_KEY] == manifest


def test_a_slow_stitch_logs_a_warning(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """PLANS/phase-5.md OQ-5: the 900 s budget is estimated, never measured,
    so the margin has to be observable in prod before it becomes an
    incident."""
    import src.contexts.library.application.stitching as module

    ticks = iter([0.0, 10_000.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))

    book = _book(chunks_total=1, chunks_done=1)
    use_case, _, audio, _, _ = _build(book, [_chunk(0)])
    _seed_audio(audio, 0, 2)

    with caplog.at_level("WARNING"):
        use_case.execute(StitchBookCommand(user_id=USER_ID, book_id=BOOK_ID))

    assert "approaching the 900s Lambda ceiling" in caplog.text
