# Phase 5 — Stitching & status polling

Goal: when the last chunk of a book reaches a terminal state, exactly one invocation notices, publishes a single stitch message, and a new **stitch Lambda** concatenates every `DONE` chunk's MP3 into one `book.mp3`, writes a **book manifest** (`book.json`) that rebases each chunk's audio into book-global milliseconds, and flips the book to a terminal status — `READY` when every chunk synthesized, `PARTIAL` when any chunk did not. A new `GET /books/{id}/status` gives the phase-6 reader a single cheap poll with a server-computed `terminal` flag.

**Verified against the current repo (post `a5f8699`, phase 4 merged):**

- `ChunkCounters` (`domain/repository.py`) already exists with `chunks_done`/`chunks_total`/`chunks_failed` and an `is_complete` property whose docstring literally says *"phase 5's stitcher trigger uses `is_complete` with zero extra read"*. `DynamoDbBookRepository.increment_chunks_done` already uses `ReturnValues="ALL_NEW"` and coerces the `Decimal`s. **Nothing about the fan-in counter needs to change in this phase.**
- `SynthesizeChunk._increment` (`application/synthesis.py`) is the single call site, reached from **both** `_finish_done` and `_finish_failed`. A permanently failed chunk increments `chunksDone` *and* `chunksFailed` (phase-4 §5.4/Q6), so the completion edge fires in local/PR too, where every chunk fails.
- `BookStatus` (`domain/value_objects.py`) already declares `READY` with the comment `# first SET in phase 5 (stitcher)`. `STITCHING` and `PARTIAL` are **not** declared and must be added (§5.2).
- `infrastructure/mp3.py` has a pure, dependency-free frame-header scanner with the MPEG-2-vs-MPEG-1 `samples_per_frame` distinction already correct (576 for MPEG-2 Layer III, which is what edge-tts's `audio-24khz-48kbitrate-mono-mp3` emits) and a `_skip_id3v2` helper. It returns `round(total_seconds * 1000)` — §7.3 makes the float seconds available too, because rounding per segment before summing is exactly the accumulating-drift bug this phase must not ship.
- `infrastructure/s3_keys.py` already reserves room for this phase: its comment says *"phase 5 needs somewhere to put the stitched artifacts (`audio/<u>/<b>/full.mp3`, `marks/<u>/<b>/full.json`) without colliding with a chunk index"*. `_AUDIO_KEY_RE`/`_MARKS_KEY_RE` require exactly `CHUNK_INDEX_WIDTH` digits, so a book-level filename cannot be mis-parsed as a chunk key. (§5.1 renames `full.*` → `book.*` and says why.)
- `infrastructure/sqs_synthesis_queue.py`'s `_sqs` property is **lazy** with a docstring explaining that an eager client raises `NoRegionError` in the credential-free backend test job. The new stitch-queue adapter copies that property verbatim (constraint: the backend test job has no region and no credentials).
- `infra/stacks/pipeline_stack.py`'s `_add_synthesize_lambda` carries a long "NO `reserved_concurrent_executions` here, deliberately — do not restore it" comment, and `infra/tests/test_synth.py::test_pipeline_stack_synthesize_function_shape` asserts `Match.absent()`. The stitch Lambda gets the same treatment.
- `_queue_with_dlq` already takes `max_receive_count`. `table` is threaded into `PipelineStack` as a **real construct**, not a name — so no re-import gymnastics are needed for anything table-shaped (§4.1 analyses what that means for the rejected DynamoDB-Stream option).
- `application/use_cases.py`'s `_REISSUABLE_STATUSES` and `application/extraction.py`'s `_CLAIMABLE_STATUSES` are both `(UPLOADED, FAILED)`. This is load-bearing for §3's central decision: **a book with no audio must not be given book-status `FAILED`**, because `FAILED` is re-claimable and a redelivered extract message would wipe and re-extract a perfectly good book.

---

## 1. End-to-end flow

```
synthesize Lambda (xN)                      SQS stitch_queue    stitch Lambda (x1)          S3 / DynamoDB
  | update_status(chunk -> DONE|FAILED, conditional)  ------------------------------------------>| chunk
  | counters = increment_chunks_done(failed=?)   <-- ALL_NEW ------------------------------------| book
  | if counters.is_complete:  enqueue_book(user, book)
  |----------------------------------------------->|
  |   (also: duplicate delivery of an already-DONE  |
  |    chunk on a complete-but-EXTRACTED book       |
  |    re-publishes -- the STITCH_REQUEUED branch)  |
                                                   |-- batch_size=1, max_concurrency=2 -->|
                                                   |                                      | get(book) -> EXTRACTED
                                                   |                                      | claim: EXTRACTED|STITCHING -> STITCHING
                                                   |                                      | list_for_book -> DONE chunks
                                                   |                                      | for each: get audio/<u>/<b>/00000N.mp3
                                                   |                                      |   strip ID3v2 + Xing, measure, append
                                                   |                                      | multipart put audio/<u>/<b>/book.mp3 -->| audio_bucket
                                                   |                                      | put marks/<u>/<b>/book.json ----------->| marks_bucket
                                                   |                                      | update_status(READY|PARTIAL,
                                                   |                                      |   expected=STITCHING, audioKey,
                                                   |                                      |   manifestKey, audioDurationMs)
                                                   |                                      |   -- ConflictError? -> SKIPPED (dedupe)
```

Three invariants the phase rests on:

1. **Exactly one increment observes `is_complete` for the first time.** `ADD chunksDone :one` is atomic and, by phase-4 §8.4, each chunk increments at most once — so exactly one post-update value equals `chunks_total`. The stitch publish hangs off that single observation, needing no extra read and no lock (§4.2).
2. **The publish is *not* the only path to a stitch.** The invocation that observed completion can die between the increment and the publish. That is covered by a re-entrancy branch modelled directly on phase-4 §4.3's `REQUEUED`: a redelivered chunk message for an already-`DONE` chunk on a complete-but-still-`EXTRACTED` book re-publishes and returns `STITCH_REQUEUED` (§4.3). Duplicate stitch messages are harmless by construction (§6.4).
3. **The manifest, not the audio file, is the timeline.** `book.mp3` is a concatenation *optimization*; every segment in `book.json` also carries its own per-chunk `audioKey`, so a book whose stitch failed — or whose chunks partly failed — is still fully playable and fully highlightable from per-chunk artifacts (§7.2). This is what makes "graceful degradation" a data-model property rather than a frontend heroic.

---

## 2. Decisions up front (details in the referenced sections)

| # | Question | Decision | § |
|---|---|---|---|
| Q1 | Stitcher trigger mechanism | **Final-chunk check → dedicated `stitch_queue`.** The winning `increment_chunks_done` (the one whose `ChunkCounters.is_complete` flips true) publishes one message; a new `StitchFunction` consumes it. **Not** DynamoDB Streams (§4.1 shows Lambda ESM filtering structurally *cannot* express `chunksDone == chunksTotal`, so a stream fires N times per book), **not** inline stitching in the synthesize Lambda (§4.1). | §4 |
| Q2 | What if the publish is lost? | New pre-claim `STITCH_REQUEUED` branch in `SynthesizeChunk`: a redelivered message for an already-`DONE` chunk, on a book that is complete and still `EXTRACTED`, re-publishes only. Exactly phase-4 §4.3's `REQUEUED` shape. | §4.3 |
| Q3 | **Book status when chunks failed** | **`READY` iff `chunksFailed == 0`; otherwise `PARTIAL` — never book-level `FAILED` from the stitcher.** `PARTIAL` + `failureReason` (`NO_AUDIO` when *every* chunk failed, `STITCH_FAILED` when concatenation itself gave up) is the graceful-degradation contract phase-4 §0 promised the UI. Book-level `FAILED` is reserved for extraction, and reusing it here would make the book re-claimable by `ExtractBook` (`_CLAIMABLE_STATUSES`) and wipe good text. | §3 |
| Q4 | Book status while stitching | **New `STITCHING`.** Claim is `EXTRACTED\|STITCHING → STITCHING`, i.e. deliberately re-claimable (a hard-crashed stitcher leaves no lease to expire — the identical reasoning as phase-4 §8.2 step 5 for `SYNTHESIZING`). Terminal transition is conditional on `STITCHING`, and *that* is the exactly-once gate. | §6.4 |
| Q5 | Audio concatenation approach | **Raw MPEG frame concatenation, no re-encoding.** No ffmpeg/lame/pydub in the image. Every segment gets a leading ID3v2 block and a leading Xing/Info/VBRI metadata frame stripped before appending, so decoders see one clean frame stream. | §7.1 |
| Q6 | Does a big book fit in a Lambda? | **Not if buffered whole** (a 300-page book is ~330 chunks × ~720 KB ≈ 240 MB, doubled by the final `bytes()` copy). So: **S3 multipart upload with a bounded 5 MiB buffer**, memory O(part), not O(book). New `MultipartWriter` port + `S3MultipartWriter` adapter. `memory_size=1536`, `timeout=900`. | §6.3, §7.4 |
| Q7 | Offset math / merged artefact | **A segment manifest, not a merged word array.** `marks/<u>/<b>/book.json` maps `chunkIndex → (bookGlobalStartMs, durationMs, bookGlobalCharStart/End, byteStart/byteEnd, audioKey, marksKey)`. Phase 6 rebases a word with one addition: `t_global = segment.t + word.t`. A merged word array for a 330-chunk book is ~4 MB of JSON to download before the first note plays — rejected. This **refines** `IMPLEMENTATION_PLAN.md`'s "merges timestamp offsets": it merges them into *offsets*, not into one array. | §7.2 |
| Q8 | Rounding | Durations accumulate as **float seconds**, and each boundary is rounded **once**: `t_next = round(cum_seconds * 1000)`, `d = t_next - t`. So `sum(d) == durationMs` exactly and segment boundaries partition the timeline with no gap or overlap. Summing per-segment rounded ints would drift up to ±165 ms over 330 chunks. `mp3.py` grows `mp3_duration_seconds`; `mp3_duration_ms` becomes `round(... * 1000)` over it — zero behaviour change. | §7.3 |
| Q9 | Durations: stored or recomputed? | **Recomputed from the bytes actually being concatenated** (after stripping), with a `WARNING` when it disagrees with the chunk item's `durationMs` by >50 ms. The manifest's `t` values are the only thing between the user and a drifting highlight; deriving them from the served bytes is the only self-consistent choice, and it catches a half-written object for free. | §7.3 |
| Q10 | Missing chunks: silence or skip? | **Skip.** Failed chunks are simply absent from `segments` and listed in `missing`. Fabricating minutes of synthesized silence reads to a user as "playback froze", inflates the file, and requires generating valid frames for no benefit. | §7.2 |
| Q11 | `GET /books/{id}/status` — warranted? | **Yes, narrowly.** It is not a subset of `GET /books/{id}` — it adds two server-computed fields the poller would otherwise have to derive: `terminal` (`status in {READY, PARTIAL, FAILED}`) and `progress.percent`. Without `terminal` the client's stop condition is `status == "READY"`, which hangs forever on a `PARTIAL` book — the exact "frozen at 99%" failure mode phase-4 §8.3 spent a section eliminating. It performs **no extra I/O** (one `GetItem`, same as `GET /books/{id}`). | §8 |
| Q12 | Stitch queue vs reuse `synthesize_queue` | **Third queue.** Visibility 90 min (6× the 900 s timeout, the repo's standing rule), `max_receive_count=3`, ESM `batch_size=1`, `max_concurrency=2`. A discriminated message type on the synthesize queue would let one poison stitch message consume the *chunk* retry budget, and the two workloads have opposite shapes (one long invocation per book vs many short ones). | §6.2 |
| Q13 | Reserved concurrency | **None. Do not add it.** Same account-quota wall as phase-4 §6.3's correction. ESM `max_concurrency` is the only throttle. Infra test asserts `Match.absent()`. | §6.3 |
| Q14 | Account concurrency budget | The account ceiling is **10 total**. With `SYNTHESIZE_MAX_CONCURRENCY=5` + `STITCH_MAX_CONCURRENCY=2` + extract + API + the SSR Lambda, a big book can starve the *user-facing API*. Recommendation: drop synthesize 5 → 3 (see OQ-2). | §6.3 |
| Q15 | New bounded context? | **No.** Stitching produces the Library's own `Book`/`Chunk` artifacts. Same answer as phase-3 OQ-1 and phase-4 Q19. | §3.1 |
| Q16 | Presigned GET for `book.mp3` | **Out of scope — phase 6.** The book row now advertises `audioKey`; turning that into a playable URL is the reader UI's job and needs decisions (CloudFront vs presigned, Range support) this phase has no way to validate. | OQ-3 |
| Q17 | Smoke-test coverage of real concatenation | Constraint: *no* environment CI can reach ever produces audio (phase-4 §0). Baseline smoke asserts the **whole stitch path minus the bytes**: `PARTIAL`/`NO_AUDIO`, zero segments, a real `book.json` written to `marks_bucket`, `terminal == true`. The byte math is carried entirely by unit tests over synthetic MPEG-2 frames. **Optional** `SilentSynthesizer` (OQ-1) closes the remaining gap locally. | §9.3, §10 |
| Q18 | Frontend | **Out of scope** — phase 6. | — |

---

## 3. The central decision: what "stitch" means when chunks failed

### 3.1 Why this is the most consequential call in the phase

Phase-4 §0 made a rule that reaches much further than it looks: `get_speech_synthesizer()` gates on `settings.environment != "prod"` **first and unconditionally**, so local dev and *every* ephemeral PR stack get a `StubSynthesizer` that raises a permanent `SynthesisDisabled`. Consequence, stated plainly because the whole phase must be designed around it:

> **In every environment any automated check can reach, `chunksFailed == chunksTotal`, no chunk is ever `DONE`, and no `audio/` or `marks/` object is ever written.** The stitcher will essentially never have real audio to stitch outside prod.

If "all chunks failed" mapped to book status `FAILED`, then the *normal, expected, everyday* state of every local and PR book would be `FAILED` — a status that (a) is what extraction sets when the PDF is corrupt or has no text layer, and (b) is in **both** `_CLAIMABLE_STATUSES` (`application/extraction.py`) and `_REISSUABLE_STATUSES` (`application/use_cases.py`). A stray redelivered S3 event would therefore claim the book, `delete_for_book`, and re-extract it — throwing away perfectly good text because the *audio* was unavailable. That is a real bug, not a theoretical one.

The user's recorded intent (phase-4 §0's frontend note) is: *"on the UI just show a simple message.. but allow the user to continue reading the book or whatever they can with no external service."* So:

### 3.2 Decision

| condition at stitch time | book status | `failureReason` | `audioKey` | manifest `segments` |
|---|---|---|---|---|
| `chunksFailed == 0` | **`READY`** | cleared | `audio/<u>/<b>/book.mp3` | every chunk |
| `0 < chunksFailed < chunksTotal` | **`PARTIAL`** | cleared | `audio/<u>/<b>/book.mp3` | the `DONE` chunks; the rest in `missing` |
| `chunksFailed == chunksTotal` (**local/PR steady state**) | **`PARTIAL`** | `NO_AUDIO` | `null` | `[]`; every index in `missing` |
| concatenation itself failed on the last SQS attempt | **`PARTIAL`** | `STITCH_FAILED` | `null` | the `DONE` chunks, `t` from stored `Chunk.duration_ms`, no `byteStart/End` |

**The stitcher never writes book status `FAILED`.** `BookStatus.FAILED` keeps exactly its phase-3 meaning: *extraction produced nothing usable*.

Two extra properties that fall out and are worth naming:

- **`PARTIAL` is terminal.** A polling client stops on `status in {READY, PARTIAL, FAILED}`; `GET /books/{id}/status` computes that as `terminal` so the client never encodes the set (§8).
- **The last row is not a dead end.** Even when concatenation fails, the manifest still lists each `DONE` chunk with its own `audioKey`/`marksKey`, so phase 6 can play chunk-by-chunk. Degradation is a data property, not a special case.

### 3.3 Why one new status and not two

Rejected: a third value distinguishing "some audio" from "no audio". `chunksFailed == chunksTotal` already says that exactly, in a field the client already receives, and every additional `BookStatus` value is another string the mapper, `BookStatus.parse`, the phase-6 sidebar and the rolling-deploy forward-compat story all have to carry. One new terminal status (`PARTIAL`) + one new transient status (`STITCHING`) is the minimum that makes the state machine honest.

**Accepted deploy-order risk, stated explicitly** (identical in shape to phase-3 OQ-5 and phase-4 §5.2): CDK orders `Storage → Pipeline → Api`, so the writer (stitch Lambda) deploys before the reader (API Lambda). A warm old API container reading a book whose status is `PARTIAL`/`STITCHING` gets `ValidationError` from `BookStatus.parse` → `400` — and unlike phase 4's chunk case this would fail the whole `GET /books` list, not one chunk. Mitigating facts: the window is the deploy gap between two stacks in one pipeline run; no book can *be* `PARTIAL` until the new stitcher has run, which requires a book to finish synthesizing inside that window; single user; no traffic during deploy. Accepted, not engineered around.

### 3.4 DDD placement — stays inside `contexts/library/`

Same argument as phase-3 OQ-1 and phase-4 §3: stitching mutates the Library's own `Book` aggregate and reads its own `Chunk`s. New/changed layout (⊕ new, Δ changed):

```
backend/src/contexts/library/
├── domain/
│   ├── value_objects.py            Δ  + BookStatus.STITCHING/.PARTIAL,
│   │                                  STITCHABLE_BOOK_STATUSES, StitchFailure
│   ├── book.py                     Δ  + audio_key, manifest_key, audio_duration_ms
│   ├── repository.py               Δ  BookRepository.update_status + audio_key/
│   │                                  manifest_key/audio_duration_ms/clear_stitch_outputs
│   ├── storage.py                  Δ  + MultipartWriter (Protocol),
│   │                                  ObjectStorage.open_multipart
│   └── stitching.py                ⊕  PURE: StitchSegment, plan_segments,
│                                      build_book_manifest, StitchQueue (Protocol),
│                                      MANIFEST_SCHEMA_VERSION
├── application/
│   ├── synthesis.py                Δ  + stitch_queue dep, _maybe_publish_stitch,
│   │                                  STITCH_REQUEUED branch
│   └── stitching.py                ⊕  StitchBook use case + command/result
├── infrastructure/
│   ├── mp3.py                      Δ  + mp3_duration_seconds, strip_container_headers
│   ├── s3_keys.py                  Δ  + book_audio_key/book_manifest_key + parsers
│   ├── s3_object_storage.py        Δ  + S3MultipartWriter, open_multipart
│   ├── sqs_stitch_queue.py         ⊕  SqsStitchQueue (lazy client -- constraint!)
│   ├── book_mapper.py              Δ  audioKey/manifestKey/audioDurationMs
│   └── dynamodb_book_repository.py Δ  update_status kwargs (§5.3)
└── interface/
    ├── dependencies.py             Δ  + get_stitch_queue
    ├── schemas.py                  Δ  + book_to_dict fields, book_status_to_dict
    ├── controllers.py              Δ  + GET /books/{book_id}/status
    ├── synthesize_handler.py       Δ  composition root gains stitch_queue
    ├── local_synthesize_worker.py  Δ  same
    ├── stitch_handler.py           ⊕  SQS Lambda handler
    └── local_stitch_worker.py      ⊕  local-dev SQS poll loop over the same handler
```

`domain/stitching.py` and the `mp3.py` additions are **pure** — no `boto3`, no network — for the same reason `domain/marks.py` and `infrastructure/mp3.py` already are: the offset math is the part phase 6's highlight sync depends on, and it is the part CI can never observe end to end (§3.1), so it must be exhaustively unit-testable.

---

## 4. The trigger: how the stitcher fires

### 4.1 Three candidates, analysed concretely

| option | verdict |
|---|---|
| **A. `ChunkCounters.is_complete` at the increment site → dedicated `stitch_queue` → new Lambda** | **Chosen.** |
| B. DynamoDB Stream on the table, filtered, driving the stitch Lambda | **Rejected.** See below — the decisive point is that the filter cannot be written. |
| C. Stitch inline, in the synthesize invocation that observed completion | **Rejected.** See below. |

**Why not DynamoDB Streams (B).** The CDK mechanics are actually *fine* — worth stating, because phase-4 §4.1 rejected streams partly on infrastructure grounds and that half of the argument no longer holds:

- The circular-dependency trap (phase-3 §6.1, re-imported `pdf_bucket`) **does not apply**. A stream event source is one `AWS::Lambda::EventSourceMapping` with `EventSourceArn: <table StreamArn>` plus `dynamodb:GetRecords/GetShardIterator/DescribeStream` on the Lambda's role. Nothing is attached to the table. `table` is already threaded into `PipelineStack` as a real construct, so `table.grant_stream_read(fn)` + `DynamoEventSource(table, ...)` would land entirely in `PipelineStack`, leaving the existing single `Pipeline → Storage` direction. Only `StorageStack` would change, adding `stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES` — and that is a **table replacement-free** in-place update.

What kills it is behaviour, not topology:

1. **Lambda event-source filtering cannot compare two attributes.** Filter patterns match literal values, prefixes and numeric ranges against *fixed* JSON paths. There is no way to express `dynamodb.NewImage.chunksDone.N == dynamodb.NewImage.chunksTotal.N`. So the stitch Lambda would be invoked on **every** counter increment — N invocations per book instead of one — each of which must read, compare, and de-duplicate. For a 330-chunk book that is 330 invocations of the memory-heaviest function in the system, against an account whose *total* concurrency is 10 (§6.3).
2. **The information is already in hand, for free.** `increment_chunks_done` returns `ChunkCounters` off `ReturnValues="ALL_NEW"` in the same round trip, precisely so the caller can answer "was I last?" with zero extra read. Re-deriving that fact from a stream is strictly more machinery for strictly less certainty.
3. **The local story stays honest.** Every async worker in this repo has a `local_*_worker.py` that polls SQS in a compose service, because LocalStack community cannot run our container-image Lambdas. A stream consumer would need `describe_stream` / `get_shard_iterator` / `get_records` with shard lifecycle handling — a genuinely different and much larger local worker. Phase-3 §6.1 chose S3→SQS over EventBridge for the same reason; phase-4 §4.1 chose the same for the fan-out. Consistency here is worth real money.
4. **Cost/ordering.** Streams are per-partition-key ordered, which we do not need (there is exactly one relevant item), and cost per read-request-unit for records we would discard 329 times out of 330.

Streams remain the right answer if a *later* phase needs table-wide change capture (e.g. phase 8's "mark stranded books FAILED" sweeper). This phase does not.

**Why not inline stitching (C).** Four independent reasons:

1. **Resource profile.** The synthesize Lambda is 1024 MB / 300 s, tuned for one websocket call. Stitching means ~330 `GetObject`s and a 240 MB multipart upload. One unlucky chunk invocation would need a completely different envelope from its 329 identical siblings.
2. **IAM widening.** `pipeline_stack.py` deliberately grants the synthesize function `audio_bucket.grant_put` — *not* `grant_read_write` — with the comment "the synthesize Lambda never reads or deletes audio/marks". Inline stitching would force `grant_read` onto the highest-concurrency function in the system.
3. **Retries have nowhere to go — the decisive one.** If inline stitching fails transiently and we re-raise, SQS redelivers the *chunk* message; `SynthesizeChunk.execute` sees `chunk.status is DONE` and returns `SKIPPED("ALREADY_DONE")` without stitching. The retry is silently swallowed and the book is stuck at `EXTRACTED` forever. A separate queue gets its own visibility timeout, its own `maxReceiveCount`, and its own DLQ.
4. **Testability.** A separate use case is directly unit-testable; a branch inside `SynthesizeChunk` that only fires on the Nth call is not.

### 4.2 Where the publish goes in `SynthesizeChunk`

`application/synthesis.py` gains one dependency and one helper. Both `_finish_done` and `_finish_failed` already funnel through `_increment`, and **both** must publish — in local/PR the completing increment is always a *failed* one, so publishing only from the success path would mean the stitcher never fires anywhere CI can see it.

```python
class SynthesizeChunk:
    def __init__(self, book_repository, chunk_repository, synthesizer,
                 audio_storage, marks_storage,
                 stitch_queue: StitchQueue,          # NEW
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None: ...

    def _maybe_publish_stitch(self, command, counters: ChunkCounters | None) -> None:
        """Fan-in edge (PLANS/phase-5.md §4.2). `increment_chunks_done` is an
        atomic ADD returning ALL_NEW, and phase-4 §8.4 guarantees each chunk
        increments at most once -- so exactly one invocation ever observes
        chunks_done == chunks_total. That single observation is the trigger.
        A raise here is deliberately NOT swallowed: the SQS message stays
        undeleted, and the redelivery hits execute()'s STITCH_REQUEUED
        branch (§4.3)."""
        if counters is None or not counters.is_complete:
            return
        self._stitch_queue.enqueue_book(user_id=command.user_id, book_id=command.book_id)
```

called at the end of `_increment` (its single call site keeps both terminal paths honest), *after* the `logger.info` and only when `counters is not None` (a book deleted mid-synthesis has nothing to stitch).

Note `is_complete` is `chunks_total > 0 and chunks_done >= chunks_total` — a `>=`, so in principle two invocations could both see it true if `chunksDone` overshot. The only way to overshoot is a re-extraction resetting `chunksDone`/`chunksTotal` to 0 underneath in-flight synthesizers. That is already a known hazard, and it is why the stitcher's own conditional claim (§6.4), not this check, is the correctness gate. Duplicate stitch messages are safe.

### 4.3 The `STITCH_REQUEUED` re-entrancy branch

The failure mode: the invocation that observed completion increments the counter, then dies (OOM, hard timeout, evicted) before `enqueue_book` returns. Nothing else will ever notice — the book sits at `EXTRACTED` with `chunksDone == chunksTotal`, which is exactly the "progress bar frozen at 100% with nothing in the API to explain it" state phase-4 §8.3 refused to ship.

Recovery uses the machinery already present: the crashed invocation never deleted its SQS message, so it is redelivered. `SynthesizeChunk.execute`'s existing first branch short-circuits `DONE` chunks — extend it:

```python
chunk = self._chunk_repository.get(command.book_id, command.chunk_index)
if chunk is None:
    return SynthesizeChunkResult("SKIPPED", reason="CHUNK_NOT_FOUND")
if chunk.status is ChunkStatus.DONE:
    # A redelivery of an already-DONE chunk means the previous invocation's
    # stitch publish is not known to have completed (it is the only step
    # after the counter increment that can fail). Re-publish; never
    # re-synthesize. Exactly PLANS/phase-4.md §4.3's REQUEUED shape, and
    # duplicate stitch messages are safe by construction (§6.4's claim).
    if self._republish_stitch_if_complete(command):
        return SynthesizeChunkResult("SKIPPED", reason="STITCH_REQUEUED")
    return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")
```

```python
def _republish_stitch_if_complete(self, command) -> bool:
    book = self._book_repository.get(command.user_id, command.book_id)
    # BookStatus.EXTRACTED only: a book already STITCHING/READY/PARTIAL has
    # either been picked up or finished, and re-publishing would queue
    # pointless work behind the stitcher's conditional claim.
    if book is None or book.status is not BookStatus.EXTRACTED:
        return False
    if book.chunks_total == 0 or book.chunks_done < book.chunks_total:
        return False
    self._stitch_queue.enqueue_book(user_id=command.user_id, book_id=command.book_id)
    return True
```

Cost: one extra `GetItem`, and only on duplicate deliveries of already-`DONE` chunks. This branch gets its own tests (§10) including the negative ones — a `READY`/`PARTIAL`/`STITCHING` book must **not** be re-published.

**Residual hole, named honestly.** If `enqueue_book` keeps failing across all 5 chunk-message attempts, the chunk message DLQs and the book stays `EXTRACTED` forever. Phase 4 eliminated per-chunk DLQ residue via the last-attempt rule; there is no equivalent trick here, because the last-attempt conversion (`FAILED`) has already happened for that chunk. The backstop is phase 8's already-deferred **DLQ-consuming Lambda that marks stranded books** (`IMPLEMENTATION_PLAN.md` phase 8, inherited from phase-3 OQ-4) — this phase adds "and re-publishes a stitch for books whose counters are complete" to that item's description rather than inventing a second sweeper here. See OQ-4.

### 4.4 `StitchQueue` port + adapter

```python
# domain/stitching.py
class StitchQueue(Protocol):
    """The fan-in trigger port -- SynthesizeChunk depends on this, not on
    boto3/SQS. Mirrors domain/synthesis.py's SynthesisQueue."""
    def enqueue_book(self, *, user_id: str, book_id: str) -> None: ...  # pragma: no cover
```

```python
# infrastructure/sqs_stitch_queue.py
STITCH_MESSAGE_VERSION = 1

class SqsStitchQueue:
    def __init__(self, *, queue_url: str, sqs: Any | None = None) -> None: ...
    @property
    def _sqs(self) -> Any: ...    # LAZY -- see below
    def enqueue_book(self, *, user_id: str, book_id: str) -> None: ...
```

Message body: `{"v": 1, "userId": "<sub>", "bookId": "<uuid>"}`.

**The `_sqs` property must be lazy, copied verbatim from `sqs_synthesis_queue.py`** including its docstring. That docstring records a real CI break: unlike S3, SQS has no global endpoint, so an eagerly constructed client makes the provider raise `NoRegionError` in the credential-free, region-free backend test job. A single `send_message` per book, so no batching, no partial-failure handling.

---

## 5. Data model changes

### 5.1 S3 key layout — additions to `infrastructure/s3_keys.py`

```python
BOOK_AUDIO_FILENAME = "book.mp3"
BOOK_MANIFEST_FILENAME = "book.json"

def book_audio_key(user_id: str, book_id: str) -> str        # audio/<u>/<b>/book.mp3
def book_manifest_key(user_id: str, book_id: str) -> str     # marks/<u>/<b>/book.json
def parse_book_audio_key(key: str) -> tuple[str, str] | None
def parse_book_manifest_key(key: str) -> tuple[str, str] | None
```

- **Deviation from phase-4 §5.1, flagged deliberately:** that section reserved the names `full.mp3` / `full.json`. Renamed to `book.mp3` / `book.json` because "full" is actively misleading for the `PARTIAL` case, which §3 makes a first-class, everyday outcome rather than an exception. Nothing was ever written under `full.*`, so this is a rename on paper only.
- No collision risk: `_AUDIO_KEY_RE`/`_MARKS_KEY_RE` require exactly `CHUNK_INDEX_WIDTH` digits before the extension, so `parse_chunk_audio_key("audio/u/b/book.mp3")` returns `None` and vice versa. A round-trip + cross-rejection test goes into the existing `test_s3_keys.py`.
- Content types reuse the existing `AUDIO_CONTENT_TYPE` / `MARKS_CONTENT_TYPE`.
- Still **not** duplicated into `infra/stacks/config.py` — nothing in CDK filters on these prefixes.

### 5.2 Domain changes — `domain/value_objects.py`

```python
class BookStatus(StrEnum):
    UPLOADED   = "UPLOADED"
    EXTRACTING = "EXTRACTING"
    EXTRACTED  = "EXTRACTED"
    STITCHING  = "STITCHING"   # first SET in phase 5 (stitch Lambda claim)
    READY      = "READY"       # first SET in phase 5 (stitcher, chunksFailed == 0)
    PARTIAL    = "PARTIAL"     # first SET in phase 5 (stitcher, chunksFailed > 0)
    FAILED     = "FAILED"      # extraction only -- the stitcher NEVER sets this (§3)
```

```python
# The set from which the stitch Lambda's claim -> STITCHING is legal.
# Deliberately includes STITCHING itself: a stitcher that died hard leaves the
# book in STITCHING with nothing to release it and no lease to expire --
# exactly NON_TERMINAL_CHUNK_STATUSES' reasoning (PLANS/phase-4.md §5.3).
# Allowing the re-claim costs at worst one duplicated concatenation (whose
# writes are idempotent, keyed purely on (user, book)); the terminal
# transition's own conditional is what makes the outcome exactly-once.
STITCHABLE_BOOK_STATUSES = (BookStatus.EXTRACTED, BookStatus.STITCHING)

# The closed set the polling client stops on (interface/schemas.py's
# `terminal` flag) -- kept here so "is this book done changing?" is one
# named concept rather than a status list smeared across the API and the
# frontend.
TERMINAL_BOOK_STATUSES = (BookStatus.READY, BookStatus.PARTIAL, BookStatus.FAILED)


class StitchFailure(StrEnum):
    """Why a book reached PARTIAL rather than READY -- stored as
    Book.failure_reason when status == PARTIAL (PLANS/phase-5.md §3.2)."""
    NO_AUDIO      = "NO_AUDIO"       # every chunk failed synthesis; text still readable.
                                     # The steady state of every non-prod env (phase-4 §0).
    STITCH_FAILED = "STITCH_FAILED"  # concatenation exhausted its SQS attempts; per-chunk
                                     # audio may still exist and is listed in the manifest.
```

`Book` gains:

| field | type | notes |
|---|---|---|
| `audio_key` | `str \| None = None` | the stitched `book.mp3`; `None` when nothing was concatenated |
| `manifest_key` | `str \| None = None` | `book.json`; set on every successful stitch, including the zero-segment one |
| `audio_duration_ms` | `int = 0` | total stitched duration; `sum(segment.d)` exactly (§7.3) |

`Book.create` leaves all three at their defaults. All defaults keep every phase-2/3/4 fixture and both mappers compiling (`item_to_book` already uses `.get` throughout).

**`Book.failure_reason` semantics widen, and the docstring must say so:** it now means *"why this book is not fully usable"* — an `ExtractionFailure` value when `status == FAILED`, a `StitchFailure` value when `status == PARTIAL`, `None` otherwise. One field with a documented discriminator beats a second near-identical field.

### 5.3 `BookRepository.update_status` — additive, same pattern as phase 4

```python
def update_status(
    self, user_id: str, book_id: str, status: BookStatus, *,
    expected_statuses: Sequence[BookStatus] | None = None,
    chunks_total: int | None = None,
    chunks_done: int | None = None,
    chunks_failed: int | None = None,
    page_count: int | None = None,
    audio_key: str | None = None,            # NEW
    manifest_key: str | None = None,         # NEW
    audio_duration_ms: int | None = None,    # NEW
    clear_stitch_outputs: bool = False,      # NEW
    failure_reason: str | None = None,
    clear_failure_reason: bool = False,
    updated_at: str | None = None,
) -> None: ...
```

Adapter notes — a line-for-line continuation of the existing implementation, no restructuring:

- Three more `if x is not None: set_clauses.append(...)` blocks. Nothing else changes about `SET`.
- `clear_stitch_outputs` appends `audioKey, manifestKey` to the `REMOVE` clause and `SET audioDurationMs = :zero`. Combining `REMOVE` with `SET` in one expression is already done for `clear_failure_reason`; the two `REMOVE` sets must be **merged into a single `REMOVE`** rather than emitting the keyword twice (a `ValidationException` at runtime, and a very easy thing to get wrong — refactor the tail into a `remove_names: list[str]`, and give it a test that sets `clear_failure_reason=True` *and* `clear_stitch_outputs=True` together).
- `audio_key is not None and clear_stitch_outputs` → `ValueError`, mirroring the existing `failure_reason`/`clear_failure_reason` guard.
- `ConditionExpression`, the `ConditionalCheckFailedException` disambiguation (`NotFoundError` vs `ConflictError`), and the `#status` alias are **unchanged** — this is exactly the machinery §6.4's claim needs and it already works.

The only new *caller* outside the stitcher is `ExtractBook._extract_and_persist`'s `EXTRACTED` flip, which adds `clear_stitch_outputs=True` alongside its existing `chunks_done=0, chunks_failed=0`. Same reason phase-4 §5.4 added the counter resets: re-extracting a previously-stitched book must not leave it advertising a stale `audioKey`/`manifestKey` pointing at audio for text that no longer exists.

`increment_chunks_done` and `ChunkCounters` are **unchanged**. `ChunkRepository` is **unchanged**.

### 5.4 Item shapes (additive only)

Book item gains `audioKey` (S), `manifestKey` (S), `audioDurationMs` (N). `audioKey`/`manifestKey` are **absent** rather than `NULL` when unset — hence `REMOVE`, matching `failureReason`'s existing treatment. `book_to_item` writes them only when not `None`; `item_to_book` reads with `.get`.

---

## 6. Infrastructure (CDK)

### 6.1 `PipelineStack` signature

```python
def __init__(self, scope, construct_id, *, environment: str,
             pdf_bucket_name: str,
             audio_bucket: s3.IBucket,
             marks_bucket: s3.IBucket,
             table: dynamodb.Table,
             google_tts_secret_name: str = "",
             git_sha: str = "local", **kwargs) -> None
```

**Unchanged.** Everything the stitch Lambda needs is already threaded in. `infra/app.py` needs **no change at all** for the pipeline (the only `app.py` edit in this phase is none — worth stating so nobody goes looking). `storage_stack.py` and `api_stack.py` are likewise untouched: `api_stack.py` already sets `AUDIO_BUCKET`/`MARKS_BUCKET` and grants `grant_read_write` on both, which is all the status endpoint needs (and it needs even that only in phase 6).

### 6.2 The stitch queue

```python
self.stitch_queue = self._queue_with_dlq(
    "Stitch", queue_name("stitch", environment),
    # 6x the stitch Lambda's 900s timeout, the same rule extract_queue and
    # synthesize_queue already follow -- a shorter visibility timeout would
    # hand the same book to a second invocation mid-concatenation. The
    # §6.4 claim would catch it, but the queue should be right on its own.
    visibility_timeout=cdk.Duration.minutes(90),
    # 3, not 5: unlike the synthesize queue there is no ESM-throttle
    # backpressure to burn attempts (max_concurrency 2 with one message per
    # book means the poller essentially never throttles), and each attempt
    # costs up to 15 minutes.
    max_receive_count=Config.STITCH_MAX_RECEIVE_COUNT,
)
cdk.CfnOutput(self, "StitchQueueUrl", value=self.stitch_queue.queue_url)
```

Plus, alongside the existing extract → synthesize grant:

```python
self.stitch_queue.grant_send_messages(synthesize_fn)
synthesize_fn.add_environment(Config.ENV_STITCH_QUEUE_URL, self.stitch_queue.queue_url)
```

This is this phase's easy-to-forget grant, exactly as `synthesize_queue.grant_send_messages(extract_fn)` was phase 4's. It requires `_add_synthesize_lambda` to **return** `synthesize_fn` (today it returns `None` and only sets `self.synthesize_function`) — a two-line change, or just use `self.synthesize_function`.

### 6.3 The stitch Lambda

```python
stitch_fn = lambda_.DockerImageFunction(
    self, "StitchFunction",
    code=lambda_.DockerImageCode.from_image_asset(
        backend_dir,
        cmd=["src.contexts.library.interface.stitch_handler.handler"],
    ),
    # Fourth function, same image -- DockerImageAsset's hash comes from the
    # source dir + build args + platform, NOT from `cmd`. One ECR image,
    # four Lambdas, four ImageConfig.Commands.
    #
    # 1536 MB is NOT sized for the whole book: §7.4's multipart writer keeps
    # resident bytes at O(one 5 MiB part + one ~720 KB segment). The memory
    # buys CPU for mp3_duration_seconds' frame scan (~1.3M frames for a
    # 300-page book) and network bandwidth for ~330 GetObjects.
    memory_size=1536,
    # 900s = the Lambda maximum. Budget for a 330-chunk book: ~330 GETs at
    # ~100ms plus ~48 UploadParts plus the frame scan -- comfortably inside,
    # but there is no larger value available if it ever isn't (see OQ-5).
    timeout=cdk.Duration.seconds(900),
    # NO reserved_concurrent_executions -- do not "restore" it. Identical
    # account-quota wall as SynthesizeFunction above: this account's total
    # Lambda concurrency is 10 and AWS rejects any reservation that drops
    # unreserved concurrency below its floor of 10. The ESM's
    # max_concurrency below is the only throttle.
    environment={
        Config.ENV_ENVIRONMENT: environment,
        Config.ENV_GIT_SHA: git_sha,
        Config.ENV_TABLE_NAME: table.table_name,
        Config.ENV_AUDIO_BUCKET: audio_bucket.bucket_name,
        Config.ENV_MARKS_BUCKET: marks_bucket.bucket_name,
        Config.ENV_STITCH_QUEUE_URL: self.stitch_queue.queue_url,
        Config.ENV_STITCH_MAX_RECEIVE_COUNT: str(Config.STITCH_MAX_RECEIVE_COUNT),
        Config.ENV_LOG_LEVEL: "INFO",
    },
)

audio_bucket.grant_read(stitch_fn)   # GetObject on every chunk MP3
audio_bucket.grant_put(stitch_fn)    # PutObject + s3:Abort* (multipart) for book.mp3
marks_bucket.grant_put(stitch_fn)    # book.json
table.grant_read_write_data(stitch_fn)   # GetItem/Query (chunks) + UpdateItem (claim, terminal)

stitch_fn.add_event_source(
    lambda_event_sources.SqsEventSource(
        self.stitch_queue,
        batch_size=1,
        max_concurrency=Config.STITCH_MAX_CONCURRENCY,   # 2 = the ESM minimum
    )
)
cdk.CfnOutput(self, "StitchFunctionName", value=stitch_fn.function_name)
```

**On `grant_put` and multipart:** CDK's `grant_put` maps to `s3:PutObject*` plus `s3:Abort*`, which together cover `CreateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload` and `AbortMultipartUpload`. No hand-written policy document. (`grant_read` is separate and genuinely needed — this is the first function that *reads* `audio_bucket`.)

**The account concurrency arithmetic, written down for the first time.** The account ceiling is 10 concurrent executions *in total*. After this phase there are six functions competing for it: `ApiFunction`, `ExtractFunction`, `SynthesizeFunction` (ESM-capped at `SYNTHESIZE_MAX_CONCURRENCY=5`), `StitchFunction` (capped at 2), the CDK bucket-notifications singleton, and the frontend SSR Lambda. During synthesis of a large book, `5 + 1 (extract) + 2 (stitch) = 8` leaves **2** for the API and the SSR Lambda combined — and a throttled API means the user's own polling gets `429`s while their book processes. That is a bad failure mode for a reading app. See **OQ-2** (recommendation: drop `SYNTHESIZE_MAX_CONCURRENCY` to 3 in this phase).

### 6.4 The exactly-once gate for stitching

Same shape as phase-4 §8.4, at the book level:

```python
# claim
book_repository.update_status(user_id, book_id, BookStatus.STITCHING,
                              expected_statuses=STITCHABLE_BOOK_STATUSES,
                              updated_at=now)
# ... do the work (idempotent S3 writes at keys that are pure functions of
#     (user_id, book_id) -- every retry overwrites, never appends) ...

# terminal
book_repository.update_status(user_id, book_id, final_status,
                              expected_statuses=(BookStatus.STITCHING,),
                              audio_key=..., manifest_key=...,
                              audio_duration_ms=..., updated_at=now)
```

The claim is permissive (`EXTRACTED` **or** `STITCHING`) for exactly phase-4 §8.2 step 5's reason: a stitcher that died hard leaves `STITCHING` with no lease to expire, and excluding it would wedge the book permanently. The **terminal** transition is the strict one (`STITCHING` only) and is the serialization point — a `ConflictError` there means a concurrent duplicate already finished, and this invocation returns `SKIPPED("ALREADY_STITCHED")` having written only bytes that were byte-identical anyway.

### 6.5 `infra/stacks/config.py` additions

```python
ENV_STITCH_QUEUE_URL         = "STITCH_QUEUE_URL"
ENV_STITCH_MAX_RECEIVE_COUNT = "STITCH_MAX_RECEIVE_COUNT"

STITCH_MAX_RECEIVE_COUNT = 3
STITCH_MAX_CONCURRENCY   = 2   # the ESM minimum; one book at a time is the
                               # real workload, and this function is the
                               # memory-heaviest against a 10-execution
                               # account ceiling (§6.3).
```

with the standard "separately deployed projects, cannot share an import; rename both or neither" lockstep comment pointing at `backend/src/config.py`.

`backend/src/config.py` gains `stitch_queue_url: str = ""` and `stitch_max_receive_count: int = 3`, and `backend/tests/conftest.py`'s `clean_env` fixture gains both names.

### 6.6 Infra test impact (`infra/tests/test_synth.py`)

`_synth_pipeline_stack` needs **no signature change**. Assertions to update/add:

- `AWS::SQS::Queue` count `4 → 6`; `test_pipeline_stack_has_redrive_policies` expects `2 → 3` queues with a `RedrivePolicy`.
- `AWS::Lambda::Function` count `3 → 4`.
- `AWS::Lambda::EventSourceMapping` count `2 → 3`.
- New `test_pipeline_stack_stitch_queue_shape`: `bookloud-*-stitch` has `VisibilityTimeout == 5400` and `RedrivePolicy.maxReceiveCount == 3`; synthesize still `1800`/`5`; extract still `720`/`3`.
- New `test_pipeline_stack_stitch_function_shape`: `ImageConfig.Command == ["src.contexts.library.interface.stitch_handler.handler"]`, `Timeout: 900`, `MemorySize: 1536`, **`ReservedConcurrentExecutions: Match.absent()`** with the same "re-adding this fails a unit test instead of a six-minute deploy" comment.
- New `test_pipeline_stack_stitch_lambda_event_source_mapping`: `{"BatchSize": 1, "ScalingConfig": {"MaximumConcurrency": 2}}`.
- Extend `test_pipeline_stack_synthesize_lambda_and_fan_out_grants` → the stitch grants: `s3:GetObject*` now appears for `audio_bucket` too, and there are now **two** `sqs:SendMessage` grants (extract→synthesize, synthesize→stitch). Assert on the *count* of `AWS::IAM::Policy` statements carrying `sqs:SendMessage`, not just presence, so losing one of the two grants fails.
- `test_pipeline_stack_naming_convention`: `bookloud-<env>-stitch` and `-stitch-dlq`.

CI's `test-infra` job already runs on `ubuntu-latest` with Docker and tolerates the `@pytest.mark.docker` PipelineStack tests. **No workflow change anywhere in this phase.**

---

## 7. Stitching: bytes, offsets, and the manifest

### 7.1 Concatenation approach — raw frames, no re-encoding

MP3 is self-framing: every frame carries its own header, so a decoder resynchronizes at any frame boundary. Concatenating frame streams therefore produces a playable file with **no** transcoding. Re-encoding would mean an ffmpeg or LAME binary in the Lambda image, a subprocess, and CPU-minutes — for zero benefit. Rejected outright.

Three real hazards, each handled:

1. **ID3v2 headers.** If any segment carries one, a mid-stream `ID3` block is skipped by decoders (harmless) but breaks any byte-offset reasoning and confuses some parsers. `mp3.py` already has `_skip_id3v2`; promote it and strip unconditionally.
2. **Xing / Info / VBRI metadata frames.** Many encoders emit a first "frame" that is not audio but a duration/seek table. Concatenating N of them embeds N bogus frames, and worse, the *first* one's claimed frame count would describe only chunk 0 while a browser applies it to the whole file — producing wrong `duration` and wrong seek. Strip a leading Xing/Info/VBRI frame from every segment (detect `Xing`/`Info` at the standard side-info offset — 21 bytes past the header for MPEG-2 mono, 36 for MPEG-1 stereo — or `VBRI` at +36). We deliberately do **not** write a Xing header on the output; see the seek note below.
3. **Format drift between segments.** Normally every chunk comes from the same engine at the same voice, so the frames are byte-format-identical (`audio-24khz-48kbitrate-mono-mp3` = MPEG-2 Layer III, 24 kHz mono, 48 kbps CBR). The one way to get drift is the Google fallback firing on some chunks — and §7.4 of phase 4 explicitly requests `audioEncoding: MP3, sampleRateHertz: 24000`, so the **sample rate matches**, which is the property that actually matters (bitrate changes mid-stream are just VBR and every decoder handles them; sample-rate changes are not reliably handled). Log a `WARNING` and record it in the manifest if a segment's sample rate differs from the first — do **not** fail the stitch, since per-chunk playback still works.

**Seek accuracy caveat, recorded so it isn't a surprise in phase 6.** With no Xing header, browsers estimate byte position from the first frame's bitrate. For a uniform edge-tts book that is exact. For a mixed-engine book it is approximate — which is precisely why the manifest carries `byteStart`/`byteEnd` per segment: phase 6 can seek accurately with a Range request against a known byte offset rather than trusting the browser's estimate.

### 7.2 The book manifest — `marks/<userId>/<bookId>/book.json`

```json
{
  "version": 1,
  "bookId": "6f1a…",
  "status": "PARTIAL",
  "audioKey": "audio/8a2c…/6f1a…/book.mp3",
  "durationMs": 1418240,
  "chunksTotal": 330,
  "chunksDone": 330,
  "chunksFailed": 2,
  "sampleRateHz": 24000,
  "segments": [
    {"i": 0, "t": 0,      "d": 118240, "s": 0,    "e": 1800,
     "audioKey": "audio/8a2c…/6f1a…/000000.mp3",
     "marksKey": "marks/8a2c…/6f1a…/000000.json",
     "b0": 0, "b1": 709440},
    {"i": 1, "t": 118240, "d": 121016, "s": 1800, "e": 3611,
     "audioKey": "…/000001.mp3", "marksKey": "…/000001.json",
     "b0": 709440, "b1": 1435536}
  ],
  "missing": [7, 42]
}
```

Field semantics — phase 6 and phase 7 both consume this, so it is a contract:

- **`i`** — chunk index. `segments` is sorted ascending by `i`, which (because `list_for_book` returns zero-padded-SK order) is also ascending by `t` and by `s`.
- **`t` / `d`** — book-global audio start and duration, integer milliseconds. `segments[k].t == segments[k-1].t + segments[k-1].d` **exactly**, and `sum(d) == durationMs` **exactly** (§7.3). Phase 6 rebases one word with a single addition: `t_global = segment.t + word.t`. It must clamp — the last word's `t + d` can exceed the segment duration by a few ms because engines report word ends optimistically.
- **`s` / `e`** — book-global **character** offsets, straight from `Chunk.char_start`/`char_end`. This is the bridge between the audio timeline and the extracted text, and it is what phase 7's chat anchoring ("which section is being read?") needs. Note the deliberate asymmetry with the per-chunk marks files, where `s`/`e` are **chunk-relative** (phase-4 §7.5): the chunk file indexes into one chunk's DOM text; the manifest indexes into the book. Both are documented in `README.md`.
- **`b0` / `b1`** — byte range of this segment inside `book.mp3` (half-open). Absent when `audioKey` at document level is `null`. Enables exact Range-request seeking (§7.1) and per-chunk extraction from the single file. Impossible to recover later without rescanning every frame, so it is computed now, for free, during concatenation.
- **`audioKey` / `marksKey` per segment** — the per-chunk artefacts, always present. **This is invariant 3 of §1**: the manifest is fully usable with `audioKey: null` at document level, so a book whose concatenation failed, or which is deliberately un-stitched, still plays and highlights chunk by chunk.
- **`missing`** — chunk indexes with no audio, ascending. The UI greys them and keeps the text readable (§3.2).
- **`sampleRateHz`** — from the first segment; `null` when there are no segments. Present so a mixed-rate book (§7.1 hazard 3) is diagnosable from the artefact rather than only from CloudWatch.
- **Short keys for the hot fields (`i/t/d/s/e/b0/b1`), long keys for the once-per-document ones** — the same trade-off phase-4 §7.5 made and for the same reason: a 330-segment document is ~60 KB with short keys and still readable in the S3 console when debugging a desync, which is the single most likely thing to go wrong in phase 6.

**Why not a merged word array.** 330 chunks × ~300 words ≈ 100k word objects ≈ 4 MB of JSON that phase 6 would have to download before the first note plays, and which duplicates data already sitting in 330 independently-fetchable files. The manifest is a few tens of KB; per-chunk marks are fetched lazily as playback approaches. This **refines** `IMPLEMENTATION_PLAN.md`'s phase-5 line "merges timestamp offsets" — the offsets are merged; the marks are not.

### 7.3 Offset math and rounding — `domain/stitching.py` (pure)

```python
MANIFEST_SCHEMA_VERSION = 1

@dataclass(frozen=True)
class StitchSegment:
    index: int
    start_ms: int
    duration_ms: int
    char_start: int
    char_end: int
    audio_key: str
    marks_key: str
    byte_start: int | None = None
    byte_end: int | None = None

def plan_segments(
    entries: Sequence[SegmentInput],   # (index, duration_seconds, byte_len, char_start, char_end, audio_key, marks_key)
) -> tuple[list[StitchSegment], int]:
    """Rebase per-chunk durations into a book-global timeline.

    Accumulates in FLOAT SECONDS and rounds ONCE per boundary:
        cum += duration_seconds
        t_next = round(cum * 1000);  d = t_next - t;  t = t_next
    so segment boundaries exactly partition the timeline (no gap, no
    overlap) and sum(d) == total_ms exactly. Summing per-segment *rounded*
    integers instead would accumulate up to 0.5 ms of error per segment --
    ~165 ms over a 330-chunk book, i.e. a visible highlight lag by the end
    of a long book, growing monotonically. Returns (segments, total_ms)."""

def build_book_manifest(
    *, book: Book, segments: Sequence[StitchSegment], missing: Sequence[int],
    book_audio_key: str | None, total_ms: int, sample_rate_hz: int | None,
) -> dict:
    """Build §7.2's document. Asserts segments are ascending in `i` and that
    t/d are contiguous -- the manifest is the timeline, and a single
    out-of-order or overlapping entry silently corrupts phase 6's lookup."""
```

`infrastructure/mp3.py` gains two pure functions and one refactor:

```python
def mp3_duration_seconds(data: bytes) -> float:
    """The existing frame scan, without the final round. Returns 0.0 when no
    valid frame is found."""

def mp3_duration_ms(data: bytes) -> int:
    return round(mp3_duration_seconds(data) * 1000)   # behaviour unchanged

def strip_container_headers(data: bytes) -> tuple[bytes, int | None]:
    """Drop a leading ID3v2 block and a leading Xing/Info/VBRI metadata
    frame (§7.1), returning (audio_frames, sample_rate_hz_of_first_frame).
    sample_rate is None when no valid frame is found."""
```

`mp3_duration_ms` keeps its exact current semantics, so `EdgeTtsSynthesizer`/`GoogleTtsSynthesizer` and every existing `test_mp3.py` case are untouched.

**Durations are recomputed, not trusted (Q9).** The stitcher calls `mp3_duration_seconds` on the *stripped* bytes it is about to append, and compares against the chunk item's `durationMs`:

```python
if abs(measured_ms - chunk.duration_ms) > DURATION_DRIFT_TOLERANCE_MS:   # 50
    logger.warning("chunk %s duration drift: stored=%d measured=%d",
                   chunk.index, chunk.duration_ms, measured_ms)
```

Reasons, in order: the manifest's `t` values are the only thing between the user and a drifting highlight, so they must describe the bytes actually being served; stripping the Xing frame changes the byte stream anyway, so a fresh measurement is the honest one; and a half-written or overwritten object shows up as a warning instead of a silent desync. Cost is one linear scan of bytes already in memory.

### 7.4 `MultipartWriter` — bounded memory

A 300-page book is ~330 chunks × ~720 KB ≈ **240 MB**. Buffering that in a `bytearray` and then materializing `bytes(...)` for `put_object` peaks near **480 MB** — survivable at 1536 MB but with no headroom, and the failure mode is an OOM **in prod only**, on a big book, in an environment that never runs an automated test (§3.1). That is precisely the class of bug this codebase cannot catch after the fact, so it gets designed out:

```python
# domain/storage.py
class MultipartWriter(Protocol):
    """Streaming write port. Buffers until >= the backend's minimum part
    size, then flushes. Used as a context manager: normal exit completes the
    upload, an exception aborts it (no orphaned multipart parts silently
    accruing storage cost)."""
    def write(self, data: bytes) -> None: ...              # pragma: no cover
    def __enter__(self) -> MultipartWriter: ...            # pragma: no cover
    def __exit__(self, exc_type, exc, tb) -> None: ...     # pragma: no cover
    @property
    def bytes_written(self) -> int: ...                    # pragma: no cover

class ObjectStorage(Protocol):
    def put_bytes(self, *, key: str, data: bytes, content_type: str) -> None: ...
    def get_bytes(self, *, key: str) -> bytes: ...
    def open_multipart(self, *, key: str, content_type: str) -> MultipartWriter: ...   # NEW
```

```python
# infrastructure/s3_object_storage.py
# S3's hard floor for every part except the last. A book smaller than this
# uploads as a single (legal) final part.
MIN_PART_BYTES = 5 * 1024 * 1024

class S3MultipartWriter:
    def __init__(self, *, client, bucket: str, key: str, content_type: str) -> None: ...
```

Peak resident bytes become `MIN_PART_BYTES + one segment` ≈ 6 MB regardless of book size. Fully testable against moto's S3, which implements multipart properly (§10).

Adding `open_multipart` to the `ObjectStorage` Protocol means `RecordingObjectStorage` in `tests/contexts/library/fakes.py` grows a matching in-memory implementation — which is also what lets the use-case tests assert the exact concatenated bytes.

---

## 8. `GET /books/{id}/status`

### 8.1 Is a separate endpoint warranted at all?

Answered honestly: `GET /books/{id}` already returns `status`, `chunksTotal`, `chunksDone`, `chunksFailed`, `failureReason`, `updatedAt` — everything a poller strictly *needs*, and `IMPLEMENTATION_PLAN.md` already names `GET /books/{id}/status` as a phase-5 deliverable. A route that returns a strict subset of another route would be duplication for its own sake.

It earns its place on two counts, and only two:

1. **`terminal`** — a server-computed boolean over `TERMINAL_BOOK_STATUSES`. Without it, a client's stop condition is `status == "READY"`, which never fires for a `PARTIAL` book, i.e. for **every** book in local dev and every PR environment (§3.1). Putting the closed set on the server means the phase-6 poll loop, the smoke test and any future client all agree by construction instead of by three independent copies of a status list.
2. **`progress.percent`** — one place that decides whether a zero-`chunksTotal` book is 0% or 100%, rather than three clients each dividing by zero differently.

It performs **no extra I/O**: one `GetItem` via the existing `GetBook` use case, exactly like `GET /books/{id}`. It adds no new use case and no new authorization path (`GetBook` → `_load_owned_book`, so another user's book is a `404`, never a `403` — the repo's standing rule).

### 8.2 Shape

```python
# interface/controllers.py
@router.get("/books/{book_id}/status")
def get_book_status(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
) -> dict:
    book = GetBook(book_repository).execute(user.sub, book_id)
    return book_status_to_dict(book)
```

```json
{
  "id": "6f1a…",
  "status": "PARTIAL",
  "terminal": true,
  "progress": {"chunksTotal": 12, "chunksDone": 12, "chunksFailed": 12, "percent": 100},
  "failureReason": "NO_AUDIO",
  "audio": {
    "audioKey": null,
    "manifestKey": "marks/8a2c…/6f1a…/book.json",
    "durationMs": 0
  },
  "updatedAt": "2026-08-10T12:34:56+00:00"
}
```

`percent` is `100` when `chunksTotal == 0` **and** the status is terminal, `0` when `chunksTotal == 0` and it is not, otherwise `round(100 * chunksDone / chunksTotal)`. That degenerate branch gets its own test — a book that failed extraction has `chunksTotal == 0` and must not render as a 0%-forever progress bar.

`GET /books/{id}` and `GET /books` also gain `audioKey`, `manifestKey`, `audioDurationMs` on `book_to_dict`, so the book payload and the status payload never disagree about the artefacts. The mild duplication between the two shapes is accepted deliberately for the reason above; `book_status_to_dict` is a separate function in `interface/schemas.py`, not a filter over `book_to_dict`, so the poll contract can stay small and stable while the book payload grows in phases 6-7.

**Exposing S3 keys, not URLs, is deliberate.** Turning `audioKey` into something a `<audio>` element can play (presigned GET vs CloudFront origin, Range support, cache headers) is a phase-6 decision with phase-6 constraints, and shipping a guess now would bake in an interface nobody can validate this phase. See OQ-3.

---

## 9. Local dev & the smoke test

### 9.1 `local/setup.sh`

One-word change: the queue loop becomes `for base in extract synthesize stitch`, which creates `bookloud-local-stitch` and `bookloud-local-stitch-dlq`. No S3 notification, no other change.

### 9.2 Compose services

- `synthesize-worker` gains `STITCH_QUEUE_URL=http://localstack:4566/000000000000/bookloud-local-stitch` — it is now a producer.
- `backend` gains the same var for symmetry (unused by the API today; keeps the four env blocks readable side by side, as the existing `SYNTHESIZE_QUEUE_URL` comment says).
- New `stitch-worker`, mirroring `synthesize-worker` exactly:

```yaml
  # Runs the same handle_records the stitch Lambda's `handler` calls, in a
  # poll loop against LocalStack's SQS. NOTE: with ENVIRONMENT=local every
  # chunk fails synthesis (PLANS/phase-4.md §0), so this worker's steady
  # state is the zero-segment stitch -- book -> PARTIAL/NO_AUDIO with a real
  # book.json and no book.mp3. That is the deterministic local contract, not
  # a bug (PLANS/phase-5.md §3.2).
  stitch-worker:
    build: { context: ./backend, target: dev }
    restart: unless-stopped
    environment:
      - ENVIRONMENT=local
      - GIT_SHA=local
      - LOG_LEVEL=DEBUG
      - AWS_ENDPOINT_URL=http://localstack:4566
      - AWS_DEFAULT_REGION=us-east-1
      - AWS_ACCESS_KEY_ID=local
      - AWS_SECRET_ACCESS_KEY=local
      - TABLE_NAME=bookloud-local
      - AUDIO_BUCKET=bookloud-local-audio
      - MARKS_BUCKET=bookloud-local-marks
      - STITCH_QUEUE_URL=http://localstack:4566/000000000000/bookloud-local-stitch
      - STITCH_MAX_RECEIVE_COUNT=3
    volumes: [./backend:/app]
    command: ["uv", "run", "python", "-m", "src.contexts.library.interface.local_stitch_worker"]
    depends_on: { localstack: { condition: service_healthy } }
```

`Makefile`'s `up` target adds `stitch-worker`. `README.md`'s quickstart updates.

`local_stitch_worker.py` is a near-copy of `local_synthesize_worker.py`: `poll_once(sqs, queue_url) -> int` (testable; deletes a message only when its own `handle_records` succeeded) plus `main()` (`# pragma: no cover`). It must pass `AttributeNames=["ApproximateReceiveCount"]` and forward it into the pseudo-record, exactly as the synthesize worker does — otherwise local dev never exercises §6.4's last-attempt path.

### 9.3 `local/smoke_test.py` — what it can honestly assert

This is the section constraint 2 forces. Restating it so the boundary is unambiguous:

> `get_speech_synthesizer()` gates on `settings.environment != "prod"` first and unconditionally. `local-smoke`'s LocalStack instance and `deploy-pr`'s real-but-ephemeral AWS stack both run with a `StubSynthesizer`. **Every chunk reaches `FAILED`/`EXTERNAL_TTS_DISABLED`, no `audio/` or `marks/` object is ever written, and the stitcher never has audio to concatenate.** `deploy-prod.yml` passes no `--login-*` args and never runs this check at all.

So the smoke test proves the **wiring** and unit tests prove the **bytes**. Concretely, `check_upload_and_synthesis` is renamed `check_upload_and_stitch` and gains a third phase after the existing extraction and synthesis assertions (which are unchanged):

1. Poll `GET /books/{id}/status` every 3 s until `terminal is true`, or `--stitch-timeout` (default 120 s) expires. **Polling the new endpoint is itself part of the test** — it is the phase's other deliverable.
2. Assert `status == "PARTIAL"` and `failureReason == "NO_AUDIO"`. This is the single assertion that proves §3.2's degraded path end to end: the fan-in edge fired, a stitch message was published and consumed, the claim and the terminal transition both ran, and the book did **not** become `FAILED`.
3. Assert `progress == {"chunksTotal": N, "chunksDone": N, "chunksFailed": N, "percent": 100}` with `N >= 1`.
4. Assert `audio.audioKey is None` and `audio.durationMs == 0` — nothing half-written for a book with no audio.
5. Assert `audio.manifestKey` is a non-null string ending in `/book.json`. **This is the load-bearing artefact assertion**: `book.json` can only exist if the stitch Lambda ran, held `marks_bucket.grant_put`, resolved `MARKS_BUCKET`, and completed its terminal DynamoDB transition. Without it, "the stitcher ran" is inferred only from a status string the API could in principle have produced some other way.
6. Assert `GET /books/{id}` agrees (`status`, `audioKey`, `manifestKey`, `audioDurationMs`) — cheap, and catches the two response shapes drifting apart.

What this deliberately does **not** cover, and where that coverage lives instead:

| uncovered by any automated check | covered by |
|---|---|
| frame concatenation producing a playable stream | `test_mp3.py` + `test_stitch_use_case.py` over synthetic MPEG-2 frames (`_frame_bytes`, already in `test_mp3.py`, promoted to `fakes.py`) |
| book-global offset math, contiguity, `sum(d) == durationMs` | `test_stitching_domain.py` (pure, exhaustive) |
| ID3/Xing stripping, byte ranges | `test_mp3.py` |
| multipart upload against a real S3 API | `test_s3_object_storage.py` (moto) |
| the operational envelope (240 MB / 330 objects inside 900 s) | **nothing.** Named as a residual risk; §6.3's memory/timeout sizing and §7.4's bounded buffer are the mitigations. OQ-5. |

OQ-1 proposes closing the first four rows *in a real deployed-ish environment* with an opt-in pure-Python `SilentSynthesizer`. The plan above stands whether or not that is adopted.

---

## 10. Test plan

All new tests under `backend/tests/contexts/library/`, reusing the existing `dynamodb_table`/`book_repo`/`chunk_repo`/`fixed_clock` fixtures. Coverage gate stays `--cov-fail-under=90`. **Zero network calls, zero AWS credentials, no region set** — the `test-backend` CI job has none (which is why §4.4's lazy SQS client is non-negotiable and gets its own regression test).

### 10.1 Test doubles — `tests/contexts/library/fakes.py` (extend)

| double | shape |
|---|---|
| `FakeStitchQueue` | records `enqueue_book(user_id, book_id)` calls; programmable failure. Mirrors the existing `FakeSynthesisQueue`. |
| `RecordingObjectStorage` (Δ) | gains `open_multipart` returning an in-memory `RecordingMultipartWriter` that concatenates on `write` and stores the result on `__exit__`, so a test can assert the **exact** stitched bytes and that an exception path leaves nothing stored. |
| `mpeg2_frames(count, *, sample_rate=24000, bitrate_idx=6)` | promoted from `test_mp3.py`'s existing `_frame_bytes` helper — builds real MPEG-2 Layer III frames. The one fixture that makes the whole byte path testable without a committed binary. |
| `xing_frame()` / `id3v2_header(size)` | build the two container headers §7.1 strips. |

### 10.2 New test files

- **`test_stitching_domain.py`** (pure, exhaustive) — `plan_segments`: contiguity (`t[k] == t[k-1] + d[k-1]`), `sum(d) == total_ms` exactly, the float-vs-int rounding regression (330 segments of a duration whose ms value is `x.5`, asserting total drift is 0 not ~165 ms), a gap in indexes leaves `t` contiguous (audio has no hole), single segment, empty input → `([], 0)`, byte ranges half-open and contiguous. `build_book_manifest`: full schema keys, `missing` sorted, `audioKey: null` variant, ascending/contiguity assertions actually fire on bad input.
- **`test_stitch_use_case.py`** — happy path: two `DONE` chunks → `READY`, exact `book.mp3`/`book.json` keys, **ordering asserted with a call-recording spy** (audio → manifest → DynamoDB, phase-4 §8.2 step 7's rule); the stitched bytes equal the concatenation of the stripped segments; `audio_duration_ms` matches `sum(d)`. Then: mixed (`1 DONE, 1 FAILED`) → `PARTIAL`, `failureReason` cleared, `missing == [1]`; **all-failed → `PARTIAL`/`NO_AUDIO`, `segments == []`, `audioKey is None`, `open_multipart` never called** (the local/PR path — its own named test); `DEFERRED("NOT_COMPLETE")` when `chunks_done < chunks_total`; `SKIPPED("BOOK_NOT_FOUND")`; `SKIPPED("ALREADY_STITCHED")` on a `READY`/`PARTIAL` book; claim `ConflictError` → `SKIPPED("ALREADY_CLAIMED")`; **claim released to `EXTRACTED` on transient failure, and re-raised**; **last attempt converts transient into `PARTIAL`/`STITCH_FAILED` with per-chunk segments and no `book.mp3`**; terminal-transition `ConflictError` → `SKIPPED`; book deleted mid-stitch (`NotFoundError` on the terminal update) → logged, `SKIPPED`; **double delivery produces one `READY` and one `SKIPPED`, and the stored bytes are byte-identical** (idempotence).
- **`test_stitch_handler.py`** — full SQS envelope against moto + fakes; malformed JSON body ignored (message deleted, no loop); unknown `v` ignored; missing `userId`/`bookId` ignored; `ApproximateReceiveCount` parsed off `attributes` and threaded into the command.
- **`test_sqs_stitch_queue.py`** (moto `sqs`) — message body shape and `v`; and the **lazy-client regression test**: constructing `SqsStitchQueue(queue_url=...)` with `AWS_DEFAULT_REGION`/`AWS_REGION` deleted must not raise (this is the exact `NoRegionError` shape that broke CI in phase 4).
- **`test_local_stitch_worker.py`** — `poll_once` deletes on success, leaves on failure, requests and forwards `ApproximateReceiveCount`.

### 10.3 Extended test files

- **`test_mp3.py`** — `mp3_duration_seconds` returns the unrounded float and `mp3_duration_ms` still returns today's exact values for every existing case (regression guard on the refactor); `strip_container_headers` removes an ID3v2 block, a Xing frame, an Info frame, a VBRI frame, and all three combined; leaves a clean stream untouched; returns the first frame's sample rate; returns `(b"", None)` for garbage; **concatenation round-trip**: three segments of known frame counts → `mp3_duration_seconds(concat) == sum(parts)` within 1 µs.
- **`test_synthesis_use_case.py`** — the completing `DONE` increment publishes exactly one stitch message; the completing **`FAILED`** increment also publishes (the local/PR path — its own named test); a non-final increment publishes nothing; `increment_chunks_done` raising `NotFoundError` publishes nothing; the `STITCH_REQUEUED` branch republishes for a complete-but-`EXTRACTED` book and **does not** for `READY`/`PARTIAL`/`STITCHING`/incomplete; a raising `enqueue_book` propagates out of `execute` (so SQS redelivers) rather than being swallowed.
- **`test_book_repository.py`** — `update_status(audio_key=…, manifest_key=…, audio_duration_ms=…)` writes all three; `clear_stitch_outputs=True` REMOVEs both keys and zeroes the duration; `clear_stitch_outputs` **together with** `clear_failure_reason` emits one valid expression (the merged-`REMOVE` trap, §5.3); `audio_key` + `clear_stitch_outputs` → `ValueError`; `expected_statuses=STITCHABLE_BOOK_STATUSES` claim succeeds from `EXTRACTED` and from `STITCHING`, `ConflictError` from `READY`, `NotFoundError` when absent.
- **`test_controllers.py`** — `GET /books/{id}/status` 200 shapes for `EXTRACTED` (non-terminal, partial percent), `READY`, `PARTIAL`; `404` for a missing book and for another user's book (never `403`); `401` anonymous; `chunksTotal == 0` percent branch.
- **`test_schemas.py`** — `book_status_to_dict` keys and `terminal` for every `BookStatus` value; `book_to_dict` gains the three fields.
- **`test_mappers.py`** — `book_to_item`/`item_to_book` round-trip the three new attributes; absent attributes → `None`/`0`.
- **`test_domain.py`** — `BookStatus.parse` accepts `STITCHING`/`PARTIAL`; `STITCHABLE_BOOK_STATUSES`/`TERMINAL_BOOK_STATUSES` contents; `StitchFailure` values.
- **`test_s3_object_storage.py`** — `open_multipart`: under 5 MiB → one part, byte-exact; over 5 MiB across many small `write`s → multiple parts, byte-exact; exception inside the `with` aborts and leaves no object; `bytes_written`.
- **`test_extraction_use_case.py`** — the `EXTRACTED` flip passes `clear_stitch_outputs=True` (re-extraction drops stale stitch pointers).
- **`test_dependencies.py`** — `get_stitch_queue()` returns a `SqsStitchQueue` built from `settings.stitch_queue_url` and does not touch the network at construction.
- **`test_synthesize_handler.py`**, **`test_local_synthesize_worker.py`** — composition roots gain `stitch_queue=get_stitch_queue()`.
- **`backend/tests/test_config.py`** — the two new settings and their defaults; `clean_env` strips them.
- **`infra/tests/test_synth.py`** — per §6.6.

---

## 11. Concrete file list

**`backend/src/contexts/library/`**
- new: `domain/stitching.py`, `application/stitching.py`, `infrastructure/sqs_stitch_queue.py`, `interface/stitch_handler.py`, `interface/local_stitch_worker.py`
- changed: `domain/value_objects.py`, `domain/book.py`, `domain/repository.py`, `domain/storage.py`, `application/synthesis.py`, `application/extraction.py`, `infrastructure/mp3.py`, `infrastructure/s3_keys.py`, `infrastructure/s3_object_storage.py`, `infrastructure/book_mapper.py`, `infrastructure/dynamodb_book_repository.py`, `interface/dependencies.py`, `interface/schemas.py`, `interface/controllers.py`, `interface/synthesize_handler.py`, `interface/local_synthesize_worker.py`

**`backend/`**: `src/config.py`, `tests/conftest.py` (changed). **`pyproject.toml`/`uv.lock` untouched — this phase adds no dependency**, which is itself a design goal (§7.1).

**`backend/tests/contexts/library/`**: `test_stitching_domain.py`, `test_stitch_use_case.py`, `test_stitch_handler.py`, `test_sqs_stitch_queue.py`, `test_local_stitch_worker.py` (new); `fakes.py`, `test_mp3.py`, `test_synthesis_use_case.py`, `test_book_repository.py`, `test_controllers.py`, `test_schemas.py`, `test_mappers.py`, `test_domain.py`, `test_s3_object_storage.py`, `test_s3_keys.py`, `test_extraction_use_case.py`, `test_dependencies.py`, `test_synthesize_handler.py`, `test_local_synthesize_worker.py`, `../../test_config.py` (changed)

**`infra/`**: `stacks/pipeline_stack.py`, `stacks/config.py`, `tests/test_synth.py` (changed). **`app.py`, `stacks/storage_stack.py` and `stacks/api_stack.py` are untouched** — stated explicitly so nobody goes looking.

**`local/`**: `setup.sh`, `smoke_test.py` (changed)

**Root**: `docker-compose.yml`, `Makefile`, `README.md`, `IMPLEMENTATION_PLAN.md` (changed)

---

## 12. Implementation sequence

1. **Pure first, zero AWS:** `mp3.py`'s `mp3_duration_seconds` / `strip_container_headers` + `test_mp3.py`. **Gate:** every existing `test_mp3.py` case must still pass unchanged — `mp3_duration_ms`'s behaviour is frozen.
2. `domain/stitching.py` (`StitchSegment`, `plan_segments`, `build_book_manifest`, `StitchQueue`) + `test_stitching_domain.py`. **Gate:** the float-vs-int rounding test must be green before anything downstream trusts a `t`.
3. `value_objects.py` (`STITCHING`, `PARTIAL`, `STITCHABLE_BOOK_STATUSES`, `TERMINAL_BOOK_STATUSES`, `StitchFailure`), `book.py`, `s3_keys.py`, `book_mapper.py` + tests. Still no AWS.
4. `dynamodb_book_repository.update_status` (the merged-`REMOVE` refactor) + `test_book_repository.py`. **Gate:** the combined `clear_failure_reason` + `clear_stitch_outputs` test must be green — it is the one that catches a `ValidationException` that would otherwise only appear at runtime.
5. `domain/storage.py`'s `MultipartWriter` + `S3MultipartWriter` + moto tests.
6. `infrastructure/sqs_stitch_queue.py` + moto tests, **including the no-region construction test**.
7. `application/stitching.py` (`StitchBook`) + `interface/stitch_handler.py` + tests, including all-failed, mixed, double-delivery, claim-release and last-attempt.
8. `application/synthesis.py`'s `stitch_queue` dependency, `_maybe_publish_stitch`, and the `STITCH_REQUEUED` branch + tests; update both composition roots.
9. `interface/schemas.py` + `controllers.py`'s `GET /books/{id}/status` + tests. Full backend suite green at ≥ 90%.
10. `pipeline_stack.py` + `infra/stacks/config.py` + infra tests. `make synth` locally.
11. `local/setup.sh`, compose `stitch-worker`, Makefile. `make up`, upload the fixture PDF by hand, watch the book go `EXTRACTED → STITCHING → PARTIAL` and confirm `book.json` really lands in LocalStack's marks bucket.
12. `local/smoke_test.py` + `make smoke` — the phase's real gate.
13. README (S3 layout, manifest schema, status lifecycle, the four workers) + `IMPLEMENTATION_PLAN.md` checklist, and extend phase 8's deferred-observability list with §4.3's stranded-stitch backstop.
14. Push; `deploy-pr` runs the whole thing against real AWS with real Lambdas.

---

## 13. Open Questions

Each states a recommendation and the trade-off, so a single word approves it.

> **All six are DECIDED (2026-08-10). Every recommendation was accepted as written.** OQ-2 came with a question worth recording, because it will come up again whenever these numbers are revisited: *"Why do we need more than one? For now it is only going to be used by me."* `max_concurrency` parallelises **the chunks of a single book**, not users — a 300-page book is ~330 independent SQS messages, so concurrency 1 makes one user wait serially through all of them. It also interacts with the retry budget: throttled messages return to the queue and bump `ApproximateReceiveCount`, the same counter that decides DLQ-ing at 5 (`config.py`'s existing note on why that value is 5, not 3). Hence 3 — more than one, but leaving 4 executions for the API and SSR Lambdas. Per-chunk edge-tts latency is **unmeasured** (nothing has ever run the real engine), so revisit once prod produces real timings.

**OQ-1 (DECIDED: adopt) — Add an opt-in, network-free `SilentSynthesizer` so local dev actually stitches real bytes?**
Today (and after this plan) no automated check anywhere concatenates a real MP3, because `ENVIRONMENT != "prod"` always yields `StubSynthesizer` (phase-4 §0). A ~60-line pure-Python `SilentSynthesizer` — valid silent MPEG-2 Layer III frames sized to the chunk's text, word marks via the existing `estimate_word_marks` — would make `make up` + `make smoke` exercise concatenation, byte offsets, the multipart upload and the manifest against LocalStack's real S3, with **zero network calls and zero third-party dependency**, so it does not re-litigate phase-4 §0's rule (that rule is about *external services*, and this is a local generator). Gate it on a new `SYNTHESIS_STUB_MODE` setting defaulting to `disabled`, set `SYNTHESIS_STUB_MODE=silent` **only** on the compose `synthesize-worker` (never on a PR stack), and give the smoke test `--expect-synthesis {failed,silent}` (default `failed`, `make smoke` passes `silent`). Split: LocalStack proves the bytes, `deploy-pr` keeps today's deterministic-failure assertions and proves the AWS wiring.
**Trade-off:** it forks the smoke assertions into two modes and adds a `SynthesisSource.SILENT` value that shows up in `chunk.synthesisSource` locally; against that, it is the only way any automated run ever exercises the audio path end to end.
**Recommendation: adopt.** If declined, drop `silent_synthesizer.py` and the smoke flag; nothing else in the plan changes.

**OQ-2 (DECIDED: lower it to 2 — the AWS floor, revised down from 3 on 2026-08-10 at the user's request: "start as low as possible.. and lemme know if its enough or we need to bump up") — Lower `SYNTHESIZE_MAX_CONCURRENCY` from 5 to 3?**
The account's *total* Lambda concurrency is 10 (phase-4 §6.3's correction). After this phase, a large book in flight can consume `5 (synthesize) + 1 (extract) + 2 (stitch) = 8`, leaving 2 for the API Lambda and the frontend SSR Lambda combined — so the user's own status polling can get throttled precisely while their book is processing. Dropping synthesize to **2** (the floor -- see the DECIDED note below) leaves 5, at the cost of longer synthesis on a big book. NOTE: this paragraph's original arithmetic was written for a recommendation of 3 and is retained only for the reasoning; **the decided value is 2**.
**Trade-off:** throughput on big books vs API responsiveness during processing. For a single-user reading app, responsiveness wins.
**DECIDED: `SYNTHESIZE_MAX_CONCURRENCY = 2`.** 2, not 1: `ScalingConfig.MaximumConcurrency`'s minimum accepted value is 2 (see §6's ESM notes), so 1 is not expressible without dropping the ScalingConfig entirely and losing the poller back-off that keeps throttling from burning `ApproximateReceiveCount`. This leaves `2 (synthesize) + 1 (extract) + 2 (stitch) = 5` of the account's 10, so the API and SSR Lambdas keep 5. Deliberately the floor: start minimal, measure a real prod book, bump if synthesis is too slow. It is one named constant (a one-line change to an already-named constant), and revisit if the account quota is ever raised.

**OQ-3 (DECIDED: defer to phase 6) — Ship an audio-delivery endpoint now, or defer to phase 6?**
This phase makes `audioKey`/`manifestKey` visible in the API but gives no way to actually fetch the bytes. A `GET /books/{id}/audio` returning a presigned GET (or a redirect) would make the stitched output observable end to end today.
**Trade-off:** shipping it now means guessing at delivery mechanics (presigned GET vs CloudFront origin, Range-request support for §7.1's seeking, expiry vs a long `<audio>` session) with nothing in the repo able to validate the guess — and in every environment CI can reach there is no audio to fetch anyway. Deferring means phase 5's artefacts are only inspectable via the AWS console until phase 6.
**Recommendation: defer to phase 6**, and record it in `IMPLEMENTATION_PLAN.md` phase 6's bullet list so it isn't lost.

**OQ-4 (DECIDED: (a), extend the phase-8 item) — Backstop for a stranded stitch: extend phase 8's DLQ consumer, or ship a retry endpoint now?**
§4.3's residual hole: if `enqueue_book` fails on every attempt of the completing chunk's message, the chunk message DLQs and the book sits at `EXTRACTED` with `chunksDone == chunksTotal` forever. Options: (a) extend the DLQ-consuming Lambda already deferred to phase 8 (phase-3 OQ-4) to also re-publish a stitch for books whose counters are complete; (b) ship `POST /books/{id}/stitch` now as a manual re-trigger.
**Trade-off:** (b) is ~20 lines and gives an immediate escape hatch, but adds a mutating endpoint with no UI to call it and a new authorization surface, for a failure mode that requires SQS to be down for the full retry window. (a) is free now and consolidates every "stranded pipeline item" recovery in one place.
**Recommendation: (a) — extend the phase-8 item**, and add a line to `IMPLEMENTATION_PLAN.md` phase 8 saying so.

**OQ-5 (DECIDED: ship serial + the 300 s WARNING) — Is 900 s / 1536 MB enough for the largest book you actually intend to read?**
With §7.4's bounded buffer, memory is no longer the binding constraint; wall-clock is. The estimate for a 300-page book is ~330 sequential `GetObject`s (~35 s), a ~1.3 M-frame Python scan (~5-10 s), and ~48 `UploadPart`s (~60 s) — roughly two minutes, well inside 900 s, which is Lambda's hard maximum with no larger value available. A 1000-page book would be ~3× that and still fit, but the margin is estimated, not measured, and §9.3 says plainly that **no automated test will ever measure it**. The cheap hardening is parallelising the segment downloads with a small `ThreadPoolExecutor` (4-8 workers), which cuts the dominant term by ~5× for ~10 lines.
**Trade-off:** concurrency in the download loop adds ordering care (results must be reassembled by index) and makes the byte-offset accumulation slightly less obvious; against that, it removes the only unmeasured operational risk in the phase.
**Recommendation: ship serial in this phase** (simplest correct thing, and it comfortably fits the stated target of personal-library books), **but add a `WARNING` log when a stitch exceeds 300 s of wall clock**, so the margin becomes observable in prod before it becomes an incident.

**OQ-6 (DECIDED: leave both tuples unchanged) — Should `PARTIAL` be re-issuable / re-synthesizable?**
`_REISSUABLE_STATUSES` and `_CLAIMABLE_STATUSES` are both `(UPLOADED, FAILED)`, so a `PARTIAL` book is a dead end: in prod, a book whose engines all failed can never get audio without deleting and re-uploading it. Adding `PARTIAL` to those tuples would fix that — but re-*upload* is the wrong lever (the PDF and its text are fine; only synthesis failed), and adding `PARTIAL` to `_CLAIMABLE_STATUSES` would let a stray S3 event wipe and re-extract a perfectly good book, which is exactly the hazard §3.1 designed `PARTIAL` to avoid.
**Trade-off:** leaving it means a real prod dead end until a retry path exists; fixing it *this* way reintroduces the bug the status was created to prevent.
**Recommendation: leave both tuples unchanged in phase 5**, and add "retry synthesis for a `PARTIAL` book (`POST /books/{id}/resynthesize`: reset `FAILED` chunks to `PENDING`, re-publish the fan-out for those indexes, reset the book to `EXTRACTED`)" to `IMPLEMENTATION_PLAN.md` phase 6, where there will be a UI to trigger it.

---

### Critical Files for Implementation
- backend/src/contexts/library/application/synthesis.py
- backend/src/contexts/library/domain/repository.py
- backend/src/contexts/library/domain/value_objects.py
- backend/src/contexts/library/infrastructure/mp3.py
- backend/src/contexts/library/infrastructure/dynamodb_book_repository.py
- infra/stacks/pipeline_stack.py
