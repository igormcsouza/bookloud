"""``SynthesizeChunk`` -- the synthesize Lambda's use case (PLANS/phase-4.md
§8.2-§8.4). The pipeline's "controller" (``interface/synthesize_handler.py``)
builds one of these per invocation and calls ``execute`` once per SQS
message.

The exactly-once counter (§8.4) is the module's correctness core: the
terminal transition (``-> DONE``/``-> FAILED``) is a conditional
``update_status`` guarded by ``expected_statuses=NON_TERMINAL_CHUNK_STATUSES``,
and ``chunksDone``/``chunksFailed`` are incremented **only** when that
conditional update actually wins the race (``ConflictError`` means another
invocation already got there first, and already incremented). This is what
makes at-least-once SQS delivery, duplicate fan-out publishes, and
crashed-mid-flight invocations all safe without a distributed lock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from src.contexts.library.domain.marks import build_marks_document
from src.contexts.library.domain.repository import BookRepository, ChunkCounters, ChunkRepository
from src.contexts.library.domain.stitching import StitchQueue
from src.contexts.library.domain.storage import ObjectStorage
from src.contexts.library.domain.synthesis import (
    MAX_SYNTHESIS_CHARS,
    SpeechSynthesizer,
    SynthesisDisabled,
    SynthesisUnavailable,
    SynthesizedAudio,
    UnsynthesizableText,
)
from src.contexts.library.domain.value_objects import (
    NON_TERMINAL_CHUNK_STATUSES,
    BookStatus,
    ChunkStatus,
    SynthesisFailure,
)
from src.contexts.library.infrastructure.s3_keys import (
    AUDIO_CONTENT_TYPE,
    MARKS_CONTENT_TYPE,
    chunk_audio_key,
    chunk_marks_key,
)
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

logger = logging.getLogger("bookloud.synthesis")

Outcome = Literal["DONE", "FAILED", "SKIPPED"]

# 5, matching infra/stacks/config.py's Config.SYNTHESIZE_MAX_RECEIVE_COUNT /
# backend/src/config.py's synthesize_max_receive_count -- the standard
# lockstep comment applies (separately deployed projects, cannot share an
# import).
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class SynthesizeChunkCommand:
    user_id: str
    book_id: str
    chunk_index: int
    attempt: int = 1


@dataclass(frozen=True)
class SynthesizeChunkResult:
    outcome: Outcome
    reason: str | None = None
    duration_ms: int = 0
    source: str | None = None
    chunks_done: int = 0
    chunks_total: int = 0


class SynthesizeChunk:
    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        synthesizer: SpeechSynthesizer,
        audio_storage: ObjectStorage,
        marks_storage: ObjectStorage,
        stitch_queue: StitchQueue,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._synthesizer = synthesizer
        self._audio_storage = audio_storage
        self._marks_storage = marks_storage
        self._stitch_queue = stitch_queue
        self._max_attempts = max_attempts

    def execute(self, command: SynthesizeChunkCommand) -> SynthesizeChunkResult:
        chunk = self._chunk_repository.get(command.book_id, command.chunk_index)
        if chunk is None:
            return SynthesizeChunkResult("SKIPPED", reason="CHUNK_NOT_FOUND")
        if chunk.status is ChunkStatus.DONE:
            # Cheap duplicate-delivery short-circuit: no engine call, no S3
            # write. But a redelivery of an already-DONE chunk also means the
            # previous invocation's stitch publish is not known to have
            # completed -- it is the only step after the counter increment
            # that can fail. Re-publish; never re-synthesize. Exactly
            # PLANS/phase-4.md §4.3's REQUEUED shape, and duplicate stitch
            # messages are safe by construction (PLANS/phase-5.md §6.4's
            # conditional claim).
            if self._republish_stitch_if_complete(command):
                return SynthesizeChunkResult("SKIPPED", reason="STITCH_REQUEUED")
            return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")
        if chunk.user_id != command.user_id:
            # Defence in depth, mirrors ExtractBook's KEY_MISMATCH check.
            return SynthesizeChunkResult("SKIPPED", reason="USER_MISMATCH")

        # Validate the text BEFORE any engine call -- this is the only place
        # permanence is decided proactively rather than reactively.
        validation_error = _validate_text(chunk.text)

        try:
            # "Anything but DONE" -- deliberately includes SYNTHESIZING
            # itself: a previous invocation that died hard (OOM, hard
            # timeout, evicted) leaves the chunk in SYNTHESIZING with no
            # claim timestamp to expire. Allowing the re-claim costs, at
            # worst, one duplicated synthesis; the counter is still
            # incremented exactly once (see below).
            self._chunk_repository.update_status(
                command.book_id,
                command.chunk_index,
                ChunkStatus.SYNTHESIZING,
                expected_statuses=NON_TERMINAL_CHUNK_STATUSES,
            )
        except ConflictError:
            return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")
        except NotFoundError:
            return SynthesizeChunkResult("SKIPPED", reason="CHUNK_NOT_FOUND")

        if validation_error is not None:
            return self._finish_failed(command, validation_error.reason.value)

        try:
            audio = self._synthesizer.synthesize(chunk.text)
        except (UnsynthesizableText, SynthesisDisabled) as exc:
            # Permanent: a property of the input or of the deployment
            # environment (§0), decided before/at the engine call, not by
            # the SQS attempt count.
            return self._finish_failed(command, exc.reason.value)
        except Exception as exc:  # noqa: BLE001 -- SynthesisUnavailable or a genuine bug; both transient-by-default
            return self._handle_transient(command, exc)

        audio_key = chunk_audio_key(command.user_id, command.book_id, command.chunk_index)
        marks_key = chunk_marks_key(command.user_id, command.book_id, command.chunk_index)
        document = build_marks_document(
            book_id=command.book_id,
            chunk_index=command.chunk_index,
            user_id=command.user_id,
            audio_key=audio_key,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            audio=audio,
        )

        try:
            # Audio first, then marks, then DynamoDB: the chunk item is the
            # only thing that ever advertises these keys, and it's written
            # last, so a consumer that sees audioKey is guaranteed both
            # objects exist. A crash between the two put()s leaves an orphan
            # MP3 that the next attempt overwrites at the identical
            # (idempotent) key.
            self._audio_storage.put_bytes(key=audio_key, data=audio.audio, content_type=AUDIO_CONTENT_TYPE)
            self._marks_storage.put_bytes(
                key=marks_key,
                data=json.dumps(document, separators=(",", ":")).encode("utf-8"),
                content_type=MARKS_CONTENT_TYPE,
            )
        except Exception as exc:  # noqa: BLE001 -- S3 5xx/throttle, same transient posture
            return self._handle_transient(command, exc)

        return self._finish_done(command, audio, audio_key=audio_key, marks_key=marks_key)

    def _handle_transient(self, command: SynthesizeChunkCommand, exc: Exception) -> SynthesizeChunkResult:
        if command.attempt < self._max_attempts:
            try:
                # Release the claim so a retry (this SQS redelivery or the
                # next one) can re-claim from PENDING. Conditional on still
                # being SYNTHESIZING: a concurrent duplicate invocation may
                # have already reached a terminal state while this one was
                # mid-flight (the claim deliberately allows concurrent
                # re-claims -- see above) -- releasing unconditionally would
                # revert an already-DONE chunk back to PENDING and, worse,
                # let a later re-synthesis double-increment chunksDone.
                self._chunk_repository.update_status(
                    command.book_id,
                    command.chunk_index,
                    ChunkStatus.PENDING,
                    expected_statuses=(ChunkStatus.SYNTHESIZING,),
                )
            except ConflictError:
                return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")
            except NotFoundError:
                return SynthesizeChunkResult("SKIPPED", reason="CHUNK_NOT_FOUND")
            # Re-raise so SQS actually retries (matches ExtractBook's
            # posture for the same class of transient failure).
            raise exc

        # Last attempt: convert transient into permanent rather than letting
        # the message die in the DLQ -- see PLANS/phase-4.md §8.3 for why
        # DLQ residue per chunk is much worse than per book.
        reason = (
            SynthesisFailure.ALL_ENGINES_FAILED.value
            if isinstance(exc, SynthesisUnavailable)
            else SynthesisFailure.UNKNOWN.value
        )
        logger.warning(
            "chunk %s/%s exhausted %d attempt(s), marking FAILED/%s: %s",
            command.book_id,
            command.chunk_index,
            self._max_attempts,
            reason,
            exc,
        )
        return self._finish_failed(command, reason)

    def _finish_failed(self, command: SynthesizeChunkCommand, reason: str) -> SynthesizeChunkResult:
        try:
            self._chunk_repository.update_status(
                command.book_id,
                command.chunk_index,
                ChunkStatus.FAILED,
                expected_statuses=NON_TERMINAL_CHUNK_STATUSES,
                failure_reason=reason,
            )
        except ConflictError:
            # Another invocation reached DONE/FAILED first. It already
            # incremented -- doing so again would push chunksDone past
            # chunksTotal (§8.4).
            return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")
        except NotFoundError:
            return SynthesizeChunkResult("SKIPPED", reason="CHUNK_NOT_FOUND")

        counters = self._increment(command, failed=True)
        return SynthesizeChunkResult(
            "FAILED",
            reason=reason,
            chunks_done=counters.chunks_done if counters else 0,
            chunks_total=counters.chunks_total if counters else 0,
        )

    def _finish_done(
        self, command: SynthesizeChunkCommand, audio: SynthesizedAudio, *, audio_key: str, marks_key: str
    ) -> SynthesizeChunkResult:
        try:
            self._chunk_repository.update_status(
                command.book_id,
                command.chunk_index,
                ChunkStatus.DONE,
                expected_statuses=NON_TERMINAL_CHUNK_STATUSES,
                audio_key=audio_key,
                marks_key=marks_key,
                duration_ms=audio.duration_ms,
                synthesis_source=audio.source.value,
                clear_failure_reason=True,
            )
        except ConflictError:
            return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")
        except NotFoundError:
            return SynthesizeChunkResult("SKIPPED", reason="CHUNK_NOT_FOUND")

        counters = self._increment(command, failed=False)
        return SynthesizeChunkResult(
            "DONE",
            duration_ms=audio.duration_ms,
            source=audio.source.value,
            chunks_done=counters.chunks_done if counters else 0,
            chunks_total=counters.chunks_total if counters else 0,
        )

    def _increment(self, command: SynthesizeChunkCommand, *, failed: bool) -> ChunkCounters | None:
        try:
            counters = self._book_repository.increment_chunks_done(
                command.user_id, command.book_id, failed=failed
            )
        except NotFoundError:
            # The book was deleted mid-synthesis: the chunk is already
            # terminal, the message is deleted, and there is nothing left
            # to count (§8.4).
            logger.warning(
                "book %s not found while incrementing counters for chunk %s (already terminal)",
                command.book_id,
                command.chunk_index,
            )
            return None
        logger.info(
            "chunk %s/%s done for book %s (%d/%d, %d failed)",
            command.chunk_index,
            command.book_id,
            command.book_id,
            counters.chunks_done,
            counters.chunks_total,
            counters.chunks_failed,
        )
        self._maybe_publish_stitch(command, counters)
        return counters

    def _maybe_publish_stitch(
        self, command: SynthesizeChunkCommand, counters: ChunkCounters | None
    ) -> None:
        """The fan-in edge (PLANS/phase-5.md §4.2). ``increment_chunks_done``
        is an atomic ADD returning ALL_NEW, and phase-4 §8.4 guarantees each
        chunk increments at most once -- so exactly one invocation ever
        observes ``chunks_done == chunks_total``. That single observation is
        the trigger; no extra read and no lock are needed.

        Called from ``_increment``, whose single call site is reached from
        **both** ``_finish_done`` and ``_finish_failed``. Publishing only
        from the success path would mean the stitcher never fires anywhere CI
        can see it: in local dev and every PR stack every chunk fails
        (phase-4 §0), so the completing increment is always a *failed* one.

        A raise here is deliberately NOT swallowed: the SQS message stays
        undeleted, and the redelivery hits ``execute``'s ``STITCH_REQUEUED``
        branch (§4.3).
        """
        if counters is None or not counters.is_complete:
            return
        self._stitch_queue.enqueue_book(user_id=command.user_id, book_id=command.book_id)

    def _republish_stitch_if_complete(self, command: SynthesizeChunkCommand) -> bool:
        return republish_stitch_if_complete(
            self._book_repository, self._stitch_queue, user_id=command.user_id, book_id=command.book_id
        )


def _validate_text(text: str) -> UnsynthesizableText | None:
    if not text.strip():
        return UnsynthesizableText(SynthesisFailure.EMPTY_TEXT, "chunk text is empty")
    if len(text) > MAX_SYNTHESIS_CHARS:
        return UnsynthesizableText(SynthesisFailure.TEXT_TOO_LONG, "chunk text exceeds MAX_SYNTHESIS_CHARS")
    return None


def republish_stitch_if_complete(
    book_repository: BookRepository, stitch_queue: StitchQueue, *, user_id: str, book_id: str
) -> bool:
    """Shared by ``SynthesizeChunk``'s own ``STITCH_REQUEUED`` branch (a
    redelivered already-``DONE`` chunk message) and, since phase 8, by
    ``application/sweeping.py``'s ``SweepDlq`` (PLANS/phase-5.md §4.3's named
    residual hole: if ``enqueue_book`` fails on every one of the completing
    chunk's attempts, the message DLQs instead of being redelivered, and only
    the DLQ sweeper ever sees it again). Pulled out to a module-level
    function -- not a method -- specifically so both call sites share one
    implementation instead of two copies drifting apart.
    """
    book = book_repository.get(user_id, book_id)
    # BookStatus.EXTRACTED only: a book already STITCHING/READY/PARTIAL has
    # either been picked up or finished, and re-publishing would queue
    # pointless work behind the stitcher's conditional claim.
    if book is None or book.status is not BookStatus.EXTRACTED:
        return False
    if book.chunks_total == 0 or book.chunks_done < book.chunks_total:
        return False
    stitch_queue.enqueue_book(user_id=user_id, book_id=book_id)
    return True
