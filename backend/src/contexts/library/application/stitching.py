"""``StitchBook`` -- the stitch Lambda's use case (PLANS/phase-5.md §6.4/§7).
The pipeline's "controller" (``interface/stitch_handler.py``) builds one of
these per invocation and calls ``execute`` once per SQS message.

Two things carry the correctness of this module:

1. **The exactly-once gate is the terminal transition, not the claim.** The
   claim is deliberately permissive (``EXTRACTED`` *or* ``STITCHING`` ->
   ``STITCHING``) because a stitcher that died hard leaves ``STITCHING`` with
   no lease to expire; excluding it would wedge the book forever. The
   terminal update is the strict one (``expected_statuses=(STITCHING,)``), so
   a ``ConflictError`` there means a concurrent duplicate already finished --
   and this invocation wrote only bytes that were byte-identical anyway,
   because every S3 key here is a pure function of ``(user_id, book_id)``.
2. **The stitcher NEVER writes book status ``FAILED``** (§3). ``FAILED``
   keeps exactly its phase-3 meaning ("extraction produced nothing usable")
   and is in both ``_CLAIMABLE_STATUSES`` and ``_REISSUABLE_STATUSES``, so
   reusing it for "no audio" would let a stray redelivered S3 event wipe and
   re-extract a perfectly good book. A book with no audio is ``PARTIAL`` +
   ``NO_AUDIO`` -- which is the *normal, everyday* state of every local and
   PR environment (phase-4 §0), not an exception path.

Ordering is load-bearing and has a dedicated test: audio -> manifest ->
DynamoDB, so anything that observes the book row's ``audioKey``/
``manifestKey`` is guaranteed both objects already exist.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

from src.contexts.library.domain.chunk import Chunk
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.stitching import (
    SegmentInput,
    build_book_manifest,
    plan_segments,
)
from src.contexts.library.domain.storage import ObjectStorage
from src.contexts.library.domain.value_objects import (
    STITCHABLE_BOOK_STATUSES,
    TERMINAL_BOOK_STATUSES,
    BookStatus,
    ChunkStatus,
    StitchFailure,
)
from src.contexts.library.infrastructure.mp3 import (
    mp3_duration_seconds,
    strip_container_headers,
)
from src.contexts.library.infrastructure.s3_keys import (
    AUDIO_CONTENT_TYPE,
    MARKS_CONTENT_TYPE,
    book_audio_key,
    book_manifest_key,
)
from src.shared_kernel.application.ports import Clock
from src.shared_kernel.domain.errors import ConflictError, NotFoundError

logger = logging.getLogger("bookloud.stitching")

Outcome = Literal["READY", "PARTIAL", "SKIPPED", "DEFERRED"]

# 3, matching infra/stacks/config.py's Config.STITCH_MAX_RECEIVE_COUNT /
# backend/src/config.py's stitch_max_receive_count -- the standard lockstep
# comment applies (separately deployed projects, cannot share an import).
DEFAULT_MAX_ATTEMPTS = 3

# A measured duration this far from the chunk item's stored durationMs means
# something is wrong (a half-written object, a re-synthesized chunk whose row
# didn't update). Log it; never fail the stitch over it.
DURATION_DRIFT_TOLERANCE_MS = 50

# PLANS/phase-5.md OQ-5: the 900 s Lambda timeout is estimated, never
# measured, and no automated test will ever measure it. Logging when a stitch
# crosses a third of the budget makes the margin observable in prod *before*
# it becomes an incident.
SLOW_STITCH_WARNING_SECONDS = 300


@dataclass(frozen=True)
class StitchBookCommand:
    user_id: str
    book_id: str
    attempt: int = 1


@dataclass(frozen=True)
class StitchBookResult:
    outcome: Outcome
    reason: str | None = None
    segments: int = 0
    missing: int = 0
    duration_ms: int = 0
    audio_key: str | None = None
    manifest_key: str | None = None


class StitchBook:
    def __init__(
        self,
        book_repository: BookRepository,
        chunk_repository: ChunkRepository,
        audio_storage: ObjectStorage,
        marks_storage: ObjectStorage,
        clock: Clock,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._book_repository = book_repository
        self._chunk_repository = chunk_repository
        self._audio_storage = audio_storage
        self._marks_storage = marks_storage
        self._clock = clock
        self._max_attempts = max_attempts

    def execute(self, command: StitchBookCommand) -> StitchBookResult:
        book = self._book_repository.get(command.user_id, command.book_id)
        if book is None:
            return StitchBookResult("SKIPPED", reason="BOOK_NOT_FOUND")
        if book.status in TERMINAL_BOOK_STATUSES:
            # Duplicate stitch message for a book that already finished.
            # Harmless by construction -- this is the branch that makes
            # §4.3's re-publish safe.
            return StitchBookResult("SKIPPED", reason="ALREADY_STITCHED")
        if book.chunks_total == 0 or book.chunks_done < book.chunks_total:
            # A stitch published before the counters flipped (or a book
            # re-extracted underneath an in-flight message). Not an error:
            # the completing increment will publish again.
            return StitchBookResult("DEFERRED", reason="NOT_COMPLETE")

        try:
            self._book_repository.update_status(
                command.user_id,
                command.book_id,
                BookStatus.STITCHING,
                expected_statuses=STITCHABLE_BOOK_STATUSES,
                updated_at=self._clock.now().isoformat(),
            )
        except ConflictError:
            return StitchBookResult("SKIPPED", reason="ALREADY_CLAIMED")
        except NotFoundError:
            return StitchBookResult("SKIPPED", reason="BOOK_NOT_FOUND")

        started = time.monotonic()
        chunks = sorted(self._chunk_repository.list_for_book(command.book_id), key=lambda c: c.index)
        try:
            plan = self._concatenate(command, chunks)
        except Exception as exc:  # noqa: BLE001 -- S3 5xx/throttle/timeouts; transient by default
            plan = self._handle_transient(command, chunks, exc)

        elapsed = time.monotonic() - started
        if elapsed > SLOW_STITCH_WARNING_SECONDS:
            logger.warning(
                "stitch for book %s took %.1fs (>%ds) -- approaching the 900s Lambda ceiling",
                command.book_id,
                elapsed,
                SLOW_STITCH_WARNING_SECONDS,
            )

        return self._finish(command, book, plan)

    # --- the bytes ---------------------------------------------------------

    def _concatenate(self, command: StitchBookCommand, chunks: list[Chunk]) -> _StitchPlan:
        done = [c for c in chunks if c.status is ChunkStatus.DONE and c.audio_key]
        missing = [c.index for c in chunks if c not in done]
        entries: list[SegmentInput] = []
        sample_rate_hz: int | None = None

        if done:
            target = book_audio_key(command.user_id, command.book_id)
            with self._audio_storage.open_multipart(
                key=target, content_type=AUDIO_CONTENT_TYPE
            ) as writer:
                for chunk in done:
                    assert chunk.audio_key is not None
                    raw = self._audio_storage.get_bytes(key=chunk.audio_key)
                    frames, rate = strip_container_headers(raw)
                    if not frames:
                        # An object that exists but carries no decodable
                        # frame contributes nothing and must not be
                        # advertised as playable.
                        logger.warning(
                            "chunk %s of book %s has no decodable MP3 frames at %s; skipping",
                            chunk.index,
                            command.book_id,
                            chunk.audio_key,
                        )
                        missing.append(chunk.index)
                        continue
                    if sample_rate_hz is None:
                        sample_rate_hz = rate
                    elif rate != sample_rate_hz:
                        # A mixed-engine book (§7.1 hazard 3). Bitrate drift
                        # is just VBR and every decoder handles it; a
                        # sample-rate change is not reliably handled -- but
                        # per-chunk playback still works, so log, record it
                        # in the manifest, and do not fail the stitch.
                        logger.warning(
                            "chunk %s of book %s has sample rate %s, expected %s",
                            chunk.index,
                            command.book_id,
                            rate,
                            sample_rate_hz,
                        )
                    seconds = mp3_duration_seconds(frames)
                    measured_ms = round(seconds * 1000)
                    if abs(measured_ms - chunk.duration_ms) > DURATION_DRIFT_TOLERANCE_MS:
                        # The manifest's t values are the only thing between
                        # the user and a drifting highlight, so they are
                        # derived from the bytes actually being served -- and
                        # a half-written object shows up here for free.
                        logger.warning(
                            "chunk %s duration drift: stored=%d measured=%d",
                            chunk.index,
                            chunk.duration_ms,
                            measured_ms,
                        )
                    writer.write(frames)
                    entries.append(
                        SegmentInput(
                            index=chunk.index,
                            duration_seconds=seconds,
                            char_start=chunk.char_start,
                            char_end=chunk.char_end,
                            audio_key=chunk.audio_key,
                            marks_key=chunk.marks_key,
                            byte_len=len(frames),
                        )
                    )

        segments, total_ms = plan_segments(entries)
        # `entries` can be empty even when `done` wasn't (every object
        # unusable); in that case the (empty) book.mp3 that was just
        # completed is simply never advertised.
        return _StitchPlan(
            segments=segments,
            missing=sorted(missing),
            total_ms=total_ms,
            sample_rate_hz=sample_rate_hz,
            audio_key=book_audio_key(command.user_id, command.book_id) if entries else None,
            stitch_failed=False,
        )

    def _handle_transient(
        self, command: StitchBookCommand, chunks: list[Chunk], exc: Exception
    ) -> _StitchPlan:
        if command.attempt < self._max_attempts:
            # Release the claim so a redelivery can re-claim from EXTRACTED.
            # Conditional on still being STITCHING: a concurrent duplicate
            # may already have reached a terminal state.
            try:
                self._book_repository.update_status(
                    command.user_id,
                    command.book_id,
                    BookStatus.EXTRACTED,
                    expected_statuses=(BookStatus.STITCHING,),
                    updated_at=self._clock.now().isoformat(),
                )
            except (ConflictError, NotFoundError):
                logger.warning(
                    "could not release the stitch claim on book %s (already terminal or gone)",
                    command.book_id,
                )
            # Re-raise so SQS actually retries (matches ExtractBook's and
            # SynthesizeChunk's posture for the same class of failure).
            raise exc

        # Last attempt: convert transient into a *degraded but terminal*
        # outcome rather than letting the message die in the DLQ with the
        # book wedged in STITCHING forever. The manifest still lists every
        # DONE chunk with its own audioKey, so phase 6 can play the book
        # chunk by chunk -- degradation is a data property, not a special
        # case (§1 invariant 3).
        logger.warning(
            "stitch for book %s exhausted %d attempt(s), degrading to PARTIAL/%s: %s",
            command.book_id,
            self._max_attempts,
            StitchFailure.STITCH_FAILED.value,
            exc,
        )
        done = [c for c in chunks if c.status is ChunkStatus.DONE and c.audio_key]
        entries = [
            SegmentInput(
                index=chunk.index,
                # Fall back to the stored per-chunk duration: no bytes were
                # (or could be) measured. No byte_len -- there is no
                # concatenated file to have offsets into.
                duration_seconds=chunk.duration_ms / 1000,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                audio_key=chunk.audio_key or "",
                marks_key=chunk.marks_key,
            )
            for chunk in done
        ]
        segments, total_ms = plan_segments(entries)
        return _StitchPlan(
            segments=segments,
            missing=sorted(c.index for c in chunks if c not in done),
            total_ms=total_ms,
            sample_rate_hz=None,
            audio_key=None,
            stitch_failed=True,
        )

    # --- the manifest + the terminal transition -----------------------------

    def _finish(self, command: StitchBookCommand, book, plan: _StitchPlan) -> StitchBookResult:
        if plan.stitch_failed:
            final_status = BookStatus.PARTIAL
            failure_reason: str | None = StitchFailure.STITCH_FAILED.value
        elif not plan.segments:
            # Every chunk failed synthesis. The text is still fully readable
            # -- this is the deterministic steady state of local dev and
            # every PR stack (§3.1), not an error.
            final_status = BookStatus.PARTIAL
            failure_reason = StitchFailure.NO_AUDIO.value
        elif plan.missing or book.chunks_failed:
            final_status = BookStatus.PARTIAL
            failure_reason = None
        else:
            final_status = BookStatus.READY
            failure_reason = None

        manifest_key = book_manifest_key(command.user_id, command.book_id)
        document = build_book_manifest(
            book=book,
            status=final_status,
            segments=plan.segments,
            missing=plan.missing,
            book_audio_key=plan.audio_key,
            total_ms=plan.total_ms,
            sample_rate_hz=plan.sample_rate_hz,
        )
        try:
            self._marks_storage.put_bytes(
                key=manifest_key,
                data=json.dumps(document, separators=(",", ":")).encode("utf-8"),
                content_type=MARKS_CONTENT_TYPE,
            )
        except Exception:  # noqa: BLE001
            if not plan.stitch_failed:
                raise
            # Already on the degraded last-attempt path: raising here would
            # DLQ the message and leave the book stuck in STITCHING, which is
            # strictly worse than a terminal book with no manifest.
            logger.exception("could not write the book manifest for %s", command.book_id)
            manifest_key = None

        try:
            self._book_repository.update_status(
                command.user_id,
                command.book_id,
                final_status,
                expected_statuses=(BookStatus.STITCHING,),
                audio_key=plan.audio_key,
                manifest_key=manifest_key,
                audio_duration_ms=plan.total_ms,
                failure_reason=failure_reason,
                clear_failure_reason=failure_reason is None,
                updated_at=self._clock.now().isoformat(),
            )
        except ConflictError:
            # A concurrent duplicate already completed. Its writes were
            # byte-identical (every key is a pure function of the ids), so
            # there is nothing to undo.
            return StitchBookResult("SKIPPED", reason="ALREADY_STITCHED")
        except NotFoundError:
            logger.warning("book %s was deleted mid-stitch", command.book_id)
            return StitchBookResult("SKIPPED", reason="BOOK_NOT_FOUND")

        return StitchBookResult(
            "READY" if final_status is BookStatus.READY else "PARTIAL",
            reason=failure_reason,
            segments=len(plan.segments),
            missing=len(plan.missing),
            duration_ms=plan.total_ms,
            audio_key=plan.audio_key,
            manifest_key=manifest_key,
        )


@dataclass(frozen=True)
class _StitchPlan:
    segments: list
    missing: list[int]
    total_ms: int
    sample_rate_hz: int | None
    audio_key: str | None
    stitch_failed: bool
