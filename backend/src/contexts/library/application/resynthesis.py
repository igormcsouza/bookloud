"""``ResynthesizeBook`` -- the repair path for a ``PARTIAL`` book
(PLANS/phase-6.md §5, resolving PLANS/phase-5.md OQ-6).

**The problem this solves, and the shortcut it refuses to take.** Today a
book whose engines all failed is a dead end: it cannot be re-uploaded
(``_REISSUABLE_STATUSES`` excludes ``PARTIAL``) and cannot be re-extracted
(``_CLAIMABLE_STATUSES`` excludes it). The obvious "fix" -- adding
``PARTIAL`` to those tuples -- reintroduces precisely the hazard ``PARTIAL``
was invented to prevent: a stray redelivered S3 event claiming a book with
perfectly good text, calling ``delete_for_book``, and re-extracting it
because the *audio* was unavailable. **This module touches neither tuple**,
and ``test_resynthesis_use_case.py``'s ``test_status_tuples_unchanged``
asserts all three literally, so a future shortcut fails a unit test before
it can fail a book.

The right lever is not re-upload. The PDF is fine, the text is fine, the
chunks are fine; only synthesis failed. So: **rewind the chunks that failed,
rewind the counters by the same amount, and re-fire the existing fan-out.**
Nothing new is invented -- ``SynthesizeChunk``, the fan-in counter,
``_maybe_publish_stitch`` and ``StitchBook`` all then run exactly as they do
after a first extraction.

**Ordering is load-bearing** (chunks -> book -> queue, phase-4 §8.2 step 7's
rule) and has a dedicated test:

1. Chunks first, so nothing is published for a chunk still marked ``FAILED``.
2. The book's conditional ``expected_statuses=(PARTIAL,)`` update second --
   **this is the exactly-once gate**. A double-clicked button, or two tabs,
   produces one ``202`` and one ``409``.
3. The queue last, so a publish never outruns the state it describes.

**The named hazard (§5.4).** Between step 2 and the new work completing, the
book sits at ``EXTRACTED`` with ``chunks_done < chunks_total``. ``EXTRACTED``
is in ``STITCHABLE_BOOK_STATUSES``, so a stale in-flight stitch message
could in principle claim it -- plausible precisely here, since the user is
retrying *because* something failed. It cannot, and the guard already
exists: ``StitchBook.execute`` short-circuits with
``DEFERRED("NOT_COMPLETE")`` when ``chunks_done < chunks_total``, checked
**before** the claim. With ``reset > 0`` the counters are rewound, so a
stale message is deferred and the completing increment publishes a fresh
one. With ``reset == 0`` the counters are complete and a duplicate stitch is
exactly what we asked for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.contexts.library.application.use_cases import _load_owned_book
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.stitching import StitchQueue
from src.contexts.library.domain.synthesis import SynthesisQueue
from src.contexts.library.domain.value_objects import BookStatus, ChunkStatus
from src.shared_kernel.application.ports import Clock
from src.shared_kernel.domain.errors import ConflictError

logger = logging.getLogger("bookloud.resynthesis")

# `PENDING` is in here for crash recovery, not for symmetry: if a previous
# /resynthesize died between the chunk resets and the book update, some
# chunks are already PENDING on a still-PARTIAL book, and excluding them
# would make the retry a permanent no-op.
_RETRYABLE_CHUNK_STATUSES = (ChunkStatus.FAILED, ChunkStatus.PENDING)


@dataclass(frozen=True)
class ResynthesizeBookResult:
    retried_chunks: int
    # True in the zero-failed-chunk (STITCH_FAILED) case, where concatenation
    # itself gave up: no chunk will ever increment again, so nothing would
    # publish a stitch unless this use case does it directly.
    republished_stitch: bool


class ResynthesizeBook:
    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        synthesis_queue: SynthesisQueue,
        stitch_queue: StitchQueue,
        clock: Clock,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._synthesis_queue = synthesis_queue
        self._stitch_queue = stitch_queue
        self._clock = clock

    def execute(self, user_id: str, book_id: str) -> ResynthesizeBookResult:
        book = _load_owned_book(self._book_repository, user_id, book_id)
        if book.status is not BookStatus.PARTIAL:
            # READY -> nothing failed. EXTRACTED/STITCHING/EXTRACTING -> work
            # is in flight and rewinding the counters underneath it would
            # corrupt the fan-in. UPLOADED/FAILED -> already covered by
            # POST /books/{id}/upload-url, which is what _REISSUABLE_STATUSES
            # exists for.
            raise ConflictError("Book has no failed audio to retry")

        reset = self._reset_failed_chunks(book_id)

        self._book_repository.update_status(
            user_id,
            book_id,
            BookStatus.EXTRACTED,
            expected_statuses=(BookStatus.PARTIAL,),
            chunks_done=max(0, book.chunks_total - len(reset)),
            chunks_failed=0,
            # Not optional: leaving a stale audioKey pointing at a book.mp3
            # assembled from the *old* chunk set would let the reader play
            # audio that no longer matches the manifest it is handed.
            clear_stitch_outputs=True,
            clear_failure_reason=True,
            updated_at=self._clock.now().isoformat(),
        )

        if reset:
            self._synthesis_queue.enqueue_chunks(
                user_id=user_id, book_id=book_id, chunk_indexes=reset
            )
            return ResynthesizeBookResult(retried_chunks=len(reset), republished_stitch=False)

        # PARTIAL/STITCH_FAILED: zero FAILED chunks, chunks_done still equals
        # chunks_total, so no chunk will ever increment again. Publishing a
        # stitch directly is correct -- the book is now EXTRACTED, which is in
        # STITCHABLE_BOOK_STATUSES, and the stitcher's writes are idempotent
        # by construction (every key is a pure function of (user_id, book_id),
        # PLANS/phase-5.md §6.4).
        self._stitch_queue.enqueue_book(user_id=user_id, book_id=book_id)
        return ResynthesizeBookResult(retried_chunks=0, republished_stitch=True)

    def _reset_failed_chunks(self, book_id: str) -> list[int]:
        reset: list[int] = []
        for chunk in sorted(self._chunk_repository.list_for_book(book_id), key=lambda c: c.index):
            if chunk.status not in _RETRYABLE_CHUNK_STATUSES:
                continue
            try:
                self._chunk_repository.update_status(
                    book_id,
                    chunk.index,
                    ChunkStatus.PENDING,
                    expected_statuses=_RETRYABLE_CHUNK_STATUSES,
                    clear_failure_reason=True,
                )
            except ConflictError:
                # Something else claimed it between the list and the update.
                # The chunk-level conditional claim is the authority (phase-4
                # §8.4), so drop it from the count and carry on -- counting it
                # would rewind chunks_done for work that is still in flight.
                logger.info(
                    "chunk %s of book %s was claimed concurrently; not retrying it",
                    chunk.index,
                    book_id,
                )
                continue
            reset.append(chunk.index)
        return reset
