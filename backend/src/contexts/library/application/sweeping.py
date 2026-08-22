"""``SweepDlq`` -- the phase-8 DLQ-sweeper Lambda's use case (deferred from
``PLANS/phase-3.md`` OQ-4, extended by ``PLANS/phase-5.md`` §4.3/OQ-4;
``IMPLEMENTATION_PLAN.md`` phase 8).

**Why this exists.** Two failure modes leave a book (or a chunk) stuck with
no further SQS redelivery ever coming:

1. **Extract DLQ.** ``application/extraction.py``'s ``ExtractBook`` releases
   the ``EXTRACTING`` claim back to ``UPLOADED`` on any transient failure and
   re-raises so SQS retries -- but it has no last-attempt conversion of its
   own (unlike ``SynthesizeChunk``). So once the extract queue's own retry
   budget is exhausted, the message DLQs and the book is left in
   ``UPLOADED``/``EXTRACTING`` forever, indistinguishable from "just
   uploaded, not yet processed" to a polling client. ``sweep_extract`` is the
   backstop: mark it ``FAILED`` (``ExtractionFailure.DLQ_EXHAUSTED``) so the
   existing ``_CLAIMABLE_STATUSES`` re-extraction path can pick it back up on
   a future retry, exactly as any other extraction failure would.

2. **Synthesize DLQ.** ``SynthesizeChunk`` *does* have a last-attempt
   conversion (``_handle_transient``), so in the ordinary case a chunk
   message never reaches the synthesize DLQ -- it is finalized to
   ``ChunkStatus.DONE``/``FAILED`` on its last SQS attempt and the message is
   deleted normally. Two things can still land a message in this DLQ:

   - a crash in the Lambda's composition root itself (before ``execute()``
     ever runs), leaving the chunk genuinely non-terminal -- ``sweep_synthesize``
     finalizes it here exactly as ``_handle_transient``'s last-attempt branch
     would have (permanent ``FAILED``, one counter increment);
   - PLANS/phase-5.md §4.3's *named residual hole*: the chunk itself is
     already terminal (``DONE``/``FAILED``, counted), but the ``STITCH_REQUEUED``
     branch's own ``enqueue_book`` call failed on every redelivery of that
     terminal chunk's message too, so it DLQs instead of ever giving the
     ``execute()`` re-entrancy branch a chance. ``sweep_synthesize`` re-checks
     completion and republishes via the exact same
     ``application/synthesis.py.republish_stitch_if_complete`` helper
     ``SynthesizeChunk`` itself uses -- one implementation, two call sites.

**Never marks a book ``FAILED`` from the synthesize path** -- that status is
reserved for extraction (``domain/value_objects.py``'s ``BookStatus.FAILED``
docstring); a book with good text but unusable audio must stay
re-claimable-for-stitch, not re-extractable.

**Never touches the stitch DLQ.** A stitch message that DLQs means the stitch
Lambda itself never got the chance to run its own last-attempt conversion (to
``PARTIAL``/``STITCH_FAILED``) -- e.g. every attempt hard-crashed rather than
returning normally. Recovering that safely would mean either re-running the
whole concatenation from this narrow sweeper or writing a second, PARTIAL-only
finalizer that duplicates ``application/stitching.py``'s terminal-transition
logic; neither was asked for by phase 8's checklist, which names only the
extract/synthesize case. The CloudWatch alarm on stitch DLQ depth (§ below)
still exists so a human notices and can manually redrive/investigate --
exactly phase 3's already-accepted "manual redrive" posture, just now backed
by an alarm instead of nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from src.contexts.library.application.synthesis import republish_stitch_if_complete
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.stitching import StitchQueue
from src.contexts.library.domain.value_objects import (
    NON_TERMINAL_CHUNK_STATUSES,
    BookStatus,
    ChunkStatus,
    ExtractionFailure,
    SynthesisFailure,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

logger = logging.getLogger("bookloud.dlq_sweep")

Outcome = Literal["FAILED", "STITCH_REQUEUED", "SKIPPED"]

# A book stranded by an exhausted extract message is always in one of these
# two statuses -- see this module's docstring, point 1.
_STRANDED_EXTRACT_STATUSES = (BookStatus.UPLOADED, BookStatus.EXTRACTING)

# NOT ``NON_TERMINAL_CHUNK_STATUSES`` -- that set deliberately INCLUDES
# ``FAILED`` (it's "eligible to be re-claimed for synthesis", the complement
# of DONE). Here we need the opposite question: "has this chunk already been
# counted by an increment_chunks_done call?" -- true for DONE *and* FAILED,
# false only for PENDING/SYNTHESIZING. Reusing the wrong set would
# re-finalize (and double-increment) a chunk that already failed normally.
_UNCOUNTED_CHUNK_STATUSES = (ChunkStatus.PENDING, ChunkStatus.SYNTHESIZING)


@dataclass(frozen=True)
class SweepExtractDlqCommand:
    user_id: str
    book_id: str


@dataclass(frozen=True)
class SweepSynthesizeDlqCommand:
    user_id: str
    book_id: str
    chunk_index: int


@dataclass(frozen=True)
class SweepDlqResult:
    outcome: Outcome
    reason: str | None = None


class SweepDlq:
    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        stitch_queue: StitchQueue,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._stitch_queue = stitch_queue

    def sweep_extract(self, command: SweepExtractDlqCommand) -> SweepDlqResult:
        book = self._book_repository.get(command.user_id, command.book_id)
        if book is None:
            return SweepDlqResult("SKIPPED", reason="BOOK_NOT_FOUND")
        if book.status not in _STRANDED_EXTRACT_STATUSES:
            # Already resolved (e.g. a later, successful redelivery beat the
            # DLQ'd copy to the claim, or a human already redrove it) --
            # never clobber a book that has since moved on.
            return SweepDlqResult("SKIPPED", reason="ALREADY_RESOLVED")

        try:
            self._book_repository.update_status(
                command.user_id,
                command.book_id,
                BookStatus.FAILED,
                expected_statuses=_STRANDED_EXTRACT_STATUSES,
                failure_reason=ExtractionFailure.DLQ_EXHAUSTED.value,
            )
        except ConflictError:
            return SweepDlqResult("SKIPPED", reason="ALREADY_RESOLVED")
        except NotFoundError:
            return SweepDlqResult("SKIPPED", reason="BOOK_NOT_FOUND")

        logger.warning(
            "extract DLQ: book %s/%s stranded in %s, marked FAILED/%s",
            command.user_id,
            command.book_id,
            book.status,
            ExtractionFailure.DLQ_EXHAUSTED.value,
        )
        return SweepDlqResult("FAILED", reason=ExtractionFailure.DLQ_EXHAUSTED.value)

    def sweep_synthesize(self, command: SweepSynthesizeDlqCommand) -> SweepDlqResult:
        chunk = self._chunk_repository.get(command.book_id, command.chunk_index)
        if chunk is None:
            return SweepDlqResult("SKIPPED", reason="CHUNK_NOT_FOUND")

        if chunk.status in _UNCOUNTED_CHUNK_STATUSES:
            return self._finalize_stranded_chunk(command)

        # Chunk already terminal (DONE/FAILED, already counted): this is
        # PLANS/phase-5.md §4.3's named residual hole -- the completing
        # chunk's message DLQ'd purely because every ``enqueue_book`` retry
        # failed, not because the chunk itself needs anything more done to
        # it. Re-publish if the book is now complete and still EXTRACTED.
        if republish_stitch_if_complete(
            self._book_repository, self._stitch_queue, user_id=command.user_id, book_id=command.book_id
        ):
            logger.warning(
                "synthesize DLQ: book %s/%s complete but stuck EXTRACTED, re-published stitch",
                command.user_id,
                command.book_id,
            )
            return SweepDlqResult("STITCH_REQUEUED")
        return SweepDlqResult("SKIPPED", reason="ALREADY_TERMINAL")

    def _finalize_stranded_chunk(self, command: SweepSynthesizeDlqCommand) -> SweepDlqResult:
        """A chunk whose message exhausted the synthesize queue's retry
        budget without ``SynthesizeChunk._handle_transient``'s own
        last-attempt rule ever converting it -- e.g. a crash in the Lambda's
        composition root before ``execute()`` ran. Finalize it exactly as
        that rule would have."""
        try:
            self._chunk_repository.update_status(
                command.book_id,
                command.chunk_index,
                ChunkStatus.FAILED,
                expected_statuses=NON_TERMINAL_CHUNK_STATUSES,
                failure_reason=SynthesisFailure.UNKNOWN.value,
            )
        except ConflictError:
            # Another invocation reached DONE/FAILED first (and already
            # incremented) between our `get` and this `update_status` --
            # fall through to the stitch-completion check below rather than
            # double-incrementing.
            pass
        except NotFoundError:
            return SweepDlqResult("SKIPPED", reason="CHUNK_NOT_FOUND")
        else:
            logger.warning(
                "synthesize DLQ: chunk %s/%s#%d stranded non-terminal, marked FAILED/%s",
                command.user_id,
                command.book_id,
                command.chunk_index,
                SynthesisFailure.UNKNOWN.value,
            )
            counters = self._increment(command)
            if counters is not None and counters.is_complete:
                self._stitch_queue.enqueue_book(user_id=command.user_id, book_id=command.book_id)
                return SweepDlqResult("STITCH_REQUEUED")
            return SweepDlqResult("FAILED", reason=SynthesisFailure.UNKNOWN.value)

        if republish_stitch_if_complete(
            self._book_repository, self._stitch_queue, user_id=command.user_id, book_id=command.book_id
        ):
            return SweepDlqResult("STITCH_REQUEUED")
        return SweepDlqResult("SKIPPED", reason="ALREADY_TERMINAL")

    def _increment(self, command: SweepSynthesizeDlqCommand):
        try:
            return self._book_repository.increment_chunks_done(command.user_id, command.book_id, failed=True)
        except NotFoundError:
            logger.warning(
                "synthesize DLQ: book %s not found while incrementing counters for chunk %s (already terminal)",
                command.book_id,
                command.chunk_index,
            )
            return None
