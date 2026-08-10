# Phase 4 — TTS synthesis pipeline (parallel)

Goal: every `CHUNK#n` written by phase 3 gets its own SQS message, its own Lambda invocation, and its own pair of S3 artifacts — an MP3 in `audio_bucket` and a word-timestamp JSON in `marks_bucket` — after which the chunk flips to `DONE` (or `FAILED` + a reason) and the book's `chunksDone` counter is atomically incremented exactly once. Primary engine is **edge-tts**; **Google Cloud TTS** is the fallback. Stitching, the `READY` status, and `GET /books/{id}/status` are **phase 5** and explicitly out of scope.

**Verified against the current repo:**

- `infra/stacks/pipeline_stack.py` already builds `synthesize_queue` + its DLQ (`max_receive_count=3`, `visibility_timeout=5 min`) with **no consumer and no producer**. Its docstring already says "The synthesize Lambda (phase 4) attaches to `synthesize_queue` later without a queue-shape change" — that turns out to be *almost* true: the queue shape does change (visibility timeout + `max_receive_count`), but nothing else about it does.
- `infra/stacks/storage_stack.py`'s `audio_bucket`/`marks_bucket` exist, are private, `BLOCK_ALL` public access, S3-managed encryption, and are **written by nobody**. `api_stack.py` already sets `AUDIO_BUCKET`/`MARKS_BUCKET` on the API Lambda and already does `audio_bucket.grant_read_write(fn)`/`marks_bucket.grant_read_write(fn)` — so **no `ApiStack` change is needed in this phase at all**.
- `ChunkStatus` (`domain/value_objects.py`) declares `PENDING`/`DONE`/`FAILED` with the comments `# first SET in phase 4` already on the latter two. **`SYNTHESIZING` is not declared** and must be added.
- `DynamoDbChunkRepository.update_status` takes `audio_key`/`marks_key` optional kwargs already, with the comment "so a status-only call (phase 3) never nulls out keys a previous call (phase 4) set". It has **no `expected_statuses`**, no `ConflictError` disambiguation, no failure-reason support — it needs exactly the generalization phase 3 §5.3 gave `BookRepository.update_status`.
- `DynamoDbBookRepository.increment_chunks_done` exists, is `ADD chunksDone :one` with `attribute_exists(PK)` + `ReturnValues="UPDATED_NEW"`, returns `int`. It has **no production caller** — only `test_book_repository.py` and two in-test fakes. It is free to change shape.
- `application/extraction.py`'s module docstring already states the load-bearing ordering ("chunks are written **before** the book flips to `EXTRACTED` … phase 4's fan-out depends on this") and `_CLAIMABLE_STATUSES = (UPLOADED, FAILED)` deliberately excludes `EXTRACTED` because "claiming an already-extracted book would wipe chunks phase 4 may already be working on". Both comments are load-bearing for §4 below.
- `domain/chunking.py` already exports `split_sentences(text, offset=0)` — reused verbatim by the Google-TTS SSML mark builder (§7.4). No new sentence splitter.
- `infrastructure/keys.py` owns `CHUNK_INDEX_WIDTH = 6` with an "immutable once any data exists" warning; the new S3 keys import it rather than re-declaring `06d`.
- **No stack puts any Lambda in a VPC.** `ApiStack`/`PipelineStack` create bare `DockerImageFunction`s with no `vpc=`/`vpc_subnets=`. So the synthesize Lambda gets AWS-managed networking with direct internet egress — no NAT gateway, no VPC endpoints, nothing to configure for edge-tts's outbound WSS or Google's HTTPS. (Verified externally: edge-tts 7.2.8 depends only on `aiohttp`, `certifi`, `tabulate`, `typing-extensions` — all manylinux-wheel-clean.)
- `backend/Dockerfile`'s header comment already anticipates this phase: "phases 3-4 pull in PyMuPDF and **edge-tts**, which blow past the Lambda zip layer size limits".

---

## 0. Addendum — real TTS only in prod, everywhere else gets a deterministic stub

**Decided after the plan below was written, overriding every place the original text assumed local/PR hit the real edge-tts endpoint** (§9.2's "this container does reach the public internet", §9.3's "accept live-endpoint flakiness as a hard CI gate", the local-dev voice comment). The rule: **`SpeechSynthesizer` only talks to a real engine when `ENVIRONMENT == "prod"`.** Local dev (`ENVIRONMENT=local`) and every ephemeral PR stack (`ENVIRONMENT=pr-N`) get a `StubSynthesizer` instead — no exceptions, no opt-in flag to turn real calls on in those environments in this phase.

Why: hitting Microsoft's undocumented free endpoint and Google's API from every developer laptop run and every CI run (`local-smoke` + `deploy-pr`, on every push) is both wasteful and a source of flaky, unrelated CI failures. Since `deploy-prod.yml` never runs the login-gated smoke checks at all (established since phase 1 — no `--login-*` args), gating real synthesis to prod also means **no automated test ever exercises the real engines** — that trade-off is accepted deliberately: real synthesis gets proven by actually using the deployed app, not by an automated check hitting a paid/rate-limited third party.

**`StubSynthesizer` — new, `infrastructure/stub_synthesizer.py`:**

```python
class StubSynthesizer:
    """Returned by get_speech_synthesizer() whenever ENVIRONMENT != "prod".
    Synthesizes nothing and never touches the network -- keeps local dev and
    every ephemeral PR stack from depending on a live third-party endpoint."""
    name = "stub"

    def synthesize(self, text: str) -> SynthesizedAudio:
        raise SynthesisDisabled()
```

New **permanent** error class in `domain/synthesis.py`, alongside `UnsynthesizableText`:

```python
class SynthesisDisabled(SynthesisError):
    """Raised by StubSynthesizer. Always permanent -- retrying a config
    state five times across two hours of SQS backoff proves nothing and
    only slows down local/PR development."""
    reason = SynthesisFailure.EXTERNAL_TTS_DISABLED
```

New value in `SynthesisFailure` (§5.2): `EXTERNAL_TTS_DISABLED = "EXTERNAL_TTS_DISABLED"`.

**`SynthesizeChunk` (§8.2/§8.3) treats `SynthesisDisabled` exactly like `UnsynthesizableText`** — caught in the same branch as the pre-flight text-validation checks, **no retry**, immediate `FAILED`/`EXTERNAL_TTS_DISABLED`, `chunksDone`/`chunksFailed` both +1, message deleted. It is a config fact known before the engine call, not a transient blip, so it does not belong in the "release to `PENDING`, let SQS retry" branch — that would burn the full 5-attempt/2-hour budget in local dev and PR environments for zero benefit, every single time.

**`interface/dependencies.py`'s `get_speech_synthesizer()`:**

```python
def get_speech_synthesizer(settings: Settings = Depends(get_settings)) -> SpeechSynthesizer:
    if settings.environment != "prod":
        return StubSynthesizer()
    primary = EdgeTtsSynthesizer(voice=settings.edge_tts_voice)
    if not settings.google_tts_secret_name:
        return primary
    return FallbackSynthesizer(primary, GoogleTtsSynthesizer(
        api_key=get_secret(settings.google_tts_secret_name), voice=settings.google_tts_voice,
    ))
```

The environment gate is checked **first and unconditionally** — a `google_tts_secret_name` accidentally set on a PR stack (it shouldn't be; OQ-A ships it unset everywhere but prod) still can't turn on real calls outside prod. `FallbackSynthesizer`/`GoogleTtsSynthesizer` are simply never constructed for local/PR, so there's no real-vs-stub branching inside them to get wrong.

**§9.2 local dev correction:** the `synthesize-worker` compose service does **not** need public internet egress and does **not** call real edge-tts — it runs `ENVIRONMENT=local`, gets a `StubSynthesizer`, and every chunk it processes reaches `FAILED`/`EXTERNAL_TTS_DISABLED` deterministically. This is a *feature* for local dev, not a limitation: `make up` + a manual upload now proves the whole fan-out/claim/counter machinery in seconds, with no dependency on Microsoft's endpoint being reachable from a developer's network.

**§9.3 smoke test correction — this replaces the original section's expectations:** `check_upload_and_synthesis` polls chunks to a terminal state exactly as originally planned, but in **every environment the smoke test runs against** (`local-smoke`'s LocalStack instance and `deploy-pr`'s real-but-ephemeral AWS stack — `deploy-prod` never runs this login-gated check at all, unchanged from every prior phase), the TTS engine is a stub. So the assertions become:

1. Poll `GET /books/{id}/chunks` until every chunk is terminal (`DONE` or `FAILED`) or `--synthesis-timeout` expires — unchanged.
2. Assert **every** chunk is `FAILED` with `failureReason == "EXTERNAL_TTS_DISABLED"` (not `DONE` — there is no real audio to expect in these environments).
3. Assert `audioKey`/`marksKey`/`durationMs` are all still null/zero on every chunk (proves nothing was half-written for a chunk that never actually synthesized).
4. Poll `GET /books/{id}` and assert `chunksDone == chunksTotal == chunksFailed` — **this is still the load-bearing fan-in proof** from the original plan (§4.2/§8.4's correctness invariants), and it is now fully deterministic: no live-endpoint flakiness risk at all, because nothing in CI ever calls out. This resolves the original §13 OQ-D outright — there is no "accept flakiness" trade-off to make, because the flaky dependency has been removed from every automated path.

The `--skip-synthesis` escape hatch from the original plan is no longer needed for flakiness reasons but is kept anyway as a cheap way to shorten local iteration.

**§6.3 reserved-concurrency correction — the plan's `reserved_concurrent_executions=10` is not deployable on this account and has been removed** (also overriding §2's Q13, §6.3's snippet, §12's "has `ReservedConcurrentExecutions: 10`" assertion, and §13's OQ-6). This AWS account's *total* Lambda concurrency limit is 10, and AWS rejects any reservation that drops unreserved concurrency below its floor of 10 — so every possible value fails, in prod exactly as in `pr-N`:

> `Specified ReservedConcurrentExecutions for function decreases account's UnreservedConcurrentExecution below its minimum value of [10]`

Caught by `deploy-pr` on PR #5 as a `CREATE_FAILED` on `SynthesizeFunction`. Nothing in the design is lost: §6.3's own reasoning already made the ESM's `ScalingConfig.MaximumConcurrency` the *real* throttle and reserved concurrency merely the backstop, and `max_concurrency=5` caps concurrent invocations without reserving account-wide capacity. The "belt-and-braces" half is simply unavailable until the account quota is raised. `test_pipeline_stack_synthesize_function_shape` now asserts `Match.absent()` so re-adding it fails a unit test instead of a six-minute deploy.

**§6.4/OQ-A correction — "dormant" means `GOOGLE_TTS_SECRET_NAME` is unset in prod too.** The original wiring defaulted prod to `Config.GOOGLE_TTS_SECRET_NAME`, which is not dormant at all: `get_speech_synthesizer()` calls `get_secret()` **eagerly, while building the synthesizer**, before edge-tts is ever attempted. With no such secret in the account (there is none — `aws secretsmanager list-secrets` returns `[]`), every prod chunk would have failed with `ResourceNotFoundException`, retried 5×, and DLQ'd — including chunks edge-tts, which needs no credentials whatsoever, would have synthesized fine. The CDK deploy would still have gone green, because `from_secret_name_v2` resolves a name to an ARN without checking existence.

`app.py` now defaults the secret name to `""` everywhere. Turning the fallback on is a deliberate two-step, in this order: (1) create the secret out of band, (2) deploy with `-c google_tts_secret_name=bookloud/google-tts-api-key`. Verified both ways by synthesizing `BookloudPipeline-prod`: unset yields `GOOGLE_TTS_SECRET_NAME: ""` and no `secretsmanager:GetSecretValue` grant; with the context flag, both reappear.

**Frontend/UX note for phase 6 (no phase-4 code change, recorded so it isn't lost):** the per-chunk `failureReason` contract this phase already produces (`EXTERNAL_TTS_DISABLED` in dev/PR, `ALL_ENGINES_FAILED` in prod after real retries are exhausted) is exactly what a future reader UI needs to render "audio unavailable for this section — continue reading" instead of a hard error, and to let the user keep reading/scrolling text for chunks with no audio rather than blocking the whole book on one failed chunk. Phase 4 does not add any UI; it only makes sure the backend contract already supports that degradation.

---

## 1. End-to-end flow

```
extract Lambda                SQS synthesize_queue        synthesize Lambda (xN, max 5 concurrent)      S3 / DynamoDB
  | save_all(CHUNK#n, PENDING) --------------------------------------------------------------------------->| chunks
  | update_status(EXTRACTED, chunksTotal=N, chunksDone=0, chunksFailed=0) ----------------------------------->| book
  | enqueue_chunks(indexes 0..N-1)   (send_message_batch, 10/call)
  |-------------------------------->|
                                   |--- batch_size=1, max_concurrency=5 --->|
                                   |                                        | get(CHUNK#n)  -> text
                                   |                                        | claim: !DONE -> SYNTHESIZING (conditional)
                                   |                                        | edge-tts  ---(WordBoundary)--->  mp3 + marks
                                   |                                        |   on ANY failure -> Google TTS fallback
                                   |                                        | put audio/<u>/<b>/000007.mp3 -->| audio_bucket
                                   |                                        | put marks/<u>/<b>/000007.json ->| marks_bucket
                                   |                                        | update_status(DONE, expected=!DONE,
                                   |                                        |   audioKey, marksKey, durationMs, source)
                                   |                                        |   -- succeeded? --> increment_chunks_done()
                                   |                                        |   -- ConflictError? --> no increment (dedupe)
```

Two invariants the whole phase rests on:

1. **The fan-out is published *after* the `EXTRACTED` flip** (§4.2), so `chunksTotal` is always set before any chunk can complete. Phase 5's `chunksDone == chunksTotal` comparison is therefore never evaluated against a `chunksTotal` of 0.
2. **`chunksDone` is incremented only by the invocation whose conditional `→ DONE` (or `→ FAILED`) update actually succeeded** (§8.4). The claim is for observability and cheap short-circuiting; the *correctness* guarantee lives on the terminal transition. This is what makes at-least-once SQS delivery, duplicate fan-out publishes, and crashed-mid-flight invocations all safe without a distributed lock.

---

## 2. Decisions up front (details in the referenced sections)

| # | Question | Decision | § |
|---|---|---|---|
| Q1 | Who publishes the fan-out? | **The extract Lambda**, via a `SynthesisQueue` **Protocol port** in `domain/`, as the last step of `ExtractBook.execute` — *after* the `EXTRACTED` flip. Not DynamoDB Streams, not a separate fan-out Lambda. | §4 |
| Q2 | Re-entrancy if the publish fails | New pre-claim branch: a redelivered extract message for a book already `EXTRACTED` with `chunksTotal > 0` **re-publishes only** (outcome `REQUEUED`), never re-extracts. Duplicate publishes are harmless by construction. | §4.3 |
| Q3 | Chunk status lifecycle | Add **`SYNTHESIZING`**. `PENDING\|FAILED\|SYNTHESIZING → SYNTHESIZING` claim (i.e. "anything but `DONE`"), then `→ DONE`/`→ FAILED` with the same conditional. `ChunkRepository.update_status` gets phase 3's `expected_statuses` generalization. | §5.3, §6 |
| Q4 | Book status during synthesis | **Unchanged — stays `EXTRACTED`.** No `SYNTHESIZING` book status. Progress is `chunksDone`/`chunksTotal`/`chunksFailed`. `READY` is phase 5's to set. | §5.2 |
| Q5 | Exactly-once `chunksDone` | Increment **only when the conditional terminal update succeeded**. `increment_chunks_done` gains `failed: bool` and returns a `ChunkCounters(chunks_done, chunks_total, chunks_failed)` read off `ReturnValues="ALL_NEW"` — phase 5 gets its final-chunk check with zero extra reads. | §5.4, §7 |
| Q6 | Terminal `FAILED` chunks and fan-in | A permanently-failed chunk **still increments `chunksDone`** (it reached a terminal state) *and* `chunksFailed`. Otherwise a single bad chunk wedges the book below `chunksTotal` forever and phase 5 never fires. | §5.4 |
| Q7 | edge-tts → Google decision tree | **Every** edge-tts failure falls through to Google. The free Edge endpoint's failure modes are undocumented and version-unstable, so the adapter catches broad `Exception`. Permanence is decided *before* any engine call (empty/oversized text) or *after* both failed on the last SQS attempt. | §8.2 |
| Q8 | Google credentials | **API key in AWS Secrets Manager**, one shared secret (`bookloud/google-tts-api-key`) across prod and every PR env, created **out of band**. Env var carries the *secret name*; unset/absent → fallback silently disabled. Called via the **REST v1beta1 endpoint with `urllib`**, not `google-cloud-texttospeech` (no gRPC, no 30 MB of deps). | §7.4, §6.4 |
| Q9 | Google word timings | Google gives **only SSML `<mark>` timepoints**, not per-word events, and the input is capped at **5000 bytes including markup**. Per-word marks would blow that cap. Decision: **one `<mark>` per sentence** (reusing `split_sentences`) + **proportional intra-sentence interpolation** over the measured audio duration. Marks JSON records `"timing": "measured" \| "estimated"`. | §7.4, §7.5 |
| Q10 | Audio duration | Measured exactly by a **pure MP3 frame-header parser** (`infrastructure/mp3.py`, ~60 lines, no deps). Needed for the Google interpolation path *and* by phase 5's offset math. Written into the marks JSON as `durationMs` and onto the chunk item. | §7.6 |
| Q11 | Marks JSON shape | Array of objects with **short keys** `{t,d,s,e,w}` — start ms, duration ms, **char start/end relative to the chunk text**, word text — plus a document header. `s`/`e` are non-negotiable: without them phase 6 must re-tokenize and hope it matches. | §7.5 |
| Q12 | S3 key layout | `audio/<userId>/<bookId>/<index:06d>.mp3` and `marks/<userId>/<bookId>/<index:06d>.json`, added to the existing `infrastructure/s3_keys.py` with parse/round-trip functions and tests, reusing `keys.CHUNK_INDEX_WIDTH`. | §5.1 |
| Q13 | Reserved concurrency | **ESM `max_concurrency=5`** (the real knob) **plus function `reserved_concurrent_executions=10`** (the backstop, deliberately higher so the ESM's `MaximumConcurrency` validation can't fail). `batch_size=1`. | §6.3 |
| Q14 | Queue shape | `synthesize_queue` visibility **30 min** (6× the 300s function timeout, matching phase 3's rule) and `max_receive_count` **5** (was 3), because throttle-induced redeliveries would otherwise burn the retry budget. | §6.2 |
| Q15 | Both engines failed | **Transient** — release the claim to `PENDING` and re-raise, *unless* this is the last SQS attempt (`ApproximateReceiveCount >= 5`), in which case mark the chunk `FAILED`/`ALL_ENGINES_FAILED` and return normally. No silent DLQ residue, real user-visible feedback. | §8.3 |
| Q16 | Mocking TTS in tests | A `SpeechSynthesizer` **Protocol** (exactly phase 3's `PdfTextExtractor` pattern) with a `FakeSynthesizer`. The two real adapters are tested with an injected fake `edge_tts.Communicate` class and an injected fake HTTP callable. **Zero network in the whole test suite.** | §10 |
| Q17 | Local dev | A `synthesize-worker` compose service mirroring `extract-worker`, running the same `handle_records`. It does hit the real edge-tts endpoint (containers have egress). | §9 |
| Q18 | Smoke test | Extend `check_upload_and_extraction` into `check_upload_and_synthesis`: poll chunks to terminal, then assert against a **stub** in every CI/PR environment (real engines are prod-only — see §0) — every chunk `FAILED`/`EXTERNAL_TTS_DISABLED`, `chunksDone == chunksTotal == chunksFailed`. Fully deterministic, no live-endpoint dependency anywhere in CI. | §0, §9.3 |
| Q21 | Real TTS vs stub by environment | **`ENVIRONMENT == "prod"` only** gets real edge-tts/Google; `local` and every `pr-N` get a `StubSynthesizer` that immediately raises a **permanent** `SynthesisDisabled` (`failureReason: EXTERNAL_TTS_DISABLED`), no retries. Decided after user feedback: no automated environment should depend on a live third-party endpoint. | §0 |
| Q19 | New bounded context? | **No.** Synthesis produces the Library's own `Chunk` artifacts, same reasoning as phase 3's OQ-1. | §3 |
| Q20 | Frontend | **Out of scope** — phase 6. No presigned-GET/audio-streaming endpoint this phase. | — |

---

## 3. DDD placement — stays inside `contexts/library/`

Same argument as phase 3's OQ-1: synthesis mutates the Library's own `Chunk` aggregates and the `Book`'s counters. A separate `synthesis` context writing another context's aggregates is a worse smell than a slightly larger context.

New/changed layout (⊕ = new, Δ = changed):

```
backend/src/contexts/library/
├── domain/
│   ├── value_objects.py            Δ  + ChunkStatus.SYNTHESIZING, SynthesisFailure,
│   │                                  SynthesisSource, MarksTiming
│   ├── chunk.py                    Δ  + duration_ms, failure_reason, synthesis_source
│   ├── book.py                     Δ  + chunks_failed
│   ├── repository.py               Δ  ChunkRepository.update_status generalized;
│   │                                  increment_chunks_done -> ChunkCounters; + ChunkCounters
│   ├── storage.py                  Δ  + ObjectStorage (Protocol)
│   ├── synthesis.py                ⊕  WordMark, SynthesizedAudio, SpeechSynthesizer (Protocol),
│   │                                  SynthesisQueue (Protocol), SynthesisError hierarchy
│   └── marks.py                    ⊕  PURE: iter_words, align_words, estimate_word_marks,
│                                      build_marks_document, MARKS_SCHEMA_VERSION
├── application/
│   ├── extraction.py               Δ  + synthesis_queue dep, publish step, REQUEUED branch
│   └── synthesis.py                ⊕  SynthesizeChunk use case + command/result
├── infrastructure/
│   ├── s3_keys.py                  Δ  + chunk_audio_key/chunk_marks_key + parsers
│   ├── s3_object_storage.py        ⊕  S3ObjectStorage (put_bytes/get_bytes), one per bucket
│   ├── sqs_synthesis_queue.py      ⊕  SqsSynthesisQueue (send_message_batch)
│   ├── edge_tts_synthesizer.py     ⊕  the ONLY module importing edge_tts
│   ├── google_tts_synthesizer.py   ⊕  the ONLY module talking to Google (urllib REST)
│   ├── fallback_synthesizer.py     ⊕  primary→fallback decorator; the decision tree lives here
│   ├── secrets.py                  ⊕  cached Secrets Manager lookup (module-level cache)
│   ├── mp3.py                      ⊕  PURE mp3_duration_ms(bytes) -> int
│   ├── chunk_mapper.py             Δ  durationMs, failureReason, synthesisSource
│   ├── book_mapper.py              Δ  chunksFailed
│   ├── dynamodb_chunk_repository.py Δ update_status rewrite (§5.3)
│   └── dynamodb_book_repository.py Δ increment_chunks_done rewrite (§5.4), update_status kwargs
└── interface/
    ├── dependencies.py             Δ  + get_audio_storage, get_marks_storage,
    │                                    get_synthesis_queue, get_speech_synthesizer
    ├── schemas.py                  Δ  + durationMs/failureReason/synthesisSource on chunk,
    │                                    chunksFailed on book
    ├── extract_handler.py          Δ  composition root gains synthesis_queue
    ├── local_extract_worker.py     Δ  same
    ├── synthesize_handler.py       ⊕  SQS Lambda handler
    └── local_synthesize_worker.py  ⊕  local-dev SQS poll loop over the same handler
```

`domain/marks.py` and `infrastructure/mp3.py` are **pure** — no `edge_tts`, no `boto3`, no network — precisely so the timing/alignment logic (the part phase 6's highlight sync actually depends on) can be tested exhaustively and instantly, exactly as phase 3 did with `layout.py`/`chunking.py`.

---

## 4. Fan-out: who enqueues the N messages

### 4.1 Why the extract Lambda, and why that's not a coupling problem

Three candidates were considered:

| option | verdict |
|---|---|
| **A. Extract Lambda publishes at the end of `ExtractBook`** | **Chosen.** |
| B. DynamoDB Stream on the book item, filtered to `status → EXTRACTED`, driving a fan-out Lambda | Rejected *for this phase*. It needs a stream on the table (`StorageStack` change), a third Lambda, stream event filtering, and — decisively — a **LocalStack stream poller** in the local worker pattern, since the whole local-dev story is "poll SQS in a compose service". Phase 3 chose S3→SQS over EventBridge for exactly this reason. Worth revisiting in phase 5, which may want streams anyway (see OQ-6). |
| C. `chunk_repository.save_all` publishes as a side effect | Rejected outright. A repository that emits messages is a repository that lies about being a repository; it would also publish *before* `chunksTotal` is set (§1, invariant 1) and would fire on every unrelated `save_all` call. |

The coupling concern is real but contained: `ExtractBook` gains a dependency on a **`SynthesisQueue` Protocol declared in `domain/synthesis.py`**, not on boto3, not on SQS, not on the synthesize use case. The application layer says "these chunks are ready for synthesis"; `infrastructure/sqs_synthesis_queue.py` is the only thing that knows that means SQS. Swapping to option B later means writing one new adapter and deleting one constructor argument — the use case does not change.

And this is what `IMPLEMENTATION_PLAN.md` literally specifies: *"Fan-out: one SQS message per chunk after extraction"* under Phase 4, plus the architecture decision *"one SQS-triggered Lambda invocation per chunk, fan-out/fan-in via an atomic `chunksDone` counter"*. Introducing a stream-driven indirection would be inventing a requirement.

### 4.2 Before or after the `EXTRACTED` flip? — **After.**

If the publish happened *before* the flip, a fast worker could finish and `increment_chunks_done` while `chunksTotal` is still `0`. In the worst case **all** N chunks finish before the flip: `chunksDone == N`, then the flip sets `chunksTotal = N`, and the "was I the last chunk?" edge that phase 5 keys off **never fires again**. That's an unrecoverable fan-in hole, and it's the decisive argument.

Publishing after the flip has one failure mode — the flip succeeds, the publish fails — which §4.3 handles.

### 4.3 `ExtractBook` changes

```python
Outcome = Literal["EXTRACTED", "FAILED", "SKIPPED", "REQUEUED"]

class ExtractBook:
    def __init__(self, book_repository, chunk_repository, pdf_storage,
                 extractor, clock, synthesis_queue: SynthesisQueue) -> None: ...
```

`execute` gains a branch **before** the claim:

```python
book = self._book_repository.get(...)          # unchanged
if book is None:            return SKIPPED("BOOK_NOT_FOUND")
if book.source_key != cmd.source_key: return SKIPPED("KEY_MISMATCH")

# NEW: a redelivery of an already-extracted book means the previous
# invocation's fan-out publish is not known to have completed (it is the
# only step after the EXTRACTED flip that can fail). Re-publish; never
# re-extract. Duplicate messages are safe by construction (§8.4).
if book.status is BookStatus.EXTRACTED and book.chunks_total > 0:
    self._synthesis_queue.enqueue_chunks(
        user_id=cmd.user_id, book_id=cmd.book_id,
        chunk_indexes=range(book.chunks_total),
    )
    return ExtractBookResult("REQUEUED", chunks_written=book.chunks_total)
```

`_CLAIMABLE_STATUSES` stays `(UPLOADED, FAILED)` — unchanged, and the existing comment stays accurate.

At the end of `_extract_and_persist`, after the existing `update_status(EXTRACTED, ...)` call (which now also passes `chunks_done=0, chunks_failed=0` — see §5.2):

```python
self._synthesis_queue.enqueue_chunks(
    user_id=command.user_id, book_id=command.book_id,
    chunk_indexes=range(len(chunks)),
)
```

A raise here propagates to `execute`'s existing `except Exception:` handler, which resets the book to `UPLOADED` and re-raises. **This is now wrong** and must be adjusted: resetting to `UPLOADED` after a successful `EXTRACTED` flip would cause a full re-extraction (deleting chunks under any workers that *did* get their message). So the publish moves **out of** `_extract_and_persist` into `execute`, sequenced after it:

```python
try:
    result = self._extract_and_persist(command, now)
except ExtractionError as exc:   ...  # unchanged
except Exception:                ...  # unchanged (release claim to UPLOADED, re-raise)

# Outside the claim-release guard: the book is already EXTRACTED and must
# stay that way. A failure here re-raises so SQS redelivers, and §4.3's
# pre-claim branch turns that redelivery into a publish-only retry.
self._synthesis_queue.enqueue_chunks(...)
return result
```

This restructuring is small but load-bearing, and gets its own test ("publish failure after a successful flip leaves the book `EXTRACTED`, not `UPLOADED`").

### 4.4 `SynthesisQueue` port + adapter

```python
# domain/synthesis.py
class SynthesisQueue(Protocol):
    def enqueue_chunks(self, *, user_id: str, book_id: str,
                       chunk_indexes: Iterable[int]) -> int: ...  # pragma: no cover
```

```python
# infrastructure/sqs_synthesis_queue.py
SYNTHESIS_MESSAGE_VERSION = 1
_BATCH_SIZE = 10   # SendMessageBatch hard cap

class SqsSynthesisQueue:
    def __init__(self, *, queue_url: str, sqs: Any | None = None) -> None: ...
    def enqueue_chunks(self, *, user_id, book_id, chunk_indexes) -> int: ...
```

Message body:

```json
{"v": 1, "userId": "<sub>", "bookId": "<uuid>", "chunkIndex": 7}
```

Implementation notes (all trap-shaped):
- `send_message_batch` caps at **10 entries** and 256 KB total; entries here are ~110 bytes, so batching is purely about call count (a 330-chunk book = 33 calls ≈ 1s).
- `Id` within a batch must be unique per call and ASCII-alphanumeric — use `str(index)`.
- `send_message_batch` **partially succeeds**: the response has both `Successful` and `Failed`. Retry the `Failed` entries **once**, then raise `RuntimeError` listing them. Silently dropping a failed entry means one chunk is never synthesized and the book never completes — a genuine correctness bug, not a nicety.
- Built through `src.infrastructure.aws.client("sqs")` so LocalStack works with zero code change.

---

## 5. Data model changes

### 5.1 S3 key layout — additions to `infrastructure/s3_keys.py`

```python
AUDIO_PREFIX = "audio/"
MARKS_PREFIX = "marks/"
AUDIO_EXTENSION = ".mp3"
MARKS_EXTENSION = ".json"
AUDIO_CONTENT_TYPE = "audio/mpeg"
MARKS_CONTENT_TYPE = "application/json"

def chunk_audio_key(user_id: str, book_id: str, index: int) -> str
def chunk_marks_key(user_id: str, book_id: str, index: int) -> str
def parse_chunk_audio_key(key: str) -> tuple[str, str, int] | None
def parse_chunk_marks_key(key: str) -> tuple[str, str, int] | None
```

- Padding comes from `from src.contexts.library.infrastructure.keys import CHUNK_INDEX_WIDTH` — **not** a hard-coded `06d`. The DynamoDB SK and the S3 key must never drift; `keys.py` already documents the width as immutable.
- Both buckets are dedicated, but the keys are still prefixed. Reason: phase 5 needs somewhere to put the stitched artifacts (`audio/<u>/<b>/full.mp3`, `marks/<u>/<b>/full.json`) without colliding with a chunk index, and a later "one bucket for everything" consolidation stays possible.
- Parsers return `None` for anything malformed, same contract and same `[^/]+`-can't-match-`/` traversal defence as `parse_source_pdf_key`. Round-trip and rejection tests go into the existing `test_s3_keys.py`.

Unlike `SOURCE_PREFIX`, these prefixes are **not** duplicated in `infra/stacks/config.py` — nothing in CDK filters on them (no S3 notification on these buckets). No lockstep comment needed, and one is deliberately not added.

### 5.2 Domain changes

`ChunkStatus` gains, with the existing inline-comment convention:

```python
SYNTHESIZING = "SYNTHESIZING"   # first SET in phase 4 (synthesize Lambda claim)
```

**Accepted deploy-order risk, stated explicitly** (identical in shape to phase 3's OQ-5): CDK orders `Storage → Pipeline → Api`, so the writer (synthesize Lambda) deploys before the reader (API Lambda). A warm old API container hitting `GET /books/{id}/chunks` for a chunk mid-claim would get `ValidationError` from `ChunkStatus.parse` → `400`. Seconds-to-minutes, single user, no traffic during deploy. Accepted.

New enums in `value_objects.py`:

```python
class SynthesisFailure(StrEnum):
    """Permanent chunk-synthesis failure reasons -- stored as
    Chunk.failure_reason when status == FAILED."""
    EMPTY_TEXT         = "EMPTY_TEXT"
    TEXT_TOO_LONG      = "TEXT_TOO_LONG"
    ALL_ENGINES_FAILED = "ALL_ENGINES_FAILED"
    UNKNOWN            = "UNKNOWN"

class SynthesisSource(StrEnum):
    EDGE_TTS   = "edge-tts"
    GOOGLE_TTS = "google-tts"

class MarksTiming(StrEnum):
    MEASURED  = "measured"    # per-word events from the engine (edge-tts)
    ESTIMATED = "estimated"   # interpolated between sentence anchors (google)
```

`Chunk` gains:

| field | type | notes |
|---|---|---|
| `duration_ms` | `int` (default `0`) | measured MP3 duration; phase 5's offset math and phase 6's seek both need it |
| `failure_reason` | `str \| None` | `SynthesisFailure` value; `None` unless `status == FAILED` |
| `synthesis_source` | `str \| None` | `SynthesisSource` value; lets you answer "did the fallback kick in?" without downloading the marks JSON |

`Book` gains `chunks_failed: int` (default `0`), mirroring `chunks_done`. `Book.create` sets it to `0`. `book_to_dict` exposes `chunksFailed`; `chunk_to_dict` exposes `durationMs`, `failureReason`, `synthesisSource`.

All defaults keep phase 2/3 fixtures and mappers compiling; mappers already use `item.get(...)` everywhere.

`BookStatus` is **unchanged** (Q4).

### 5.3 `ChunkRepository.update_status` — the same generalization phase 3 gave `BookRepository`

Current signature has no conditional-claim support at all. New port signature:

```python
def update_status(
    self,
    book_id: str,
    index: int,
    status: ChunkStatus,
    *,
    expected_statuses: Sequence[ChunkStatus] | None = None,
    audio_key: str | None = None,
    marks_key: str | None = None,
    duration_ms: int | None = None,
    synthesis_source: str | None = None,
    failure_reason: str | None = None,
    clear_failure_reason: bool = False,
) -> None: ...
```

Adapter implementation notes — deliberately a line-for-line mirror of `dynamodb_book_repository.update_status`, so the two read identically:

- `SET #status = :status[, audioKey = :ak][, marksKey = :mk][, durationMs = :dm][, synthesisSource = :ss][, failureReason = :fr]`, optionally suffixed with ` REMOVE failureReason`.
- `failure_reason is not None and clear_failure_reason` → `ValueError` (programming error).
- `ConditionExpression` is `attribute_exists(PK)`, **AND** `#status IN (:exp0, …)` when `expected_statuses` is given. `#status` alias retained (reserved word) — keep the existing comment.
- **On `ConditionalCheckFailedException`, disambiguate**: re-`get`; absent → `NotFoundError` (preserves phase-3 behaviour + its tests), present → `ConflictError`. This is the single most important line in the phase — it is what makes the terminal transition an exactly-once gate (§8.4).
- Existing phase-3 call sites (none in prod, several in tests) keep working unchanged.

There is deliberately **no** `not_status` kwarg. "Anything but `DONE`" is expressed as `expected_statuses=NON_TERMINAL_CHUNK_STATUSES` where

```python
# domain/value_objects.py — the exact complement of DONE, so "not yet
# finished" stays a single named concept instead of a negation operator
# smeared across the adapter.
NON_TERMINAL_CHUNK_STATUSES = (ChunkStatus.PENDING, ChunkStatus.SYNTHESIZING, ChunkStatus.FAILED)
```

### 5.4 `increment_chunks_done` — what it needs, without painting phase 5 into a corner

Today it returns a bare `int` (`chunksDone`) from `ReturnValues="UPDATED_NEW"`. Phase 5's stitcher trigger needs to know **"is `chunksDone` now equal to `chunksTotal`?"** at the moment of increment. Reading the book again afterwards is a race (two workers could both read after both increments and both think they're last, or neither). The fix is free: `ReturnValues="ALL_NEW"` returns the whole post-update item in the same round trip.

```python
# domain/repository.py
@dataclass(frozen=True)
class ChunkCounters:
    chunks_done: int
    chunks_total: int
    chunks_failed: int

    @property
    def is_complete(self) -> bool:
        # chunks_total > 0 guards the "publish happened before the flip"
        # class of bug (§4.2) from silently reading as "complete".
        return self.chunks_total > 0 and self.chunks_done >= self.chunks_total

# BookRepository
def increment_chunks_done(self, user_id: str, book_id: str, *,
                          failed: bool = False) -> ChunkCounters: ...
```

Adapter: one `UpdateItem`, `UpdateExpression="ADD chunksDone :one" + (", chunksFailed :one" if failed else "")`, `ConditionExpression="attribute_exists(PK)"`, `ReturnValues="ALL_NEW"`, `NotFoundError` on conditional failure (unchanged). Reads `chunksDone`/`chunksTotal`/`chunksFailed` with `int(attrs.get(name, 0))` (boto3 resource API returns `Decimal`).

Phase 4 **uses** `ChunkCounters` only for structured logging (`"chunk 7/330 done for book X"`). Phase 5 will use `.is_complete` to trigger stitching with no extra read. That's the "don't paint into a corner" answer: yes, change it, and change it now while there is exactly one caller.

`BookRepository.update_status` additionally gains `chunks_done: int | None = None` and `chunks_failed: int | None = None`, used by the `EXTRACTED` flip to **reset both counters to 0**. Without this, re-extracting a previously-`FAILED` book (phase 3 explicitly supports that: `FAILED` is in `_CLAIMABLE_STATUSES`) leaves a stale `chunksDone` and the fan-in arithmetic is wrong forever.

### 5.5 Item shapes (additive only)

Chunk item gains `durationMs` (N), `synthesisSource` (S), `failureReason` (S, **absent** rather than NULL when unset — hence `REMOVE`). Book item gains `chunksFailed` (N).

---

## 6. Infrastructure (CDK)

### 6.1 `PipelineStack` signature

```python
def __init__(self, scope, construct_id, *, environment: str,
             pdf_bucket_name: str,
             audio_bucket: s3.IBucket,        # NEW
             marks_bucket: s3.IBucket,        # NEW
             table: dynamodb.Table,
             google_tts_secret_name: str = "",  # NEW, default "" = fallback disabled
             git_sha: str = "local", **kwargs) -> None
```

**Deliberate asymmetry, must be commented:** `pdf_bucket` is threaded as a *name* and re-imported, because phase 3 needed the S3→SQS notification and the queue policy to land in the same stack (the circular-dependency trap, §6.1 of phase 3). `audio_bucket`/`marks_bucket` need **only IAM grants** — no notification, no bucket policy — so passing the real constructs creates a plain `Pipeline → Storage` dependency, which already exists. Importing them by name would work too but would lose CDK's grant ergonomics for nothing.

`infra/app.py` passes `audio_bucket=storage.audio_bucket, marks_bucket=storage.marks_bucket` and `google_tts_secret_name=app.node.try_get_context("google_tts_secret_name") or Config.GOOGLE_TTS_SECRET_NAME`.

### 6.2 Queue shape change

`_queue_with_dlq` gains `max_receive_count: int = 3`:

```python
self.synthesize_queue = self._queue_with_dlq(
    "Synthesize", queue_name("synthesize", environment),
    # 6x the synthesize Lambda's 300s timeout, same rule as extract_queue.
    visibility_timeout=cdk.Duration.minutes(30),
    # 5, not 3: throttling from the ESM's MaximumConcurrency returns the
    # message to the queue and *does* bump ApproximateReceiveCount, so a
    # 3-attempt budget can be consumed by backpressure alone, DLQ-ing
    # perfectly good chunks on a large book. Kept in lockstep with
    # backend/src/config.py's synthesize_max_receive_count (§8.3).
    max_receive_count=Config.SYNTHESIZE_MAX_RECEIVE_COUNT,
)
```

`extract_queue` is unchanged (12 min / 3).

### 6.3 The synthesize Lambda

```python
synthesize_fn = lambda_.DockerImageFunction(
    self, "SynthesizeFunction",
    code=lambda_.DockerImageCode.from_image_asset(
        backend_dir,
        cmd=["src.contexts.library.interface.synthesize_handler.handler"],
    ),
    # I/O-bound (websocket + HTTPS), not CPU-bound like extraction. 1024 MB
    # is chosen for Lambda's memory-proportional *network* bandwidth, not
    # for compute; the only CPU work is the MP3 frame scan.
    memory_size=1024,
    timeout=cdk.Duration.seconds(300),
    # Backstop only. The ESM's max_concurrency below is the real throttle;
    # this is deliberately HIGHER than it, because CreateEventSourceMapping
    # rejects a ScalingConfig.MaximumConcurrency that exceeds the function's
    # reserved concurrency.
    reserved_concurrent_executions=10,
    environment={
        Config.ENV_ENVIRONMENT: environment,
        Config.ENV_GIT_SHA: git_sha,
        Config.ENV_TABLE_NAME: table.table_name,
        Config.ENV_AUDIO_BUCKET: audio_bucket.bucket_name,
        Config.ENV_MARKS_BUCKET: marks_bucket.bucket_name,
        Config.ENV_SYNTHESIZE_QUEUE_URL: self.synthesize_queue.queue_url,
        Config.ENV_EDGE_TTS_VOICE: Config.DEFAULT_EDGE_TTS_VOICE,
        Config.ENV_GOOGLE_TTS_VOICE: Config.DEFAULT_GOOGLE_TTS_VOICE,
        Config.ENV_GOOGLE_TTS_SECRET_NAME: google_tts_secret_name,
        Config.ENV_SYNTHESIZE_MAX_RECEIVE_COUNT: str(Config.SYNTHESIZE_MAX_RECEIVE_COUNT),
        Config.ENV_LOG_LEVEL: "INFO",
    },
)
synthesize_fn.add_event_source(
    lambda_event_sources.SqsEventSource(
        self.synthesize_queue,
        batch_size=1,
        max_concurrency=5,   # ScalingConfig.MaximumConcurrency; minimum allowed is 2
    )
)
```

**Third function, same image.** Exactly phase 3's reasoning: `DockerImageAsset`'s hash comes from the source directory + build args + target + platform, **not** from `cmd`. One ECR image, three Lambdas (`ApiFunction`, `ExtractFunction`, `SynthesizeFunction`), three `ImageConfig.Command`s.

**Reserved-concurrency reasoning, concretely.** A 300-page book chunks to roughly 330 chunks at 1800 chars. edge-tts synthesizes a ~2-minute utterance in ~10–25s wall clock (it streams faster than realtime over a websocket). At concurrency 5 that's `330 × 20s / 5 ≈ 22 minutes` for a large book; at 10 it's ~11 minutes; at 1 it's nearly two hours. The upper bound isn't cost (Lambda at 1 GB for 330×20s is under $0.10 per book) — it's that **the Edge Read Aloud endpoint is an undocumented free consumer service with no published rate limit**, and community reports of `403`/`429` under sustained parallel load from datacenter IPs are common. Five concurrent websocket sessions from one Lambda account is roughly "one person with five browser tabs" and is defensible; 50 is abuse and will get throttled or blocked. **5 is the recommendation**, expressed as a single `Config.SYNTHESIZE_MAX_CONCURRENCY = 5` constant so it is a one-line change if the 403 rate stays at zero and throughput starts mattering.

Why `max_concurrency` on the ESM rather than only `reserved_concurrent_executions`: with reserved concurrency alone, the SQS poller keeps receiving messages, the invocations get **throttled**, the messages return to the queue after the visibility timeout, and `ApproximateReceiveCount` climbs — burning the DLQ budget on backpressure. `ScalingConfig.MaximumConcurrency` exists specifically to make the poller itself back off. Using both, with reserved > ESM max, is the belt-and-braces configuration.

`batch_size=1` for the same reason as phase 3: one pathological chunk can never poison a batch, and partial-batch-failure reporting becomes unnecessary.

### 6.4 Google API key — Secrets Manager, out-of-band, optional

```python
if google_tts_secret_name:
    secret = secretsmanager.Secret.from_secret_name_v2(
        self, "GoogleTtsSecret", google_tts_secret_name
    )
    secret.grant_read(synthesize_fn)   # also grants kms:Decrypt where needed
```

- **One shared secret across every environment**, name `bookloud/google-tts-api-key`. Env-scoping it would mean creating a secret per ephemeral PR stack, which is absurd for a personal app; a single-user app with one GCP key has nothing to isolate.
- The secret is **created out of band** (`aws secretsmanager create-secret`, documented in the README). CDK never sees the value — putting it in a stack parameter or environment variable would bake it into the CloudFormation template.
- **The whole thing is optional.** If `google_tts_secret_name` is `""` (the default, and what every PR env gets unless a human sets the context value), the grant is skipped, `GOOGLE_TTS_SECRET_NAME` is empty, and `get_speech_synthesizer()` returns a bare `EdgeTtsSynthesizer` with no fallback wrapper, logging a single `INFO`. This is what keeps the phase deployable and CI-green before any human touches GCP.
- **API key, not a service account.** Cloud Text-to-Speech supports API-key auth on the REST endpoint (`?key=…`), the key can be restricted to that single API in the GCP console, and it avoids shipping a service-account JSON, a JWT signer, and an OAuth token cache into a Lambda for one fallback call. `secrets.py` caches the fetched value in a module-level variable so warm invocations don't re-hit Secrets Manager.

### 6.5 IAM grants (all via CDK `grant*`; no hand-written policy documents)

| grant | why |
|---|---|
| `audio_bucket.grant_put(synthesize_fn)` | `s3:PutObject` for the MP3. `grant_put`, not `grant_read_write` — the synthesize Lambda never reads or deletes audio |
| `marks_bucket.grant_put(synthesize_fn)` | same, for the marks JSON |
| `table.grant_read_write_data(synthesize_fn)` | `GetItem` (chunk text), `UpdateItem` (claim, terminal transition, counter) |
| `secret.grant_read(synthesize_fn)` | `secretsmanager:GetSecretValue` (+ `kms:Decrypt`), **only when a secret name is configured** |
| `SqsEventSource(synthesize_queue)` (implicit) | `ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` |
| **`synthesize_queue.grant_send_messages(extract_fn)`** | the fan-out producer grant — the one easy-to-forget grant in the phase |
| `extract_fn` env | `+ Config.ENV_SYNTHESIZE_QUEUE_URL` |
| **API Lambda** | **nothing new** — `audio_bucket`/`marks_bucket` `grant_read_write` and both env vars already exist since phase 2 |

New `CfnOutput`: `SynthesizeFunctionName`.

### 6.6 `infra/stacks/config.py` additions

```python
ENV_SYNTHESIZE_QUEUE_URL        = "SYNTHESIZE_QUEUE_URL"
ENV_EDGE_TTS_VOICE              = "EDGE_TTS_VOICE"
ENV_GOOGLE_TTS_VOICE            = "GOOGLE_TTS_VOICE"
ENV_GOOGLE_TTS_SECRET_NAME      = "GOOGLE_TTS_SECRET_NAME"
ENV_SYNTHESIZE_MAX_RECEIVE_COUNT = "SYNTHESIZE_MAX_RECEIVE_COUNT"

DEFAULT_EDGE_TTS_VOICE   = "en-US-AriaNeural"
DEFAULT_GOOGLE_TTS_VOICE = "en-US-Neural2-C"
GOOGLE_TTS_SECRET_NAME   = "bookloud/google-tts-api-key"
SYNTHESIZE_MAX_RECEIVE_COUNT = 5
SYNTHESIZE_MAX_CONCURRENCY   = 5
```

with the standard "separately deployed projects, cannot share an import; rename both or neither" lockstep comment pointing at `backend/src/config.py`.

### 6.7 Infra test impact (`infra/tests/test_synth.py`)

`_synth_pipeline_stack` gains `audio_bucket=storage.audio_bucket, marks_bucket=storage.marks_bucket` (and a variant passing `google_tts_secret_name="test-secret"`). Assertions to update/add:

- `AWS::Lambda::Function` count `2 → 3` (extract, synthesize, notifications handler).
- `AWS::Lambda::EventSourceMapping` count `1 → 2`; the synthesize mapping has `BatchSize: 1` and `ScalingConfig: {MaximumConcurrency: 5}`.
- Synthesize function has `ReservedConcurrentExecutions: 10` and `Timeout: 300`.
- Synthesize queue `VisibilityTimeout == 1800`; its `RedrivePolicy.maxReceiveCount == 5`; extract queue still `720` / `3`.
- IAM actions across the stack include `s3:PutObject`, `sqs:SendMessage` (the extract → synthesize grant), and — in the secret-configured variant — `secretsmanager:GetSecretValue`.
- New negative test: with `google_tts_secret_name=""`, **no** `secretsmanager:*` action appears anywhere.
- `test_pipeline_stack_naming_convention` gains the two new kwargs.

CI's `test-infra` job runs on `ubuntu-latest` (Docker present) and already tolerates docker-marked PipelineStack tests. **No workflow change anywhere in this phase.**

---

## 7. Synthesis engines

### 7.1 Dependency

`backend/pyproject.toml` `[project].dependencies` += `"edge-tts>=7.0,<8"`; regenerate `uv.lock`. Verified transitive set: `aiohttp<4`, `certifi`, `tabulate`, `typing-extensions` — all with manylinux wheels for `x86_64`, so `uv pip install --target ${LAMBDA_TASK_ROOT}` inside `public.ecr.aws/lambda/python:3.12` will not try to compile anything. **No `google-cloud-texttospeech`** (it drags in `grpcio`, `protobuf`, and the full `google-api-core` stack for one HTTP POST). `edge_tts` may be imported **only** by `infrastructure/edge_tts_synthesizer.py`.

### 7.2 Ports and value objects — `domain/synthesis.py`

```python
@dataclass(frozen=True)
class WordMark:
    text: str
    offset_ms: int      # from the start of THIS chunk's audio
    duration_ms: int
    char_start: int     # into the chunk's text; filled by align/estimate, not the engine
    char_end: int

@dataclass(frozen=True)
class SynthesizedAudio:
    audio: bytes
    content_type: str            # "audio/mpeg"
    duration_ms: int
    marks: tuple[WordMark, ...]
    voice: str
    source: SynthesisSource
    timing: MarksTiming

class SpeechSynthesizer(Protocol):
    @property
    def name(self) -> str: ...                                   # pragma: no cover
    def synthesize(self, text: str) -> SynthesizedAudio: ...     # pragma: no cover

class SynthesisError(DomainError): ...                # base, status_code 422
class UnsynthesizableText(SynthesisError): ...        # PERMANENT
    # carries .reason: SynthesisFailure
class SynthesisUnavailable(SynthesisError): ...       # TRANSIENT (engine/network)
```

Deliberately **sync**, not async. The rest of the codebase (FastAPI route handlers, repositories, phase 3's handler) is synchronous; a single `asyncio.run(...)` inside the edge-tts adapter keeps the async surface confined to one file, which is exactly where it belongs.

Limits, declared once in `domain/synthesis.py` next to the port:

```python
MAX_SYNTHESIS_CHARS = 4000   # comfortably above chunking.DEFAULT_MAX_CHARS (2600)
```

### 7.3 `EdgeTtsSynthesizer` — `infrastructure/edge_tts_synthesizer.py`

```python
class EdgeTtsSynthesizer:
    name = SynthesisSource.EDGE_TTS.value

    def __init__(self, *, voice: str = DEFAULT_VOICE,
                 attempts: int = 2, timeout_seconds: float = 75.0,
                 communicate_cls: type | None = None) -> None: ...
    def synthesize(self, text: str) -> SynthesizedAudio: ...
```

`communicate_cls` defaults to `edge_tts.Communicate` and exists **solely so tests can inject a fake** with no network and no monkeypatching of a third-party module's globals (§10).

The async core:

```python
async def _stream(self, text: str) -> tuple[bytes, list[tuple[str, int, int]]]:
    communicate = self._communicate_cls(
        text,
        self._voice,
        # LOAD-BEARING: edge-tts >= 7.x defaults `boundary` to
        # "SentenceBoundary". Word-level highlighting needs WordBoundary
        # events, so this must be passed explicitly -- omitting it silently
        # produces one mark per sentence and a highlight that lurches.
        boundary="WordBoundary",
        connect_timeout=10,
        receive_timeout=30,
    )
    audio = bytearray()
    words: list[tuple[str, int, int]] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            # offset/duration are in 100-nanosecond "ticks"
            # (TICKS_PER_SECOND = 10_000_000).
            words.append((chunk["text"],
                          chunk["offset"] // TICKS_PER_MS,
                          chunk["duration"] // TICKS_PER_MS))
    return bytes(audio), words
```

`TICKS_PER_MS = 10_000`. The whole thing runs under `asyncio.run(asyncio.wait_for(self._stream(text), self._timeout_seconds))`.

Post-processing, in order:
1. Empty audio (or `NoAudioReceived`) → raise `SynthesisUnavailable`.
2. `duration_ms = mp3_duration_ms(audio)` (§7.6). If the parser can't find a single valid frame → `SynthesisUnavailable` (the bytes aren't MP3, something is badly wrong).
3. `char_start`/`char_end` via `marks.align_words(text, [w.text for w in words])` — edge-tts returns the word **text** but no offset into the input, so alignment is our job (§7.5).
4. `timing = MarksTiming.MEASURED`, `source = EDGE_TTS`.

Retry policy: up to `attempts=2` with a 1.5 s sleep between, catching `edge_tts.exceptions.EdgeTTSException` and `Exception` broadly. On exhaustion, raise `SynthesisUnavailable` with the last exception chained. **Broad catching is deliberate and documented**: the free endpoint's failure surface (`NoAudioReceived`, `UnexpectedResponse`, `UnknownResponse`, `WebSocketError`, `SkewAdjustmentError`, plus raw `aiohttp.ClientError`/`asyncio.TimeoutError`/`ssl.SSLError`) is undocumented, changes between edge-tts releases, and includes the `Sec-MS-GEC` DRM token dance that Microsoft added in 2024. There is no failure mode of edge-tts that we want to treat as permanent — every one of them is a reason to try Google.

Reliability notes for the implementer, recorded so they aren't rediscovered in an incident: the endpoint requires a clock-derived DRM token (Lambda clocks are NTP-accurate, and edge-tts self-corrects skew on `403`); Microsoft has been observed rate-limiting and occasionally blocking datacenter IP ranges; there is no SLA, no support, and no announcement channel. This is precisely why `IMPLEMENTATION_PLAN.md` specifies a fallback, and why §8.3 treats a double failure as transient rather than as a permanently broken chunk.

### 7.4 `GoogleTtsSynthesizer` — `infrastructure/google_tts_synthesizer.py`

```python
class GoogleTtsSynthesizer:
    name = SynthesisSource.GOOGLE_TTS.value

    def __init__(self, *, api_key: str, voice: str = DEFAULT_GOOGLE_VOICE,
                 attempts: int = 2, timeout_seconds: float = 45.0,
                 http_post: Callable[[str, bytes, float], bytes] | None = None) -> None: ...
    def synthesize(self, text: str) -> SynthesizedAudio: ...
```

`http_post` defaults to a tiny `urllib.request` implementation and is injected in tests.

Request — `POST https://texttospeech.googleapis.com/v1beta1/text:synthesize?key=<API_KEY>`:

```json
{
  "input":  {"ssml": "<speak><mark name=\"s0\"/>First sentence. <mark name=\"s1\"/>Second.</speak>"},
  "voice":  {"languageCode": "en-US", "name": "en-US-Neural2-C"},
  "audioConfig": {"audioEncoding": "MP3", "sampleRateHertz": 24000},
  "enableTimePointing": ["SSML_MARK"]
}
```

Response: `{"audioContent": "<base64 mp3>", "timepoints": [{"markName": "s0", "timeSeconds": 0.0}, …]}`.

**Why marks per sentence and not per word.** Google Cloud TTS does **not** emit per-word boundary events. The only timing mechanism is `enableTimePointing: ["SSML_MARK"]`, which reports the audio time of each `<mark>` you place yourself. Two hard constraints kill the naive per-word approach:

- **The input is capped at 5000 bytes *including* SSML markup.** A 1800-char chunk is ~300 words; `<mark name="w123"/>` is ~19 bytes, so per-word marks add ~5700 bytes — over the cap before the text is even counted.
- Google's own SSML guidance says *"do not add consecutive marks … marks in rapid succession might not generate events"*, which is exactly what per-word marking does. There are also live reports of `v1beta1` timepointing truncating after the first sentence.

So: one `<mark name="sN"/>` before each sentence, using `chunking.split_sentences(text)` (already in the domain, already tested, and already the same splitter the chunker itself uses). A 1800-char chunk is ~12 sentences → ~200 bytes of markup. Well within the cap, no consecutive marks, no truncation pressure.

The sentence timepoints become **anchors**, and `marks.estimate_word_marks(text, duration_ms, anchors)` interpolates each word's start time proportionally by character position **within its sentence** (§7.5). The result is exact at every sentence boundary and linearly interpolated in between — for a ~10-word sentence, worst-case per-word error is a fraction of a second, and it re-syncs every sentence rather than drifting across the whole chunk. `timing = MarksTiming.ESTIMATED` records this honestly in the marks JSON so phase 6 can, if it ever wants to, soften the highlight animation for estimated chunks.

Defensive degradations, in order:
- If the assembled SSML exceeds `GOOGLE_MAX_SSML_BYTES = 4800`, **drop the marks** and send plain text; anchors become just `[(0, 0), (len(text), duration_ms)]` — pure proportional estimation over the whole chunk. Degrading the timing beats failing the chunk.
- If `timepoints` comes back empty or truncated (fewer than 2 entries), same fallback.
- Marks whose `markName` doesn't parse, or whose times aren't monotonic, are discarded before anchoring.

SSML escaping: `&amp;`, `&lt;`, `&gt;`, `"` — a dedicated `_escape_ssml` with its own test, because an unescaped ampersand in a book's text would produce a 400 from Google on exactly the chunks that need the fallback most.

Errors: HTTP `429`/`5xx` → retry once after 2 s, then `SynthesisUnavailable`. HTTP `400`/`403` (bad key, API not enabled, quota exhausted) → `SynthesisUnavailable` **without** retry, logging the response body's `error.message` at `ERROR` — it's a configuration problem the operator needs to see, but it is still not a reason to permanently fail the chunk (fix the key, redrive, done).

**Free-tier constraints.** Google's free tier is 4M chars/month Standard and 1M chars/month WaveNet/Neural2. A 300-page book is roughly 600k characters. If Google were the *primary* engine, Neural2 would cover about 1.5 books a month — a real constraint. As a **fallback that fires only on individual edge-tts failures**, realistic usage is tens to low hundreds of chunks per month, i.e. under 0.5M characters — comfortably inside even the Neural2 tier. `Neural2` is therefore the recommended default (`en-US-Neural2-C`) rather than Standard: a fallback chunk dropped into the middle of an edge-tts book will sound different regardless, and a robotic Standard voice makes that jarring rather than merely noticeable. Worth noting for completeness: if edge-tts ever goes fully dark and Google silently becomes the primary path for a whole book, one 300-page book would consume ~60% of the monthly Neural2 tier and then start billing at ~$16/M chars. That is a *good* failure mode (it works, it costs a few dollars) but it should not be a surprise — hence a `WARNING` log whenever the fallback fires, and OQ-E.

### 7.5 Marks — `domain/marks.py` (pure)

```python
MARKS_SCHEMA_VERSION = 1

def iter_words(text: str) -> Iterator[tuple[int, int, str]]:
    """(char_start, char_end, word) for every whitespace-delimited token.
    THE single tokenizer -- alignment and estimation must agree or the two
    engines produce mutually incompatible marks files."""

def align_words(text: str, word_texts: Sequence[str]) -> list[tuple[int, int]]:
    """Map engine-reported word strings back onto char ranges in `text`."""

def estimate_word_marks(text: str, *, duration_ms: int,
                        anchors: Sequence[tuple[int, int]]) -> list[WordMark]:
    """anchors = sorted (char_offset, time_ms), always including (0, 0) and
    (len(text), duration_ms)."""

def build_marks_document(*, book_id: str, chunk_index: int, user_id: str,
                         audio_key: str, char_start: int, char_end: int,
                         audio: SynthesizedAudio) -> dict
```

`align_words` algorithm: a forward cursor plus `text.find(word, cursor)`. On a hit, emit `(pos, pos + len(word))` and set `cursor = pos + len(word)`. On a miss (or a hit implausibly far ahead — beyond `cursor + 200` chars, meaning we've desynchronized), emit `(cursor, cursor + len(word))` and advance the cursor by `len(word)` **without** consuming a real match, so one bad token can't cascade. Both branches are explicitly tested. The extractor already NFKC-normalizes the text (phase 3 §7.5 step 8) and we send that exact text to edge-tts, so exact matches are overwhelmingly the norm; the fallback exists for punctuation-attachment and ligature edge cases.

**Marks JSON, written to `marks_bucket`:**

```json
{
  "version": 1,
  "bookId": "6f1a…",
  "chunkIndex": 7,
  "source": "edge-tts",
  "voice": "en-US-AriaNeural",
  "timing": "measured",
  "audioKey": "audio/8a2c…/6f1a…/000007.mp3",
  "charStart": 12600,
  "charEnd": 14400,
  "durationMs": 118240,
  "wordCount": 302,
  "words": [
    {"t": 0,    "d": 320, "s": 0,  "e": 7,  "w": "Chapter"},
    {"t": 340,  "d": 180, "s": 8,  "e": 11, "w": "one"}
  ]
}
```

Shape rationale, since phase 6 consumes this:

- **`words` is sorted by `t`, strictly non-decreasing** — asserted at build time, because phase 6's highlight sync is a `bisect_right` over `t` on every `timeupdate` event (~4 Hz). A single out-of-order entry silently corrupts the search. `build_marks_document` sorts and asserts.
- **`s`/`e` are char offsets *relative to the chunk text*, not the book.** The reader renders one chunk's text into the DOM; relative offsets index straight into it. `charStart`/`charEnd` at the document level give the book-absolute mapping when it's needed (phase 7's chat anchoring), so nothing is lost.
- **Short keys.** 300 words × 5 keys; long names would roughly double the file for zero benefit. ~15 KB per chunk, ~5 MB for a 330-chunk book — a rounding error in S3, and each file is fetched independently anyway.
- `durationMs` at the document level is the **measured** MP3 duration, not `words[-1].t + words[-1].d` (which omits trailing silence). Phase 5 sums these to compute per-chunk audio offsets; getting it from the last word instead would accumulate drift over 330 chunks.
- `t`/`d` are milliseconds (integers). HTML `<audio>.currentTime` is float seconds; ms integers avoid float-equality traps in the binary search and are the natural unit after dividing edge-tts's 100 ns ticks.
- **Rejected alternative:** parallel arrays (`{"t":[…],"d":[…],"w":[…]}`) — ~25% smaller and marginally faster to `bisect`, but unreadable in the S3 console when debugging a desync, which is the single most likely thing to go wrong in phase 6. Array-of-objects wins. (OQ-3, decided.)

### 7.6 `mp3_duration_ms` — `infrastructure/mp3.py` (pure)

```python
def mp3_duration_ms(data: bytes) -> int:
    """Sum frame durations from MPEG audio frame headers. Returns 0 when no
    valid frame is found (caller treats that as a synthesis failure)."""
```

Why this exists rather than a `mutagen`/`pydub` dependency: it is ~60 lines of table lookups, it's needed on the **Google path before any timing can be computed at all**, phase 5 needs exact durations for its offset math, and adding an audio library (and, for pydub, an ffmpeg binary) to the Lambda image for a byte-counting exercise is not justified.

Implementation: skip an `ID3` header if present (`b"ID3"` + syncsafe size); then scan for `0xFF Ex` sync words; decode MPEG version (1 / 2 / 2.5), layer, bitrate index, sample-rate index, padding bit; `frame_len = (samples_per_frame // 8) * bitrate // sample_rate + padding`; accumulate `samples_per_frame / sample_rate`. `samples_per_frame` is **1152 for MPEG-1 Layer III but 576 for MPEG-2/2.5 Layer III** — and edge-tts's default output is `audio-24khz-48kbitrate-mono-mp3`, i.e. **MPEG-2**, so getting this wrong yields exactly 2× the true duration on the primary path. Both branches get a test.

### 7.7 `FallbackSynthesizer` — where the decision tree lives

```python
class FallbackSynthesizer:
    """Implements SpeechSynthesizer by composing two of them. This is the
    ONLY place that knows edge-tts is primary and Google is the fallback --
    both adapters are engine-agnostic peers."""
    name = "fallback"

    def __init__(self, primary: SpeechSynthesizer, fallback: SpeechSynthesizer) -> None: ...

    def synthesize(self, text: str) -> SynthesizedAudio:
        try:
            return self._primary.synthesize(text)
        except UnsynthesizableText:
            raise                       # permanent: the fallback would fail identically
        except Exception as exc:
            logger.warning("primary synthesizer %s failed, falling back to %s: %s",
                           self._primary.name, self._fallback.name, exc)
        return self._fallback.synthesize(text)   # its own failures propagate as-is
```

`UnsynthesizableText` (empty/oversized text) is re-raised without trying the fallback, because it is a property of the input, not of the engine. Everything else falls through. This class is ~20 lines and gets the phase's most important behavioural test with two fakes and zero network.

---

## 8. The synthesize Lambda: error handling, idempotency, ordering

### 8.1 Handler — `interface/synthesize_handler.py`

```python
def handler(event: dict, context: object) -> None
def handle_records(records: Sequence[dict], use_case: SynthesizeChunk) -> list[SynthesizeChunkResult]
def command_from_record(record: dict) -> SynthesizeChunkCommand | None
```

- Composition root built **per invocation**, never at import — same rule as `extract_handler.py`, and what lets `mock_aws()` and LocalStack both work.
- `command_from_record` parses the JSON body, ignores anything unparseable or with a `v` it doesn't recognize (logged at `WARNING`, message deleted — a poison message must not loop forever), and reads the attempt number:
  ```python
  attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))
  ```
  SQS delivers this as a **string**; the ESM always populates it. It is the input to §8.3's last-attempt rule.

### 8.2 `SynthesizeChunk` use case — `application/synthesis.py`

```python
@dataclass(frozen=True)
class SynthesizeChunkCommand:
    user_id: str
    book_id: str
    chunk_index: int
    attempt: int = 1

@dataclass(frozen=True)
class SynthesizeChunkResult:
    outcome: Literal["DONE", "FAILED", "SKIPPED"]
    reason: str | None = None
    duration_ms: int = 0
    source: str | None = None
    chunks_done: int = 0
    chunks_total: int = 0

class SynthesizeChunk:
    def __init__(self, book_repository: BookRepository,
                 chunk_repository: ChunkRepository,
                 synthesizer: SpeechSynthesizer,
                 audio_storage: ObjectStorage,
                 marks_storage: ObjectStorage,
                 max_attempts: int = 5) -> None: ...
```

Sequence:

1. `chunk = chunk_repository.get(book_id, index)` → `None` → `SKIPPED("CHUNK_NOT_FOUND")` (the book was deleted or re-extracted with fewer chunks).
2. `chunk.status is DONE` → `SKIPPED("ALREADY_DONE")`. The cheap duplicate-delivery short-circuit; no engine call, no S3 write.
3. `chunk.user_id != command.user_id` → `SKIPPED("USER_MISMATCH")` (defence in depth; mirrors `ExtractBook`'s `KEY_MISMATCH` check).
4. **Validate the text before any engine call** — this is the *only* place permanence is decided proactively:
   - `not chunk.text.strip()` → `UnsynthesizableText(EMPTY_TEXT)`
   - `len(chunk.text) > MAX_SYNTHESIS_CHARS` → `UnsynthesizableText(TEXT_TOO_LONG)` (should be unreachable: the chunker's hard max is 2600, but a hand-seeded or future-re-chunked item shouldn't melt the Lambda)
5. **Claim**: `update_status(book_id, index, SYNTHESIZING, expected_statuses=NON_TERMINAL_CHUNK_STATUSES)`.
   - `ConflictError` → `SKIPPED("ALREADY_DONE")` (raced with a concurrent duplicate that just finished).
   - `NotFoundError` → `SKIPPED("CHUNK_NOT_FOUND")`.
   - Note the claim deliberately **includes `SYNTHESIZING`** in the expected set, i.e. it is "anything but `DONE`". A previous invocation that died hard (OOM, hard timeout, Lambda evicted) leaves the chunk in `SYNTHESIZING` with nothing to release it; excluding `SYNTHESIZING` would wedge that chunk permanently and there is no claim timestamp to expire. Allowing the re-claim costs, at worst, one duplicated synthesis — and §8.4 guarantees the counter is still incremented exactly once.
6. `audio = synthesizer.synthesize(chunk.text)`.
7. Write **audio first, then marks, then DynamoDB**:
   ```python
   audio_storage.put_bytes(key=audio_key, data=audio.audio, content_type=AUDIO_CONTENT_TYPE)
   marks_storage.put_bytes(key=marks_key,
                           data=json.dumps(document, separators=(",", ":")).encode("utf-8"),
                           content_type=MARKS_CONTENT_TYPE)
   ```
   Ordering rationale, mirroring phase 3's "chunks before the status flip": the chunk item is the **only** thing that ever advertises these keys, and it is written last, so a consumer that sees `audioKey` is guaranteed both objects exist. A crash between the two `put`s leaves an orphan MP3 that the next attempt overwrites at the identical key (the key is a pure function of `(user, book, index)` — every write is idempotent).
8. **Terminal transition + counter** (§8.4).

### 8.3 Permanent vs transient — the crux

| condition | reason code | chunk status | `chunksDone` | SQS message |
|---|---|---|---|---|
| empty / whitespace-only chunk text | `EMPTY_TEXT` | `FAILED` | **+1** (and `chunksFailed` +1) | deleted |
| chunk text over `MAX_SYNTHESIS_CHARS` | `TEXT_TOO_LONG` | `FAILED` | **+1** / +1 | deleted |
| chunk missing / user mismatch / already `DONE` | — | unchanged | — | deleted |
| edge-tts failed, Google succeeded | — | `DONE` (`source: google-tts`) | +1 | deleted |
| edge-tts failed, Google not configured, **attempt < 5** | — | released to `PENDING` | — | **retried** |
| **both** engines failed, **attempt < 5** | — | released to `PENDING` | — | **retried** |
| **both** engines failed, **attempt == 5** (last) | `ALL_ENGINES_FAILED` | `FAILED` | **+1** / +1 | deleted |
| S3 5xx / DynamoDB throttle, attempt < 5 | — | released to `PENDING` | — | retried |
| unexpected exception (a bug), attempt < 5 | — | released to `PENDING` | — | retried |
| unexpected exception, attempt == 5 | `UNKNOWN` | `FAILED` | **+1** / +1 | deleted |

Three decisions encoded here, each with a reason:

- **Engine failure is transient, not permanent.** Marking a chunk `FAILED` the first time Microsoft's free endpoint hiccups would permanently pit a book over a bad 30 seconds. Four retries across up to two hours of visibility-timeout backoff is the right posture for a service with no SLA.
- **The last attempt converts transient into permanent** (via `ApproximateReceiveCount`), rather than letting the message die in the DLQ. Phase 3's OQ-4 accepted DLQ residue for whole books; doing the same per chunk would be much worse — the book would sit at `chunksDone = 329/330` forever, phase 5 would never fire, and the *only* symptom would be a progress bar frozen at 99% with nothing in the API to explain it. `FAILED` + `ALL_ENGINES_FAILED` + a bumped `chunksFailed` gives phase 5 a decision to make and phase 6 something to render. `SYNTHESIZE_MAX_RECEIVE_COUNT` is duplicated between `infra/stacks/config.py` and `backend/src/config.py` with the standard lockstep comment; if it drifts upward on the queue side, the handler simply gives up early (safe), and if it drifts downward, messages DLQ (visible). Neither drift is silent.
- **The claim is always released on any non-terminal exit.** Same `except Exception:` shape as `ExtractBook`, and equally non-optional: without it a transient error leaves the chunk `SYNTHESIZING`, and although the permissive claim (§8.2 step 5) would eventually recover it, the intermediate state would misreport progress to phase 6.

**Both engines failed, final state.** Chunk `FAILED`/`ALL_ENGINES_FAILED`; book stays `EXTRACTED` with `chunksDone` including the failure and `chunksFailed >= 1`. The book is *not* marked `FAILED` in this phase — one bad chunk out of 330 shouldn't discard a whole book's audio, and the book-level verdict (`READY` with gaps vs `FAILED`) is phase 5's call, made with `chunksFailed` in hand. Phase 4's contract is exactly: *every chunk reaches a terminal state, and the counters tell you which.*

### 8.4 The exactly-once counter — the phase's correctness core

```python
try:
    self._chunk_repository.update_status(
        book_id, index, ChunkStatus.DONE,
        expected_statuses=NON_TERMINAL_CHUNK_STATUSES,   # i.e. "not already DONE"
        audio_key=audio_key, marks_key=marks_key,
        duration_ms=audio.duration_ms,
        synthesis_source=audio.source.value,
        clear_failure_reason=True,
    )
except ConflictError:
    # Another invocation reached DONE first. It already incremented.
    # Doing so again would push chunksDone past chunksTotal and phase 5's
    # `chunks_done == chunks_total` edge would never fire.
    return SynthesizeChunkResult("SKIPPED", reason="ALREADY_DONE")

counters = self._book_repository.increment_chunks_done(user_id, book_id)
```

The identical guarded pattern wraps the `→ FAILED` transition, with `increment_chunks_done(..., failed=True)`.

That single `except ConflictError: return` is what lets everything upstream be sloppy in the ways distributed systems are always sloppy: SQS delivers twice, the extract Lambda publishes the fan-out twice (§4.3), a crashed invocation gets its chunk re-claimed. All of it converges, because **the DynamoDB conditional update is the serialization point** and the counter increment is strictly downstream of it. The alternative — trying to make the claim exclusive — would need a lease timestamp, an expiry policy, and clock assumptions, for strictly worse guarantees.

`increment_chunks_done` raising `NotFoundError` (book deleted mid-synthesis) is caught and logged; the chunk is already `DONE`, the message is deleted, and there is nothing left to count.

---

## 9. Local dev & LocalStack

### 9.1 `local/setup.sh`

**No change.** Both queues and all three buckets already exist; the fan-out is producer-driven (no notification config to add). Worth stating explicitly so nobody goes looking.

### 9.2 Compose services

`docker-compose.yml`:
- `extract-worker` gains `SYNTHESIZE_QUEUE_URL=http://localstack:4566/000000000000/bookloud-local-synthesize` — it is now a **producer**.
- New `synthesize-worker`, mirroring `extract-worker` exactly:
  ```yaml
  synthesize-worker:
    build: { context: ./backend, target: dev }
    restart: unless-stopped
    environment:
      - ENVIRONMENT=local
      - LOG_LEVEL=DEBUG
      - AWS_ENDPOINT_URL=http://localstack:4566
      - AWS_DEFAULT_REGION=us-east-1
      - AWS_ACCESS_KEY_ID=local
      - AWS_SECRET_ACCESS_KEY=local
      - TABLE_NAME=bookloud-local
      - AUDIO_BUCKET=bookloud-local-audio
      - MARKS_BUCKET=bookloud-local-marks
      - SYNTHESIZE_QUEUE_URL=http://localstack:4566/000000000000/bookloud-local-synthesize
      - SYNTHESIZE_MAX_RECEIVE_COUNT=5
      # No GOOGLE_TTS_SECRET_NAME: local dev runs edge-tts only. Set it (plus
      # real AWS creds) if you need to exercise the fallback by hand.
    volumes: [./backend:/app]
    command: ["uv", "run", "python", "-m", "src.contexts.library.interface.local_synthesize_worker"]
    depends_on: { localstack: { condition: service_healthy } }
  ```
  This container **does** reach the public internet (default bridge networking) — that is the point; edge-tts is not mocked locally.
- `backend` gains `SYNTHESIZE_QUEUE_URL` for symmetry (unused by the API today, but it keeps the three env blocks readable side by side).

`Makefile`'s `up` target adds `synthesize-worker`. `README.md`'s quickstart comment updates accordingly.

`local_synthesize_worker.py` is a near-copy of `local_extract_worker.py`: `poll_once(sqs, queue_url) -> int` (testable, deletes a message only when its own `handle_records` succeeded, leaves failures for redelivery) plus `main()` (infinite loop, `# pragma: no cover`). It must synthesize the `attributes.ApproximateReceiveCount` the handler reads by passing `AttributeNames=["ApproximateReceiveCount"]` to `receive_message` and forwarding it into the pseudo-record — otherwise local dev never exercises the last-attempt path. Small, easy to forget, gets a test.

### 9.3 `local/smoke_test.py` — extend, don't defer

`check_upload_and_extraction` is **renamed** `check_upload_and_synthesis` and gains a synthesis phase after the existing extraction assertions. New `--synthesis-timeout` arg (default 180 s) and `--skip-synthesis` escape hatch.

New steps:
1. Poll `GET /books/{id}/chunks` every 3 s until **every** chunk's `status` is `DONE` or `FAILED`, or the timeout expires.
2. Assert at least one chunk is `DONE`.
3. For each `DONE` chunk assert `audioKey` and `marksKey` are non-null and `durationMs > 0`.
4. Poll `GET /books/{id}` and assert `chunksDone == chunksTotal` — **this is the real fan-in proof**, and it's the assertion that would have caught every ordering bug in §4.2 and §8.4.
5. Print `synthesisSource` per chunk so the CI log shows at a glance whether the fallback fired.

`durationMs > 0` is the load-bearing assertion: it can only be non-zero if real MP3 bytes came back from a real engine and the frame parser understood them. Asserting only on key strings would pass against a Lambda that wrote two empty objects.

The embedded `_SMOKE_PDF_B64` fixture yields ~450 chars of body text → **exactly one chunk** → one edge-tts call of roughly 20 words. The synthesis phase therefore adds ~10 s to the smoke test, not minutes.

Deferring this to phase 5 was considered and rejected: phase 4's entire deliverable is "chunks individually reach `DONE` with `audioKey`/`marksKey`", and phase 3 established `local/smoke_test.py` as the only proof that runs against real infra. A phase whose only gate is unit tests with mocked engines would ship the fan-out wiring, the IAM grants, the env vars, the queue's visibility timeout, and the ESM concurrency config completely unverified.

**Accepted risk, recorded honestly:** the `local-smoke` CI job and `deploy-pr` now depend on a live third-party endpoint. If Microsoft blocks GitHub Actions egress, CI goes red for reasons unrelated to the change under test. `--skip-synthesis` is the one-line escape hatch, and `deploy-prod.yml` — which passes no `--login-*` args — never runs this check at all. See OQ-D.

---

## 10. Test plan

All new tests under `backend/tests/contexts/library/`, reusing the existing `dynamodb_table`/`book_repo`/`chunk_repo`/`fixed_clock` fixtures. Coverage gate stays `--cov-fail-under=90`. **The suite makes zero network calls and needs zero credentials** (CI's `test-backend` job runs with no AWS services at all).

### 10.1 Test doubles — `tests/contexts/library/fakes.py` (new)

| double | shape |
|---|---|
| `FakeSynthesizer(name, result=…, error=…, calls=[])` | satisfies `SpeechSynthesizer`; returns a canned `SynthesizedAudio` or raises a programmable exception; records every call |
| `FakeCommunicate(chunks)` | mimics `edge_tts.Communicate`: constructor records `(text, voice, boundary, …)`, `stream()` is an async generator yielding a scripted list of `{"type": …}` dicts. Injected via `communicate_cls=` — **no monkeypatching of the `edge_tts` module's globals** |
| `FakeHttpPost(responses)` | `(url, body, timeout) -> bytes`; injected via `http_post=` into `GoogleTtsSynthesizer`; can raise `urllib.error.HTTPError` with a scripted status/body |
| `FakeSynthesisQueue` | records `enqueue_chunks` calls; programmable failure |
| `RecordingObjectStorage` | in-memory `put_bytes`/`get_bytes`; lets tests assert the exact key and decode the marks JSON |

The `SpeechSynthesizer` Protocol is precisely the phase-3 `PdfTextExtractor` pattern applied to a network dependency.

A tiny **real** MP3 byte fixture (a few hand-built silent MPEG-2 Layer III frames, ~200 bytes, constructed in code, not committed as a binary) backs the `mp3_duration_ms` tests and the `SynthesizedAudio` fakes — so `duration_ms` is exercised for real end to end rather than stubbed.

### 10.2 Test files

- **`test_marks.py`** (pure, exhaustive) — `iter_words` on unicode/punctuation/multiple spaces; `align_words` exact match, punctuation-attached tokens, an unmatched token's cursor-advance fallback, a wildly-out-of-range match rejected; `estimate_word_marks` monotonicity, exactness at anchors, single-anchor degenerate case, empty text; `build_marks_document` schema keys, sort-by-`t` assertion, `wordCount` correctness.
- **`test_mp3.py`** (pure) — MPEG-1 Layer III 44.1 kHz frame, **MPEG-2 Layer III 24 kHz** frame (edge-tts's actual output; the 576-vs-1152 regression guard), padding bit, ID3v2 header skipped, garbage bytes → `0`, truncated final frame.
- **`test_edge_tts_synthesizer.py`** — `boundary="WordBoundary"` is passed (the load-bearing kwarg gets its own assertion); tick→ms conversion; audio chunks concatenated in order; empty audio → `SynthesisUnavailable`; retry-then-succeed; retry-exhausted → `SynthesisUnavailable`; `EdgeTTSException` subclasses and a bare `Exception` both handled; `char_start`/`char_end` populated via alignment; `timing == "measured"`.
- **`test_google_tts_synthesizer.py`** — request body shape (`enableTimePointing`, `ssml`, voice, `languageCode` derived from the voice name); one `<mark>` per sentence, none consecutive; SSML escaping of `& < > "`; over-budget SSML degrades to plain text + 2 anchors; empty/truncated `timepoints` degrades; base64 audio decoded; `429` retried, `403` not retried; `timing == "estimated"`.
- **`test_fallback_synthesizer.py`** — primary succeeds → fallback never called; primary raises → fallback result returned + a `WARNING` logged; `UnsynthesizableText` from primary re-raised without touching the fallback; both fail → the fallback's exception propagates.
- **`test_stub_synthesizer.py`** — `synthesize()` always raises `SynthesisDisabled` with `reason == EXTERNAL_TTS_DISABLED`, never touches network/asyncio.
- **`test_dependencies.py`** (extend) — `get_speech_synthesizer()` returns `StubSynthesizer` for `environment in {"local", "pr-1"}` even when a Google secret name is set; returns a real `EdgeTtsSynthesizer` (optionally wrapped in `FallbackSynthesizer`) only for `environment == "prod"`.
- **`test_synthesis_use_case.py`** — happy path writes both objects at the exact expected keys then flips `DONE` (ordering asserted with a call-recording spy); `SKIPPED` branches (missing chunk, already `DONE`, user mismatch, claim conflict); permanent branches (`EMPTY_TEXT`, `TEXT_TOO_LONG`); **claim released to `PENDING` on transient failure**; **last-attempt (`attempt=5`) converts transient into `FAILED`/`ALL_ENGINES_FAILED`**; **double delivery increments `chunksDone` exactly once** (run `execute` twice, assert the counter is 1); `chunksFailed` incremented on the failure path.
- **`test_synthesize_handler.py`** — full SQS envelope end to end against moto + fakes; malformed JSON body ignored; unknown `v` ignored; `ApproximateReceiveCount` parsed off `attributes` and threaded into the command; batch of 2.
- **`test_sqs_synthesis_queue.py`** (moto `sqs`) — batches of 10, a 25-chunk book produces 3 calls, message body shape, **partial `Failed` retried once then raised**.
- **`test_s3_object_storage.py`** (moto `s3`) — `put_bytes` sets `ContentType`, round-trip, missing key → `NotFoundError`.
- **`test_s3_keys.py`** (extend) — audio/marks key round-trips, zero-padding width matches `CHUNK_INDEX_WIDTH`, malformed/traversal keys rejected.
- **`test_chunk_repository.py`** (extend) — `expected_statuses` claim succeeds/fails, `ConflictError` vs `NotFoundError` disambiguation, `duration_ms`/`synthesis_source`/`failure_reason` written, `clear_failure_reason` removes the attribute, both failure kwargs → `ValueError`, a status-only call still doesn't null out `audioKey`.
- **`test_book_repository.py`** (extend/update) — `increment_chunks_done` returns `ChunkCounters` (the two existing `== 1` / `== 2` assertions become `.chunks_done`), `failed=True` bumps both counters, `is_complete` true only at `chunks_done >= chunks_total > 0`, `update_status(chunks_done=0, chunks_failed=0)` resets.
- **`test_extraction_use_case.py`** (extend) — fan-out published **after** the `EXTRACTED` flip (spy ordering); publish covers `0..N-1`; **publish failure leaves the book `EXTRACTED`, not `UPLOADED`**; the `REQUEUED` branch re-publishes without re-extracting; the in-file `increment_chunks_done` fake updated.
- **`test_local_synthesize_worker.py`** — `poll_once` deletes on success, leaves on failure, requests and forwards `ApproximateReceiveCount`.
- Existing tests updated: `test_domain.py`, `test_mappers.py`, `test_schemas.py`, `test_controllers.py`, `test_use_cases.py`, `test_dependencies.py`, `test_extract_handler.py`, `test_config.py`.
- `infra/tests/test_synth.py` — per §6.7.

---

## 11. Concrete file list

**`backend/src/contexts/library/`**
- new: `domain/synthesis.py`, `domain/marks.py`, `application/synthesis.py`, `infrastructure/s3_object_storage.py`, `infrastructure/sqs_synthesis_queue.py`, `infrastructure/edge_tts_synthesizer.py`, `infrastructure/google_tts_synthesizer.py`, `infrastructure/fallback_synthesizer.py`, `infrastructure/stub_synthesizer.py`, `infrastructure/secrets.py`, `infrastructure/mp3.py`, `interface/synthesize_handler.py`, `interface/local_synthesize_worker.py`
- changed: `domain/value_objects.py`, `domain/chunk.py`, `domain/book.py`, `domain/repository.py`, `domain/storage.py`, `application/extraction.py`, `infrastructure/s3_keys.py`, `infrastructure/chunk_mapper.py`, `infrastructure/book_mapper.py`, `infrastructure/dynamodb_chunk_repository.py`, `infrastructure/dynamodb_book_repository.py`, `interface/dependencies.py`, `interface/schemas.py`, `interface/extract_handler.py`, `interface/local_extract_worker.py`

**`backend/`**: `src/config.py`, `pyproject.toml` + `uv.lock` (changed)

**`backend/tests/contexts/library/`**: `fakes.py`, `test_marks.py`, `test_mp3.py`, `test_edge_tts_synthesizer.py`, `test_google_tts_synthesizer.py`, `test_fallback_synthesizer.py`, `test_synthesis_use_case.py`, `test_synthesize_handler.py`, `test_sqs_synthesis_queue.py`, `test_s3_object_storage.py`, `test_local_synthesize_worker.py` (new); `test_s3_keys.py`, `test_chunk_repository.py`, `test_book_repository.py`, `test_extraction_use_case.py`, `test_extract_handler.py`, `test_domain.py`, `test_mappers.py`, `test_schemas.py`, `test_controllers.py`, `test_use_cases.py`, `test_dependencies.py`, `../../test_config.py` (changed)

**`infra/`**: `stacks/pipeline_stack.py`, `stacks/config.py`, `app.py`, `tests/test_synth.py` (changed). **`stacks/api_stack.py` and `stacks/storage_stack.py` are untouched.**

**`local/`**: `smoke_test.py` (changed). **`setup.sh` untouched.**

**Root**: `docker-compose.yml`, `Makefile`, `README.md`, `IMPLEMENTATION_PLAN.md` (changed)

---

## 12. Implementation sequence

1. **Pure domain first, zero AWS, zero network:** `domain/marks.py` + `test_marks.py`, `infrastructure/mp3.py` + `test_mp3.py`. **Gate:** the MPEG-2-vs-MPEG-1 `samples_per_frame` test must pass before anything downstream trusts a duration.
2. `value_objects.py` (`SYNTHESIZING`, the three new enums, `NON_TERMINAL_CHUNK_STATUSES`), `chunk.py`, `book.py`, `domain/synthesis.py`, `domain/storage.py`'s `ObjectStorage`, `s3_keys.py` + tests. Still no AWS.
3. Mappers + **both repository rewrites** + tests. **Gate:** the `ConflictError`-vs-`NotFoundError` disambiguation test and the `ChunkCounters` tests must be green before the use cases are written — everything in §8.4 depends on them.
4. Add `edge-tts` to `pyproject.toml`, regenerate `uv.lock`, **confirm the resolved `aiohttp` is a manylinux wheel**, and build the Lambda image once locally to confirm the layer still fits.
5. `edge_tts_synthesizer.py` + `google_tts_synthesizer.py` + `fallback_synthesizer.py` + `secrets.py` + tests, all with injected fakes. **Gate:** a single manual `python -c` against the real edge-tts endpoint to sanity-check the `boundary="WordBoundary"` kwarg and the tick units against the current pinned version — done once, by hand, never in the suite.
6. `s3_object_storage.py`, `sqs_synthesis_queue.py` + moto tests.
7. `SynthesizeChunk` + `synthesize_handler.py` + tests, including the double-delivery and last-attempt cases.
8. `ExtractBook` fan-out changes + `extract_handler.py`/`local_extract_worker.py` composition roots + tests. Full backend suite green at ≥ 90%.
9. `pipeline_stack.py` + `config.py` + `app.py` + infra tests. `make synth` locally.
10. Compose `synthesize-worker` + Makefile. `make up`, upload a real PDF by hand, watch a chunk go `PENDING → SYNTHESIZING → DONE` and confirm the MP3 in LocalStack actually plays.
11. `local/smoke_test.py` + `make smoke` — the phase's real gate.
12. README + `IMPLEMENTATION_PLAN.md` checklist. Push; `deploy-pr` runs the whole thing against real AWS with real Lambdas.

---

## 13. Open Questions

### Decided (recorded for the log)

**OQ-1 — Fan-out mechanism. DECIDED: extract Lambda publishes, via a `SynthesisQueue` Protocol, after the `EXTRACTED` flip.** DynamoDB Streams rejected for this phase on LocalStack-testability grounds (§4.1); revisit in phase 5.

**OQ-2 — Google timing strategy. DECIDED: sentence-level SSML `<mark>` anchors + proportional intra-sentence interpolation**, with `"timing": "estimated"` recorded in the marks file. Per-word marks are infeasible (5000-byte input cap, "no consecutive marks" guidance).

**OQ-3 — Marks JSON shape. DECIDED: array of short-keyed objects `{t,d,s,e,w}`**, sorted by `t`, with `s`/`e` relative to the chunk text. Parallel-array encoding rejected on debuggability grounds.

**OQ-4 — Chunk `FAILED` counts toward `chunksDone`. DECIDED: yes**, plus a separate `chunksFailed`. Otherwise a single unsynthesizable chunk wedges the book below `chunksTotal` forever and phase 5 never fires.

**OQ-5 (dup label, book status) — Book status during synthesis. DECIDED: unchanged (`EXTRACTED`).** No new `BookStatus`; phase 5 owns `READY`.

**OQ-6 — Reserved concurrency. DECIDED: ESM `max_concurrency=5`, function `reserved_concurrent_executions=10`**, both behind named `Config` constants for one-line tuning.

**OQ-7 — DLQ residue. DECIDED: eliminated for chunks** via the last-attempt `ApproximateReceiveCount` rule (§8.3), rather than inherited from phase 3's OQ-4. The DLQ remains as a backstop for genuinely malformed messages.

### Genuinely open — need a human decision before or during implementation

**OQ-A — DECIDED: ship dormant.** Code path built and tested now; GCP project/API key/secret provisioned whenever convenient, out of band. Fallback stays disabled (and, per §0, entirely unreachable outside prod regardless) until that secret exists.

**OQ-B — DECIDED: `en-US-AriaNeural` (edge-tts) / `en-US-Neural2-C` (Google).** Per-book voice selection (a user setting) remains deferred to phase 6.

**OQ-C — Google REST field names.** The v1beta1 JSON REST shape (`enableTimePointing: ["SSML_MARK"]`, `timepoints[].markName`/`timeSeconds`) is taken from the published proto/reference, not from a live call against the deployed key. **Action needed during implementation:** one manual `curl` against the real endpoint to confirm the exact JSON casing before the adapter is considered done. If it differs, only `google_tts_synthesizer.py` changes. (Note also the reported v1beta1 bug where timepoints truncate after the first period — the §7.4 "empty/truncated → degrade to proportional" branch is the defence, but if it reproduces, sentence anchoring buys nothing over whole-chunk proportional estimation and the code should be simplified accordingly.)

**OQ-D — RESOLVED by §0's addendum, not by the original recommendation.** The original text recommended accepting live-endpoint flakiness as a hard CI gate. After user feedback, the actual decision is stronger: **no CI/PR environment ever calls a real TTS engine at all** — `ENVIRONMENT != "prod"` always gets `StubSynthesizer`. The smoke test still hard-gates CI, but on a fully deterministic stubbed failure path, so the flakiness trade-off this OQ was asking about no longer exists.

**OQ-E — Cost/quota alarm.** If edge-tts goes permanently dark, Google silently becomes the primary engine and a single 300-page book consumes ~60% of the monthly Neural2 free tier. Phase 4 logs a `WARNING` on every fallback and records `synthesisSource` on each chunk, which is enough to notice manually. Recommendation: **defer to phase 8** (alongside the DLQ alarm phase 3 already deferred there), and note it in `IMPLEMENTATION_PLAN.md` so it isn't lost.

---

### Critical Files for Implementation
- infra/stacks/pipeline_stack.py
- backend/src/contexts/library/application/extraction.py
- backend/src/contexts/library/infrastructure/dynamodb_chunk_repository.py
- backend/src/contexts/library/infrastructure/dynamodb_book_repository.py
- backend/src/contexts/library/domain/value_objects.py

Sources consulted for the external-API details: edge-tts `communicate.py`/`exceptions.py` (rany2/edge-tts on GitHub), Google Cloud TTS v1beta1 reference and SSML guide (docs.cloud.google.com), reported v1beta1 timepoint truncation (Google developer forums).
