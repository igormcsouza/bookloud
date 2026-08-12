"""``ResynthesizeBook`` (PLANS/phase-6.md §5, §13.2).

The two tests that matter most here are not about the happy path:

- ``test_status_tuples_unchanged`` is named after PLANS/phase-5.md OQ-6. The
  tempting "fix" for the ``PARTIAL`` dead end is to add ``PARTIAL`` to
  ``_CLAIMABLE_STATUSES``/``_REISSUABLE_STATUSES``, which reintroduces
  exactly the hazard ``PARTIAL`` was invented to prevent -- a redelivered S3
  event wiping a book's perfectly good text because its *audio* failed. This
  test makes that shortcut fail a unit test before it can fail a book.
- ``test_stale_stitch_message_is_deferred_after_resynthesize`` runs the real
  ``StitchBook`` against the real post-resynthesis state, because §5.4's
  safety argument is a claim about *another module's* guard.
"""

from __future__ import annotations

import pytest

from src.contexts.library.application.resynthesis import ResynthesizeBook
from src.contexts.library.application.stitching import StitchBook, StitchBookCommand
from src.contexts.library.domain.value_objects import (
    BookStatus,
    ChunkStatus,
    StitchFailure,
    SynthesisFailure,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError
from tests.contexts.library.conftest import seed_book, seed_chunks
from tests.contexts.library.fakes import (
    FakeStitchQueue,
    FakeSynthesisQueue,
    RecordingObjectStorage,
)

USER = "user-1"
OTHER_USER = "user-2"
BOOK = "book-1"
TOTAL = 5


def _use_case(book_repo, chunk_repo, fixed_clock, *, synthesis=None, stitch=None):
    return ResynthesizeBook(
        book_repo,
        chunk_repo,
        synthesis or FakeSynthesisQueue(),
        stitch or FakeStitchQueue(),
        fixed_clock,
    )


def _seed_partial_book(
    book_repo,
    chunk_repo,
    *,
    failed_indexes: tuple[int, ...] = (1, 3),
    failure_reason: str | None = StitchFailure.NO_AUDIO.value,
):
    """A ``PARTIAL`` book with ``TOTAL`` chunks: ``failed_indexes`` FAILED,
    every other chunk DONE. Counters mirror what the pipeline would have
    left behind -- ``chunks_done == chunks_total`` (a FAILED chunk still
    increments; phase-4 Q6) and ``chunks_failed == len(failed_indexes)``."""
    seed_book(book_repo, id=BOOK, user_id=USER)
    seed_chunks(chunk_repo, book_id=BOOK, user_id=USER, count=TOTAL)
    for index in range(TOTAL):
        if index in failed_indexes:
            chunk_repo.update_status(
                BOOK,
                index,
                ChunkStatus.FAILED,
                failure_reason=SynthesisFailure.EXTERNAL_TTS_DISABLED.value,
            )
        else:
            chunk_repo.update_status(
                BOOK,
                index,
                ChunkStatus.DONE,
                audio_key=f"audio/{USER}/{BOOK}/{index:06d}.mp3",
                marks_key=f"marks/{USER}/{BOOK}/{index:06d}.json",
                duration_ms=1000,
            )
    book_repo.update_status(
        USER,
        BOOK,
        BookStatus.PARTIAL,
        chunks_total=TOTAL,
        chunks_done=TOTAL,
        chunks_failed=len(failed_indexes),
        audio_key=f"audio/{USER}/{BOOK}/book.mp3" if failure_reason != "NO_AUDIO" else None,
        manifest_key=f"marks/{USER}/{BOOK}/book.json",
        audio_duration_ms=3000,
        failure_reason=failure_reason,
    )
    return book_repo.get(USER, BOOK)


# --- the happy path --------------------------------------------------------


def test_resets_only_failed_chunks_to_pending(book_repo, chunk_repo, fixed_clock) -> None:
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))

    result = _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, BOOK)

    assert result.retried_chunks == 2
    assert result.republished_stitch is False
    statuses = {c.index: c.status for c in chunk_repo.list_for_book(BOOK)}
    assert statuses == {
        0: ChunkStatus.DONE,
        1: ChunkStatus.PENDING,
        2: ChunkStatus.DONE,
        3: ChunkStatus.PENDING,
        4: ChunkStatus.DONE,
    }


def test_reset_chunks_have_their_failure_reason_cleared(
    book_repo, chunk_repo, fixed_clock
) -> None:
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1,))

    _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, BOOK)

    reset = next(c for c in chunk_repo.list_for_book(BOOK) if c.index == 1)
    assert reset.failure_reason is None


def test_publishes_the_fan_out_for_exactly_those_indexes_once(
    book_repo, chunk_repo, fixed_clock
) -> None:
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))
    synthesis = FakeSynthesisQueue()

    _use_case(book_repo, chunk_repo, fixed_clock, synthesis=synthesis).execute(USER, BOOK)

    assert synthesis.calls == [{"user_id": USER, "book_id": BOOK, "chunk_indexes": [1, 3]}]


def test_book_update_rewinds_counters_and_clears_stitch_outputs(
    book_repo, chunk_repo, fixed_clock
) -> None:
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))

    _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, BOOK)

    book = book_repo.get(USER, BOOK)
    assert book.status is BookStatus.EXTRACTED
    assert book.chunks_total == TOTAL
    assert book.chunks_done == TOTAL - 2
    assert book.chunks_failed == 0
    assert book.failure_reason is None
    # Not optional: a stale audioKey would let the reader play a book.mp3
    # assembled from the *old* chunk set against the *new* manifest.
    assert book.audio_key is None
    assert book.manifest_key is None
    assert book.audio_duration_ms == 0


def test_ordering_is_chunks_then_book_then_queue(book_repo, chunk_repo, fixed_clock) -> None:
    """phase-4 §8.2 step 7's rule: nothing is published for a chunk still
    marked FAILED, and no publish outruns the state it describes."""
    events: list[str] = []

    class SpyChunkRepo:
        def __init__(self, inner):
            self._inner = inner

        def list_for_book(self, book_id):
            return self._inner.list_for_book(book_id)

        def update_status(self, *args, **kwargs):
            events.append("chunk")
            return self._inner.update_status(*args, **kwargs)

    class SpyBookRepo:
        def __init__(self, inner):
            self._inner = inner

        def get(self, *args, **kwargs):
            return self._inner.get(*args, **kwargs)

        def update_status(self, *args, **kwargs):
            events.append("book")
            return self._inner.update_status(*args, **kwargs)

    class SpyQueue(FakeSynthesisQueue):
        def enqueue_chunks(self, **kwargs):
            events.append("queue")
            return super().enqueue_chunks(**kwargs)

    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))

    ResynthesizeBook(
        SpyBookRepo(book_repo),
        SpyChunkRepo(chunk_repo),
        SpyQueue(),
        FakeStitchQueue(),
        fixed_clock,
    ).execute(USER, BOOK)

    assert events == ["chunk", "chunk", "book", "queue"]


# --- the STITCH_FAILED branch (§5.2 step 6) --------------------------------


def test_zero_failed_chunks_publishes_a_stitch_not_a_fan_out(
    book_repo, chunk_repo, fixed_clock
) -> None:
    """A PARTIAL/STITCH_FAILED book has no FAILED chunks: concatenation
    itself gave up. With no chunk left to increment, nothing would ever
    publish a stitch unless this branch does it directly."""
    _seed_partial_book(
        book_repo,
        chunk_repo,
        failed_indexes=(),
        failure_reason=StitchFailure.STITCH_FAILED.value,
    )
    synthesis, stitch = FakeSynthesisQueue(), FakeStitchQueue()

    result = _use_case(
        book_repo, chunk_repo, fixed_clock, synthesis=synthesis, stitch=stitch
    ).execute(USER, BOOK)

    assert result.retried_chunks == 0
    assert result.republished_stitch is True
    assert synthesis.calls == []
    assert stitch.calls == [{"user_id": USER, "book_id": BOOK}]


def test_zero_failed_chunks_leaves_counters_complete(book_repo, chunk_repo, fixed_clock) -> None:
    _seed_partial_book(
        book_repo,
        chunk_repo,
        failed_indexes=(),
        failure_reason=StitchFailure.STITCH_FAILED.value,
    )

    _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, BOOK)

    book = book_repo.get(USER, BOOK)
    assert book.chunks_done == book.chunks_total == TOTAL
    assert book.status is BookStatus.EXTRACTED


# --- crash recovery and concurrency ----------------------------------------


def test_pending_chunks_are_candidates_too(book_repo, chunk_repo, fixed_clock) -> None:
    """A previous /resynthesize that died between the chunk resets and the
    book update leaves PENDING chunks on a still-PARTIAL book. Excluding them
    would make the retry a permanent no-op."""
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1,))
    chunk_repo.update_status(BOOK, 3, ChunkStatus.PENDING)
    synthesis = FakeSynthesisQueue()

    result = _use_case(book_repo, chunk_repo, fixed_clock, synthesis=synthesis).execute(USER, BOOK)

    assert result.retried_chunks == 2
    assert synthesis.calls[0]["chunk_indexes"] == [1, 3]


def test_a_concurrently_claimed_chunk_is_logged_and_excluded(
    book_repo, chunk_repo, fixed_clock, caplog
) -> None:
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))

    class ConflictOnOne:
        def __init__(self, inner):
            self._inner = inner

        def list_for_book(self, book_id):
            return self._inner.list_for_book(book_id)

        def update_status(self, book_id, index, status, **kwargs):
            if index == 1:
                raise ConflictError("claimed elsewhere")
            return self._inner.update_status(book_id, index, status, **kwargs)

    synthesis = FakeSynthesisQueue()
    with caplog.at_level("INFO", logger="bookloud.resynthesis"):
        result = ResynthesizeBook(
            book_repo, ConflictOnOne(chunk_repo), synthesis, FakeStitchQueue(), fixed_clock
        ).execute(USER, BOOK)

    assert result.retried_chunks == 1
    assert synthesis.calls[0]["chunk_indexes"] == [3]
    # Excluded from the count, not merely from the publish: counting it would
    # rewind chunks_done for work that is still in flight.
    assert book_repo.get(USER, BOOK).chunks_done == TOTAL - 1
    assert "claimed concurrently" in caplog.text


def test_double_execution_yields_one_success_and_one_conflict(
    book_repo, chunk_repo, fixed_clock
) -> None:
    """The conditional ``expected_statuses=(PARTIAL,)`` book update is the
    exactly-once gate: a double-clicked button, or two tabs, produces one 202
    and one 409."""
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))
    use_case = _use_case(book_repo, chunk_repo, fixed_clock)

    use_case.execute(USER, BOOK)

    with pytest.raises(ConflictError):
        use_case.execute(USER, BOOK)


# --- the status guard ------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        BookStatus.UPLOADED,
        BookStatus.EXTRACTING,
        BookStatus.EXTRACTED,
        BookStatus.STITCHING,
        BookStatus.READY,
        BookStatus.FAILED,
    ],
)
def test_non_partial_statuses_raise_conflict(
    book_repo, chunk_repo, fixed_clock, status: BookStatus
) -> None:
    seed_book(book_repo, id=BOOK, user_id=USER)
    seed_chunks(chunk_repo, book_id=BOOK, user_id=USER, count=TOTAL)
    book_repo.update_status(USER, BOOK, status, chunks_total=TOTAL)
    synthesis = FakeSynthesisQueue()

    with pytest.raises(ConflictError):
        _use_case(book_repo, chunk_repo, fixed_clock, synthesis=synthesis).execute(USER, BOOK)

    assert synthesis.calls == []


def test_another_users_book_is_not_found(book_repo, chunk_repo, fixed_clock) -> None:
    _seed_partial_book(book_repo, chunk_repo)

    with pytest.raises(NotFoundError):
        _use_case(book_repo, chunk_repo, fixed_clock).execute(OTHER_USER, BOOK)


def test_a_missing_book_is_not_found(book_repo, chunk_repo, fixed_clock) -> None:
    with pytest.raises(NotFoundError):
        _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, "nope")


# --- the two guards this phase must not weaken ------------------------------


def test_status_tuples_unchanged() -> None:
    """PLANS/phase-5.md OQ-6 / phase-6.md §5.4. Asserted *literally*, not via
    a membership check, so that adding a status to any of these three tuples
    is a test failure rather than a silent widening.

    Why ``PARTIAL`` must stay out of the first two: both are sets a
    *redelivered S3 event* can act on. A book that is ``PARTIAL`` has good
    text and bad audio; letting an extract claim it would call
    ``delete_for_book`` and re-extract, throwing the text away to fix the
    audio. ``/resynthesize`` exists precisely so that trade never has to be
    made."""
    from src.contexts.library.application.extraction import _CLAIMABLE_STATUSES
    from src.contexts.library.application.use_cases import _REISSUABLE_STATUSES
    from src.contexts.library.domain.value_objects import STITCHABLE_BOOK_STATUSES

    assert _REISSUABLE_STATUSES == (BookStatus.UPLOADED, BookStatus.FAILED)
    assert _CLAIMABLE_STATUSES == (BookStatus.UPLOADED, BookStatus.FAILED)
    assert STITCHABLE_BOOK_STATUSES == (BookStatus.EXTRACTED, BookStatus.STITCHING)


def test_stale_stitch_message_is_deferred_after_resynthesize(
    book_repo, chunk_repo, fixed_clock
) -> None:
    """§5.4's named hazard, run against the real ``StitchBook``.

    After a resynthesize the book sits at ``EXTRACTED`` with
    ``chunks_done < chunks_total``, and ``EXTRACTED`` is in
    ``STITCHABLE_BOOK_STATUSES`` -- so a stale in-flight stitch message
    (plausible precisely here: the user is retrying *because* something
    failed) could in principle claim it and stitch a half-finished set. The
    guard is ``StitchBook``'s ``chunks_done < chunks_total`` short-circuit,
    checked *before* the claim."""
    _seed_partial_book(book_repo, chunk_repo, failed_indexes=(1, 3))
    _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, BOOK)

    stitcher = StitchBook(
        book_repo,
        chunk_repo,
        RecordingObjectStorage(label="audio"),
        RecordingObjectStorage(label="marks"),
        fixed_clock,
    )
    result = stitcher.execute(StitchBookCommand(user_id=USER, book_id=BOOK))

    assert result.outcome == "DEFERRED"
    assert result.reason == "NOT_COMPLETE"
    # And the book was NOT claimed -- it is still waiting for the fan-out.
    assert book_repo.get(USER, BOOK).status is BookStatus.EXTRACTED


def test_stale_stitch_after_a_zero_reset_resynthesize_is_honoured(
    book_repo, chunk_repo, fixed_clock
) -> None:
    """The other half of §5.4: with ``reset == 0`` the counters stay
    complete, so the stitch this use case published is not deferred -- a
    duplicate stitch is exactly what was asked for, and the stitcher's writes
    are idempotent by construction."""
    _seed_partial_book(
        book_repo,
        chunk_repo,
        failed_indexes=(),
        failure_reason=StitchFailure.STITCH_FAILED.value,
    )
    _use_case(book_repo, chunk_repo, fixed_clock).execute(USER, BOOK)

    audio = RecordingObjectStorage(label="audio")
    for index in range(TOTAL):
        audio.objects[f"audio/{USER}/{BOOK}/{index:06d}.mp3"] = _mpeg_frames()
    stitcher = StitchBook(
        book_repo, chunk_repo, audio, RecordingObjectStorage(label="marks"), fixed_clock
    )

    result = stitcher.execute(StitchBookCommand(user_id=USER, book_id=BOOK))

    assert result.outcome in ("READY", "PARTIAL")
    assert result.reason != "NOT_COMPLETE"


def _mpeg_frames() -> bytes:
    from tests.contexts.library.fakes import mpeg2_frames

    return mpeg2_frames(42)
