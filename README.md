# Bookloud

A personal webapp that reads uploaded PDFs aloud with word-level highlighting
synced to audio, plus a chat sidebar for asking questions about the book,
scoped to the section currently being read.

Built phase by phase per `IMPLEMENTATION_PLAN.md`. This repo is currently at
**Phase 5**: Stitching & status polling. Every non-public backend route
requires a valid Cognito JWT; the frontend has working login/logout/
session-refresh via a Next.js backend-for-frontend; local dev emulates
Cognito with `jagregory/cognito-local`. The backend's `Library` bounded
context owns `Book`/`Chunk` CRUD against the single DynamoDB table plus the
full pipeline: `POST /books` creates a book and returns a presigned S3
upload; the PDF landing in the bucket fires an S3 event -> SQS -> the extract
Lambda (PyMuPDF extraction, running header/footer filtering, chunking,
`CHUNK#n` rows, book -> `EXTRACTED`); a per-chunk fan-out drives the
synthesize Lambda (TTS -> MP3 + word timings, chunk -> `DONE`, atomic
`chunksDone` increment); and the increment that completes the book publishes
one message to a third queue where the **stitch Lambda** concatenates every
`DONE` chunk's MP3 into a single `book.mp3`, writes a book manifest
(`book.json`) rebasing each chunk into book-global milliseconds, and flips
the book to `READY` or `PARTIAL`. `GET /books/{id}/status` is the one cheap
poll a client needs.

## Library context (backend)

`backend/src/contexts/library/` is a DDD-flavoured bounded context following
the repo's `{domain,application,infrastructure,interface}` layout:

```
library/
├── domain/           Book (aggregate root, carries sourceKey/
│                    failureReason/chunksFailed plus audioKey/manifestKey/
│                    audioDurationMs), Chunk (its own aggregate -- see
│                    below, carries pageStart/pageEnd plus
│                    durationMs/synthesisSource/failureReason),
│                    BookStatus/ChunkStatus/ExtractionFailure/
│                    SynthesisFailure/StitchFailure/SynthesisSource/
│                    MarksTiming, BookRepository/ChunkRepository/
│                    PdfStorage/ObjectStorage/MultipartWriter/
│                    PdfTextExtractor/SpeechSynthesizer/SynthesisQueue/
│                    StitchQueue (Protocols, no ABCs), plus five pure
│                    (no pymupdf/edge_tts/boto3) modules: layout.py
│                    (header/footer/footnote policy), chunking.py (text ->
│                    chunk boundaries), marks.py (word timing alignment
│                    + interpolation) and stitching.py (book-global offset
│                    math + the book manifest)
├── application/      RequestBookUpload (create + presign, replaces
│                    phase 2's CreateBook), ReissueBookUpload, GetBook,
│                    ListBooks, DeleteBook, ListBookChunks, ExtractBook
│                    (the extract Lambda's use case), SynthesizeChunk (the
│                    synthesize Lambda's use case), StitchBook (the stitch
│                    Lambda's use case)
├── infrastructure/   keys.py/s3_keys.py (PK/SK + S3 key helpers),
│                    book_mapper.py/chunk_mapper.py (dict <-> entity),
│                    the two DynamoDB repository adapters,
│                    s3_pdf_storage.py (presigned POST + get_bytes),
│                    s3_object_storage.py (incl. S3MultipartWriter),
│                    sqs_synthesis_queue.py, sqs_stitch_queue.py,
│                    pymupdf_extractor.py (the only module importing
│                    pymupdf), edge_tts_synthesizer.py (the only module
│                    importing edge_tts), google_tts_synthesizer.py (REST
│                    via urllib, no GCP SDK), fallback_synthesizer.py,
│                    stub_synthesizer.py, silent_synthesizer.py, secrets.py,
│                    mp3.py (pure MP3 frame-header duration parser +
│                    container-header stripping)
└── interface/        FastAPI Depends providers, camelCase response
                     schemas, POST /books + POST /books/{id}/upload-url +
                     GET /books + GET /books/{id} + GET /books/{id}/chunks
                     + GET /books/{id}/status controllers,
                     extract_handler.py + synthesize_handler.py +
                     stitch_handler.py (the three SQS Lambda handlers), and
                     local_{extract,synthesize,stitch}_worker.py (their
                     LocalStack-dev poll-loop equivalents)
```

**Upload contract** (what the phase 6 reader UI will call): `POST /books
{"title": "..."}` returns `{book, upload: {url, fields, key, expiresIn,
maxBytes}}`. Build a `FormData`, append every entry of `upload.fields` first,
append the file **last** under the field name `file` (S3 ignores anything
after the file field), `POST` to `upload.url`, expect `204`. Then poll
`GET /books/{id}/status` until `terminal` is `true`, and
`GET /books/{id}/chunks` for the extracted text. `POST /books/{id}/upload-url`
re-issues a presigned upload for retry (only while `status` is
`UPLOADED`/`FAILED`; `409` otherwise).

**Status polling.** `GET /books/{id}/status` is the one endpoint a client
should poll. It costs the same single `GetItem` as `GET /books/{id}` and adds
two fields the client would otherwise have to derive:

```json
{"id": "...", "status": "PARTIAL", "terminal": true,
 "progress": {"chunksTotal": 12, "chunksDone": 12, "chunksFailed": 12, "percent": 100},
 "failureReason": "NO_AUDIO",
 "audio": {"audioKey": null, "manifestKey": "marks/<u>/<b>/book.json", "durationMs": 0},
 "updatedAt": "..."}
```

**Stop on `terminal`, never on `status == "READY"`.** `terminal` is computed
server-side over `{READY, PARTIAL, FAILED}`, so the closed set lives in one
place instead of being copied into every client. A poll loop that waits for
the literal string `READY` hangs forever on a `PARTIAL` book — which, because
real TTS is prod-only (below), is *every* book in local dev and in every
ephemeral PR environment.

**Book status lifecycle.**

```
UPLOADED --> EXTRACTING --> EXTRACTED --> STITCHING --> READY    (every chunk synthesized)
    |            |                                  \-> PARTIAL  (some/none synthesized)
    \------------\-------------------------------------> FAILED  (extraction only)
```

`FAILED` means *extraction* produced nothing usable, and nothing else — the
stitcher never sets it. That is deliberate: `FAILED` is re-claimable by the
extract Lambda, so using it for "the audio is missing" would let a stray
redelivered S3 event wipe and re-extract a book whose text was perfectly
fine. A book with good text but no audio is `PARTIAL` + `failureReason`
`NO_AUDIO` (nothing synthesized) or `STITCH_FAILED` (concatenation gave up),
and stays fully readable. `PARTIAL` is terminal.

**Stitching.** When the atomic increment that observes
`chunksDone == chunksTotal` lands, that single invocation publishes one
message to `stitch_queue`; the stitch Lambda claims the book
(`EXTRACTED|STITCHING -> STITCHING`, conditionally), concatenates the raw
MPEG frames of every `DONE` chunk into `audio/<u>/<b>/book.mp3`, writes
`marks/<u>/<b>/book.json`, and flips to `READY`/`PARTIAL` conditionally on
still being `STITCHING` — that last conditional is the exactly-once gate, so
duplicate stitch messages are harmless. Concatenation is **raw frame
appending, no re-encoding** (no ffmpeg/LAME in the image); each segment's
leading ID3v2 block and Xing/Info/VBRI metadata frame are stripped first so
decoders see one clean frame stream. Memory is bounded by an S3 multipart
writer, so a 240 MB book costs ~6 MB of resident buffer, not 480 MB.

**The manifest is the timeline, not the audio file.** `book.json` rebases
each chunk into book-global milliseconds:

```json
{"version": 1, "bookId": "...", "status": "READY",
 "audioKey": "audio/<u>/<b>/book.mp3", "durationMs": 1418240,
 "chunksTotal": 330, "chunksDone": 330, "chunksFailed": 0, "sampleRateHz": 24000,
 "segments": [{"i": 0, "t": 0, "d": 118240, "s": 0, "e": 1800,
               "audioKey": "audio/<u>/<b>/000000.mp3",
               "marksKey": "marks/<u>/<b>/000000.json",
               "b0": 0, "b1": 709440}],
 "missing": [7, 42]}
```

- `t`/`d` are book-global milliseconds. `segments[k].t == segments[k-1].t +
  segments[k-1].d` **exactly**, and `sum(d) == durationMs` **exactly** —
  durations accumulate as float seconds and each boundary is rounded once, so
  there is no drift (summing per-segment rounded integers would lag ~165 ms
  by the end of a 330-chunk book). Phase 6 rebases a word with one addition:
  `t_global = segment.t + word.t`.
- `s`/`e` are **book-global character** offsets, straight from
  `Chunk.charStart`/`charEnd`. Note the deliberate asymmetry with the
  per-chunk marks files, whose `s`/`e` are **chunk-relative**: the chunk file
  indexes into one chunk's text, the manifest indexes into the book.
- `b0`/`b1` are the segment's half-open byte range inside `book.mp3`, for
  exact Range-request seeking (no Xing header is written, so a browser's own
  byte estimate is only approximate on a mixed-engine book).
- Every segment always carries its own `audioKey`/`marksKey`, so a book with
  `audioKey: null` at the document level — one whose concatenation failed, or
  which has no stitched file — is still fully playable and highlightable
  chunk by chunk. Graceful degradation is a data-model property here, not a
  special case in the UI.
- `missing` lists the chunk indexes with no audio, ascending. Failed chunks
  are simply absent from `segments`; no silence is fabricated.

Turning `audioKey` into something an `<audio>` element can play (presigned
GET vs CloudFront origin, Range support, cache headers) is deliberately a
phase-6 decision — this phase exposes S3 keys, not URLs.

**Synthesis pipeline.** When extraction finishes, the extract Lambda publishes
one SQS message per chunk (fan-out), and the synthesize Lambda processes each
independently: it atomically claims the chunk (`-> SYNTHESIZING`), calls TTS,
writes an MP3 to `audio_bucket` and a word-timestamp JSON to `marks_bucket`,
then flips the chunk to `DONE` and atomically increments the book's
`chunksDone`. Fan-in is that counter: the increment is atomic and each chunk
increments at most once, so exactly one invocation ever observes
`chunksDone == chunksTotal` — and that single observation publishes the
stitch message. The counter increment is gated on the *conditional* terminal
update succeeding, which is what makes duplicate SQS deliveries, duplicate
fan-out publishes and crashed-mid-flight invocations all safe with no
distributed lock. A chunk that permanently fails still counts toward
`chunksDone` (and bumps `chunksFailed`), so one bad chunk can never wedge a
book below `chunksTotal` forever. If the invocation that observed completion
dies before its publish lands, the redelivery of that chunk's message
re-publishes instead of re-synthesizing.

S3 layout: `audio/<userId>/<bookId>/<index:06d>.mp3` and
`marks/<userId>/<bookId>/<index:06d>.json` per chunk, plus
`audio/<userId>/<bookId>/book.mp3` and `marks/<userId>/<bookId>/book.json`
for the stitched book. The marks JSON is what phase 6's highlight sync
binary-searches on every `timeupdate`:

```json
{"version": 1, "bookId": "...", "chunkIndex": 7, "source": "edge-tts",
 "timing": "measured", "durationMs": 118240, "wordCount": 302,
 "words": [{"t": 0, "d": 320, "s": 0, "e": 7, "w": "Chapter"}]}
```

`t`/`d` are milliseconds; `s`/`e` are char offsets **relative to the chunk
text**; `words` is sorted by `t`. `timing` is `measured` when the engine gave
real per-word events (edge-tts) or `estimated` when they were interpolated
between sentence anchors (Google, which only exposes SSML `<mark>` timepoints).

**Real TTS runs in prod only.** `get_speech_synthesizer()` checks
`ENVIRONMENT != "prod"` first and unconditionally, so local dev and every
ephemeral PR stack never reach a real engine. By default they get a
`StubSynthesizer`, which raises a *permanent* `SynthesisDisabled`, so those
chunks go straight to `FAILED`/`EXTERNAL_TTS_DISABLED` with no retries and no
network call ever, and the book stitches to `PARTIAL`/`NO_AUDIO`. This keeps
developer laptops and CI off Microsoft's undocumented free endpoint and makes
the smoke test fully deterministic. Consequently no automated test exercises
the real engines; that is deliberate.

**`SYNTHESIS_STUB_MODE=silent`** (set only on docker-compose's
`synthesize-worker`, never on a deployed stack) swaps that raise-only stub for
a `SilentSynthesizer`: ~60 lines of pure Python that emit valid silent MPEG-2
Layer III frames in edge-tts's real output format, with word marks from the
existing estimator. Still zero network calls and zero new dependencies — it is
a local *generator*, not an external service, so it does not weaken the rule
above. What it buys is the only automated run anywhere that exercises the byte
path end to end: `make up && make smoke` really concatenates frames, computes
real byte offsets, performs a real S3 multipart upload against LocalStack and
lands a `READY` book with a playable `book.mp3`. (`make smoke` passes
`--expect-synthesis silent`; CI's per-PR deploy passes nothing and keeps the
deterministic `PARTIAL`/`NO_AUDIO` assertions.)

**Enabling the Google Cloud TTS fallback** (optional, prod-only, currently
dormant): create a GCP project, enable the Cloud Text-to-Speech API, create an
API key restricted to that single API, then store it once:

```bash
aws secretsmanager create-secret \
  --name bookloud/google-tts-api-key --secret-string '<key>'
```

With the secret absent (the default everywhere today), the synthesize Lambda
runs edge-tts alone and logs a single INFO at startup. See `PLANS/phase-4.md`
for the full decisions log.

**Single-table key patterns** (table: `bookloud-<env>`, PK/SK both strings):

| item | PK | SK |
|---|---|---|
| book | `USER#<sub>` | `BOOK#<bookId>` |
| chunk | `BOOK#<bookId>` | `CHUNK#<index>` (zero-padded to 6 digits, e.g. `CHUNK#000007`) |

Isolation is enforced **by the key**, not a post-read check: every
`BookRepository` method takes `(user_id, book_id)` and addresses
`USER#<sub>`'s partition directly, so a wrong user id simply finds nothing --
a missing book and someone else's book are both a `404`, never a `403`.

**Chunks are a separate aggregate from `Book`, not embedded in the book
item** -- unlike this repo's DDD reference (jgautocar), which embeds
tasks/photos in the parent item with no independent repository. Three
reasons: the schema already puts chunks in their own items; a whole book's
extracted text would blow past DynamoDB's 400 KB item limit; and, decisively,
phase 4's chunk synthesis runs one Lambda per chunk in parallel with fan-in
via an atomic `chunksDone` counter on the book record -- read-modify-write of
one embedded aggregate from N concurrent Lambdas would lose updates.
Consequently `ChunkRepository` takes a bare `book_id: str` (chunk items carry
no user in their key) and enforces no access control itself; every use case
that touches chunks authorizes the book first via `BookRepository.get(user_id,
book_id)` before ever calling into `ChunkRepository`.

See `PLANS/phase-2.md` for the full design rationale and decisions log.

## Auth model

Username + password sign-in (not email-based). **Accounts are admin-provisioned
only -- there is no public signup.** The Cognito user pool has
`self_sign_up_enabled=False`, so Cognito itself rejects the `SignUp` API
regardless of what the frontend does; see "Provisioning a new reader account"
below. The browser never talks to Cognito directly:
`POST /api/auth/{login,new-password,refresh,logout}` (Next.js route handlers)
do, over Cognito's plain JSON API. The refresh token (30-day validity) lives
in an `httpOnly; SameSite=Lax` cookie (`bookloud_refresh`) the browser can't
read; the id token (~1h) is held in a JS module variable and attached as
`Authorization: Bearer` on calls to the backend API, which never validates it
itself -- API Gateway's Cognito JWT authorizer does, forwarding the verified
claims to the Lambda. See `PLANS/phase-1.md` for the full design (and why this
topology, not Amplify Auth or a FastAPI-side `/auth/*`); §11 covers the
admin-only revision.

`app/signup/page.tsx` and its `/api/auth/{signup,confirm}` routes still exist
in the repo (unreachable from the UI, no "Sign up" link) in case self-signup
is ever revisited -- hitting `/signup` directly and submitting now surfaces
Cognito's rejection as a 401.

### First login: forced password change

An admin-provisioned user is created with a *temporary* password. Their
first `InitiateAuth` returns Cognito's `NEW_PASSWORD_REQUIRED` challenge
instead of tokens; `/login` detects this and swaps in a "choose a new
password" step. Once that new password is set, the account behaves like any
other -- normal login, no further forced changes. There is no self-service
"change password while logged in" page (out of scope for this phase).

### Provisioning a new reader account

There is no signup form. To add a reader, create their Cognito user with a
*temporary* (non-permanent) password -- either the AWS Console or the CLI:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <USER_POOL_ID> \
  --username <username> \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id <USER_POOL_ID> \
  --username <username> \
  --password '<temporary-password>'
  # note: no --permanent -- this is what triggers NEW_PASSWORD_REQUIRED
```

Or via the Console: Cognito -> User pools -> the pool -> Users -> "Create
user" -> set a temporary password, uncheck "send an invitation" if you'd
rather share the password out-of-band. Either way, tell the new user their
temporary password out of band; their first login at `/login` will prompt
them to choose their own password before they can use the app.

## Quickstart (local dev)

Requires Docker, Python 3.12+, Node 24, and [uv](https://docs.astral.sh/uv/).

```bash
make up      # LocalStack + cognito-local + backend (uvicorn, hot reload) +
             # extract-worker, synthesize-worker and stitch-worker (poll
             # loops over their respective queues) on :8000
make ui      # + Next.js frontend on :3000 (optional)
make smoke   # smoke test against the running stack: upload -> extraction ->
             # synthesis fan-out/fan-in -> stitch -> poll
             # GET /books/{id}/status to READY. Real TTS is never called;
             # locally the offline SilentSynthesizer produces real bytes so
             # the concatenation and manifest are genuinely exercised.
make down    # tear everything down
```

`make up` seeds a local Cognito pool (via `jagregory/cognito-local`) with two
users:
- **`dev` / `devpassword`** -- permanent password, no forced change, for
  routine local dev / `make smoke` convenience. Log in with it at
  `http://localhost:3000/login` after `make ui`, or run
  `curl -H "Authorization: Bearer $(make -s token)" localhost:8000/me` to hit
  the backend directly.
- **`newuser` / `TempPass123!`** -- a *temporary* password, to exercise the
  admin-provisioned forced-first-login flow locally: logging in with it
  triggers the "choose a new password" step instead of a normal login.

Run the full test suite (backend pytest, frontend vitest + tsc, infra
`aws_cdk.assertions` synth tests):

```bash
make test
```

`make synth` runs `cdk synth -c environment=dev` for a local sanity check of
every stack. `make e2e` is the CI-friendly one-shot: `up` -> `ui` -> smoke
both -> `down`.

## Repo layout

```
bookloud/
├── backend/    FastAPI, DDD-flavoured (contexts/<ctx>/{domain,application,
│               infrastructure,interface}, shared_kernel/), packaged as a
│               Lambda container image. GET /health, GET /me (auth),
│               the full Library upload/extraction/synthesis surface
│               (contexts/library/, phases 2-4) today; stitching, the
│               reader UI and chat land in phases 5-7. src/auth/ is
│               cross-cutting, not a context. The same image also backs the
│               extract and synthesize Lambdas
│               (infra/stacks/pipeline_stack.py), differing only by their
│               container `cmd`.
├── frontend/   Next.js 15 (App Router) + Tailwind, deployed via OpenNext to
│               Lambda + CloudFront. Landing page, login (incl. forced
│               first-login password change), and the auth BFF route
│               handlers today; the reader UI and chat sidebar land in
│               phases 6-7. app/signup/ still exists but is unreachable from
│               the UI (admin-only provisioning, see Auth model above).
├── infra/      Python CDK app: AuthStack (Cognito, self_sign_up_enabled=
│               False, PreSignUp trigger kept but unreachable), StorageStack
│               (DynamoDB + S3), PipelineStack (extract/synthesize/stitch
│               SQS queues + DLQs, the three pipeline Lambdas, and the
│               S3 -> SQS notification), ApiStack (HTTP API + Lambda + Cognito JWT
│               authorizer), FrontendStack (CloudFront + Lambda + S3).
│               Dependency order: Storage -> Pipeline -> Api -> Frontend
│               (Auth feeds into Api and Frontend too).
├── local/      docker-compose helper scripts: setup.sh seeds LocalStack
│               (table/buckets/queues + the S3 -> SQS notification) +
│               bootstraps cognito-local, cognito_bootstrap.py provisions
│               the local Cognito pool/client/dev user + newuser (temp
│               password), smoke_test.py is the stdlib-only smoke test used
│               locally, in CI, and against every deployed environment. The
│               `extract-worker`/`synthesize-worker`/`stitch-worker` compose
│               services run the three pipeline Lambdas' handler code in poll
│               loops (LocalStack community can't run our container-image
│               Lambda).
├── docker-compose.yml, Makefile
├── .github/workflows/   ci.yml (reusable), deploy-pr.yml, destroy-pr.yml,
│                        deploy-prod.yml
├── IMPLEMENTATION_PLAN.md   the overall phase-by-phase plan
└── PLANS/phase-N.md         each phase's approved, detailed implementation plan
```

## Deploy model

One AWS account/region (`us-east-1`) hosts prod and every ephemeral PR
environment, distinguished purely by resource name. A single CDK context
value, `environment`, drives every name:

| `environment`   | when              | stack names (example)             |
|------------------|-------------------|------------------------------------|
| `dev`            | local default      | `BookloudApi-dev`                  |
| `pr-<N>`         | per-PR ephemeral   | `BookloudApi-pr-42`                |
| `prod`           | push to `main`     | `BookloudApi-prod`                 |

- **On every PR**: `ci.yml` runs backend/frontend/infra unit tests plus a
  local compose smoke test; `deploy-pr.yml` then deploys all five stacks
  suffixed `pr-<N>` to the same AWS account, builds the frontend with
  OpenNext against the just-deployed API, and runs `local/smoke_test.py`
  against the live URLs. The stack is left standing (that's the point of an
  ephemeral environment) and a PR comment links to it.
- **On PR close/merge**: `destroy-pr.yml` deletes the five `pr-<N>` stacks in
  reverse dependency order (`Frontend -> Api -> Pipeline -> Auth -> Storage`).
- **On push to `main`**: `deploy-prod.yml` runs the same test-then-deploy
  sequence against the `prod` stacks.

Removal policy: `RETAIN` for `environment == "prod"`, `DESTROY` (+
`auto_delete_objects`) otherwise -- PR environments vanish completely on
teardown; prod data survives a stack deletion.

See `PLANS/phase-0.md` for the full rationale, including the container-image
Lambda packaging (needed from phase 3 onward for PyMuPDF/edge-tts), the
OpenNext + CloudFront wiring, and the open questions still pending human
sign-off (AWS account ID, GitHub secrets).
