# Bookloud

A personal Android app that reads uploaded PDFs aloud with word-level
highlighting synced to audio, plus a chat sidebar for asking questions about
the book, scoped to the section currently being read.

The client is a native Expo/React Native Android app (`mobile/`) -- chosen
over a browser tab for background audio, lock-screen controls, and
one-handed reading-while-listening. It talks to a FastAPI backend on AWS
(API Gateway + Lambda, DynamoDB, S3, SQS, Cognito), all defined as CDK infra.

Every non-public backend route requires a valid Cognito JWT; the client
authenticates directly against Cognito's plain JSON API (no backend-for-
frontend -- the app client has no secret, so there's nothing a server layer
would protect); local dev emulates Cognito with `jagregory/cognito-local`.

See "Architecture" below for the full request/processing flow and the SQS
pipeline design, "Mobile app" for the client, and "Quickstart" to run it
locally.

## Architecture

```
Mobile app (Expo / React Native, Android) -- mobile/
        │  HTTPS, Authorization: Bearer <Cognito id token>
        ▼
API Gateway (HTTP API)
        │  Cognito JWT authorizer on every non-public route
        ├────────────────────────────────┐
        ▼                                ▼
  ApiFunction (FastAPI, Lambda)     ChatFunction (Lambda Function URL,
  books / chunks / status /         token-streaming; verifies the JWT
  upload / audio routes             itself -- no API Gateway in front)
        │        │                          │
        ▼        ▼                          ▼
  DynamoDB   S3: pdf / audio /       same DynamoDB + S3, plus an LLM
  (single     marks buckets          call scoped to the chunk(s) being
   table)                            read

PDF upload / processing pipeline (SQS fan-out -> fan-in):

PDF lands in the pdf bucket (via a presigned POST the app got from
POST /books) --> S3 event notification --> extract_queue
        │
        ▼  ExtractFunction polls extract_queue (SQS event source mapping)
  PyMuPDF text extraction, chunking, CHUNK# rows written to DynamoDB,
  book -> EXTRACTED, one SQS message published per chunk (fan-out)
        │
        ▼  SynthesizeFunction polls synthesize_queue, N chunks in parallel
  edge-tts (Google Cloud TTS fallback) -> MP3 + word-timing JSON to S3,
  chunk -> DONE, atomic chunksDone++ on the book (fan-in)
        │
        ▼  the one invocation that observes chunksDone == chunksTotal
           publishes a single message to stitch_queue
  StitchFunction polls stitch_queue, concatenates every DONE chunk's MP3
  into book.mp3, writes the book manifest (book.json), book -> READY/PARTIAL

Every queue above (extract/synthesize/stitch) has its own DLQ. The extract
and synthesize DLQs are drained by a DlqSweeperFunction that marks stranded
books FAILED/PARTIAL instead of leaving them stuck forever; CloudWatch
alarms watch DLQ depth and a TTS-fallback log metric.
```

**Why SQS, and how it's actually consumed.** The three pipeline stages
(extract, synthesize, stitch) are decoupled by their own SQS queue rather
than calling each other directly, for two reasons: fan-out (one extracted
book becomes N independent per-chunk synthesis jobs, each its own Lambda
invocation, so a 300-page book synthesizes in parallel instead of
serially) and durability (a crashed or throttled invocation just leaves its
message unacknowledged -- SQS redelivers it, no work is lost, no
distributed lock is needed). Fan-in back onto one book uses an atomic
`chunksDone` counter on the DynamoDB book item, not a second queue per
book: exactly one chunk's increment ever observes the counter hit the
total, and that invocation is the one that publishes the single message
that advances the pipeline to the next stage.

Each queue is attached to its Lambda via an **SQS event source mapping**
(ESM) -- Lambda's own managed poller calls `ReceiveMessage` on the queue and
invokes the function when something arrives. This is *not* a push/webhook
mechanism from SQS's side; the polling happens on AWS's side, on your
behalf, continuously, whether or not the queue has any messages. Every
queue in this stack sets `ReceiveMessageWaitTimeSeconds` to SQS's max of 20
seconds (long polling): an idle poller's `ReceiveMessage` call blocks for up
to 20s waiting for a message instead of returning immediately and being
called again in a tight loop. That distinction is what an idle-infrastructure
SQS bill is made of -- five queues short-polling 24/7 can burn through the
AWS free tier's 1M requests/month on empty queues alone, with book uploads
themselves accounting for only a few hundred requests each. Long polling
cuts that idle request volume by roughly two orders of magnitude with no
effect on delivery latency for a real message -- a Lambda still fires
essentially instantly once one lands.

Other deliberate SQS choices worth knowing when touching this pipeline:

- **Visibility timeout is sized at ~6x each Lambda's own timeout** on every
  queue, so a message can never become visible to a second concurrent
  invocation while the first is still legitimately working it.
- **`max_receive_count` + a DLQ per queue** bounds retries -- a chunk that
  can never succeed (a permanently broken PDF page, an engine outage) stops
  retrying and lands in its DLQ instead of looping forever.
- **`max_concurrency` throttles the synthesize and stitch queues'** event
  source mappings, independent of Lambda reserved concurrency (this
  account's total Lambda concurrency budget is small enough that reserving
  any of it per-function isn't possible). This is also what keeps
  synthesis calls to the free, unofficial edge-tts endpoint polite rather
  than bursty.
- **The stitch DLQ has no consumer** -- unlike extract/synthesize, a
  permanently failing stitch leaves the book cleanly `PARTIAL` rather than
  needing an automated retry, so it only needs a depth alarm, not a
  sweeper.

**What could be improved.** An SQS event source mapping's background poller
keeps running for as long as the mapping is enabled, independent of how
long each `ReceiveMessage` call blocks -- long polling only slows the empty
loop's *rate*, it doesn't idle the poller down completely. If idle request
volume ever needs to drop further than that, the next step is to replace
the always-on event source mapping with a **scheduled poll**: an
EventBridge rule invoking each pipeline Lambda directly on a fixed interval,
with the Lambda draining its own queue in that one invocation instead of
Lambda's poller doing it continuously in the background. That trades a
worse best-case latency per pipeline stage (bounded by the poll interval
instead of near-instant) for a much lower and more predictable request
floor, and is the natural next lever here if request volume becomes a
problem again.

## Library context (backend)

`backend/src/contexts/library/` is a DDD-flavoured bounded context following
the repo's `{domain,application,infrastructure,interface}` layout: `domain/`
holds the `Book`/`Chunk` aggregates, status/failure-reason enums, repository
and storage/queue Protocols (no ABCs), and a handful of pure modules with no
AWS/library dependencies (chunking, word-timing alignment, book-global
offset math); `application/` holds one use case per client-facing or
Lambda-triggered operation (request an upload, extract, synthesize a chunk,
stitch, resynthesize); `infrastructure/` holds the DynamoDB/S3/SQS adapters
and the TTS engine implementations (edge-tts primary, Google Cloud TTS
fallback); `interface/` holds the FastAPI controllers and the SQS Lambda
handlers (plus their LocalStack poll-loop equivalents for local dev).

**Upload contract** (what `mobile/lib/upload.ts` does): `POST /books
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

### Audio delivery (phase 6)

`audioKey`/`manifestKey` are S3 keys, not URLs. Turning one into something an
`<audio>` element can play is what phase 6 decided, and the answer is forced
rather than chosen:

- **`GET /books/{id}/audio` returns JSON containing an S3 presigned GET URL**
  (`{url, expiresIn, durationMs, contentType}`), fetched with the normal
  `authFetch`; the client puts that URL in `<audio src>`. `<audio>` issues a
  plain browser GET that **cannot carry an `Authorization` header**, and
  `ApiStack` guards `/{proxy+}` with a Cognito authorizer reading exactly that
  header — so any design where the media request hits our API is dead on
  arrival. Proxying the bytes is not an alternative but an impossibility:
  Lambda's response payload cap is 6 MB and a 300-page book is ~240 MB.
- **Range works, and that is the whole reason seeking works.** SigV4
  query-string presigning signs the method, host, path and query; the signed
  header set is just `host`. `Range` is therefore unsigned and S3 honours
  whatever the browser sends — Chrome issues `Range: bytes=N-` on every seek
  and expects a `206`. `local/smoke_test.py` asserts this against a real S3
  API, because no unit test, mock or fake can prove it.
- **The URL expires in 1 hour, and the client treats that as a hint, not a
  guarantee.** A URL presigned with the Lambda role's *temporary* credentials
  is void when those credentials expire, whatever `ExpiresIn` says. So the
  design is reactive: on the `<audio>` `error` event the client re-requests
  the URL, restores `currentTime` and resumes, bounded at 3 attempts.
- **`409`, not an error, when the book has no audio.** That is the everyday
  state of every environment except local compose.
- **The manifest and the per-chunk marks are proxied through the API**
  (`GET /books/{id}/manifest`, `GET /books/{id}/chunks/{n}/marks`), returning
  the stored S3 objects verbatim. ~60 KB and ~25 KB, nowhere near the 6 MB
  cap, and it buys: no CORS rule on `marks_bucket`, ownership enforced by the
  usual `_load_owned_book` rather than by key opacity, and exactly *one*
  exception to "everything uses `authFetch`" instead of three.

No CORS rule is needed on `audio_bucket` either: a media element loading a
cross-origin `src` without a `crossorigin` attribute is not a CORS request.

### Repairing a `PARTIAL` book (phase 6)

`POST /books/{id}/resynthesize` — `PARTIAL` only, `202` on success. It resets
the `FAILED` chunks to `PENDING`, rewinds `chunksDone` by the same amount,
clears the stale stitch outputs, and re-fires the existing fan-out; when zero
chunks were reset (the `STITCH_FAILED` case, where no chunk will ever
increment again) it publishes a stitch message directly instead.

The obvious alternative — adding `PARTIAL` to `_CLAIMABLE_STATUSES` /
`_REISSUABLE_STATUSES` so the book could simply be re-uploaded or
re-extracted — is exactly the hazard `PARTIAL` was invented to prevent: a
stray redelivered S3 event would claim a book with perfectly good text, call
`delete_for_book`, and re-extract it because the *audio* failed. Those tuples
are unchanged, and `test_status_tuples_unchanged` asserts all three of them
literally so that shortcut fails a unit test before it can fail a book.

The conditional `expected_statuses=(PARTIAL,)` book update is the exactly-once
gate: a double-clicked button, or two open tabs, produces one `202` and one
`409`.

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
runs edge-tts alone and logs a single INFO at startup.

**Single-table key patterns** (table: `bookloud-<env>`, PK/SK both strings):

| item | PK | SK |
|---|---|---|
| book | `USER#<sub>` | `BOOK#<bookId>` |
| chunk | `BOOK#<bookId>` | `CHUNK#<index>` (zero-padded to 6 digits, e.g. `CHUNK#000007`) |
| chat message | `BOOK#<bookId>` | `CHAT#<timestamp>#<id>` |
| chat quota | `USER#<sub>` | `QUOTA#<YYYY-MM>` |

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

## Mobile app (Expo, issue #10)

`mobile/` is an Expo (React Native + TypeScript) Android app talking to the
backend API above. Expo Router + NativeWind, matching the stack of this
user's other Expo apps. Roughly: an auth screen driving Cognito's
username/password + forced-first-login challenge flow, a library screen
(book list, upload, retry/re-upload), and a reader screen (word-level
highlight sync, mini-player, an "Ask" chat sheet).

Background audio uses `expo-av` with `staysActiveInBackground` for
lock-screen/Now-Playing presence; a fully custom lock-screen transport
(remote play/pause/seek) is native-module territory beyond expo-av's surface
and is a known follow-up, not claimed here. Chat's SSE transport uses
`react-native-sse` rather than `fetch().body.getReader()`, which RN/Hermes
doesn't reliably support.

Local dev: `cd mobile && npx expo start`, pointed at a running `make up`
backend or a deployed `pr-N` API via `EXPO_PUBLIC_API_BASE_URL`/
`EXPO_PUBLIC_CHAT_BASE_URL` (see `mobile/.env.example`). Real Android builds
are triggered by publishing a GitHub release (`mobile-release.yml`, EAS
Build, APK attached to the release -- sideload distribution, no Play
Console). PR environments deploy backend only; there is no per-PR mobile
build.

### Highlight sync

**The manifest is the timeline; the audio file is an optimization.**
Position, segment and word are derived from `book.json` plus the per-chunk
marks, not from the player's own reported duration (with no Xing header,
that's an *estimate* on a mixed-engine book).

- A `requestAnimationFrame` loop interpolates the active word between the
  player's own status callbacks, which fire far less often than a spoken
  word passes.
- Position is located with two binary searches -- the manifest's segment
  list, then the rebased word list inside that segment -- but steady state
  is one comparison against the current index, falling back to a full
  search only on a discontinuity (seek).
- Marks are fetched lazily, one segment at a time with a small prefetch and
  cache; a missing marks object (the normal case wherever real TTS hasn't
  run) is negatively cached so the render loop doesn't re-request it dozens
  of times a second.

### Graceful degradation

**If chunk text exists, it renders.** Audio and text degrade independently:
a `PARTIAL`/`NO_AUDIO` book (the *only* terminal state a book reaches
anywhere real TTS is disabled -- see "Real TTS runs in prod only" above)
still shows full text with playback disabled and a "Try audio again"
action; only `FAILED` (extraction itself failing) has no text to show yet.

### Polling

- Book status polling backs off from a fast interval to a slower one after
  an initial window, stops entirely on the server's `terminal` flag or a
  404, and pauses while the app is backgrounded, firing one immediate poll
  on foreground.
- The library list only polls while at least one book is non-terminal, and
  generates zero background traffic once everything has settled.

### Known ceiling

`GET /books/{id}/chunks` returns every chunk's full text in one response.
The hard limit is Lambda's 6 MB response cap, at roughly **3,300 chunks
(~2,700 pages)**. Not paginated, deliberately -- a cursor plus a
virtualized list interacts badly with only rendering the active chunk's
highlighted slice, for a limit no personal library will hit.

## Auth model

Username + password sign-in (not email-based). **Accounts are admin-provisioned
only -- there is no public signup.** The Cognito user pool has
`self_sign_up_enabled=False`, so Cognito itself rejects the `SignUp` API
regardless of what the client does; see "Provisioning a new reader account"
below. The app talks to Cognito's plain JSON API directly (`mobile/lib/
auth.ts`) -- the app client has no secret, so there is nothing a
backend-for-frontend layer would protect. The refresh token lives in
`expo-secure-store`; the id token (~1h) is held there too and attached as
`Authorization: Bearer` on calls to the backend API, which never validates
it itself -- API Gateway's Cognito JWT authorizer does, forwarding the
verified claims to the Lambda.

### First login: forced password change

An admin-provisioned user is created with a *temporary* password. Their
first `InitiateAuth` returns Cognito's `NEW_PASSWORD_REQUIRED` challenge
instead of tokens; the sign-in screen detects this and swaps in a "choose a
new password" step. Once that new password is set, the account behaves like
any other -- normal login, no further forced changes. There is no
self-service "change password while logged in" screen (out of scope).

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
temporary password out of band; their first sign-in in the app will prompt
them to choose their own password before they can use it.

## Quickstart (local dev)

Requires Docker, Python 3.12+, Node 24, and [uv](https://docs.astral.sh/uv/).

Ports follow a fixed `1854X` scheme: `18540` backend, `18541` the Expo dev
server (started separately, below), `18542` chat, `18543` cognito-local.
LocalStack keeps its default `4566` (see `docker-compose.yml`'s header
comment).

```bash
# S3_PUBLIC_HOST=<your-LAN-IP> makes presigned S3 URLs (upload/download)
# reachable from a phone -- omit it and they default to localhost, which
# only works for a browser/emulator on this same machine.
S3_PUBLIC_HOST=<your-LAN-IP> make up
             # LocalStack + cognito-local + backend (uvicorn, hot reload) +
             # extract-worker, synthesize-worker and stitch-worker (poll
             # loops over their respective queues) on :18540
make smoke   # smoke test against the running stack: upload -> extraction ->
             # synthesis fan-out/fan-in -> stitch -> poll
             # GET /books/{id}/status to READY. Real TTS is never called;
             # locally the offline SilentSynthesizer produces real bytes so
             # the concatenation and manifest are genuinely exercised.
make down    # tear everything down
```

Real TTS is never called locally by default -- `synthesize-worker`'s stitched `book.mp3` is genuine MPEG frames, genuinely silent. To actually hear narration before deploying, opt into the one deliberate exception:

```bash
SYNTHESIS_STUB_MODE=edge_tts S3_PUBLIC_HOST=<your-LAN-IP> make up
```

Gated on `ENVIRONMENT=local` exactly (`get_speech_synthesizer()`'s docstring) -- a `pr-N`/CI stack can never reach it even by accident, so the "no automated environment calls a live endpoint" guarantee is untouched. `edge-tts` needs no API key, so this is a manual opt-in with no quota/cost risk, just the usual flakiness of an unofficial endpoint. Don't run `make smoke` in this mode -- it asserts the deterministic `--expect-synthesis silent` outcome and will correctly complain about the mismatch.

Run the Expo app against that backend separately -- `cd mobile && npx expo
start` (fixed at port `18541`), with `EXPO_PUBLIC_API_BASE_URL`/
`EXPO_PUBLIC_CHAT_BASE_URL`/`EXPO_PUBLIC_COGNITO_ENDPOINT` in `mobile/.env`
pointed at `http://<your-LAN-IP>:1854{0,2,3}` (a phone/emulator can't reach
`localhost` on the host). See "Mobile app" above.

`make up` seeds a local Cognito pool (via `jagregory/cognito-local`) with two
users:
- **`dev` / `password`** -- permanent password, no forced change, for
  routine local dev / `make smoke` convenience. Sign in with it in the Expo
  app, or run `curl -H "Authorization: Bearer $(make -s token)"
  localhost:18540/me` to hit the backend directly.
- **`newuser` / `TempPass123!`** -- a *temporary* password, to exercise the
  admin-provisioned forced-first-login flow locally: signing in with it
  triggers the "choose a new password" step instead of a normal login.

Run the full test suite (backend pytest, mobile jest + tsc + eslint, infra
`aws_cdk.assertions` synth tests):

```bash
make test
```

`make synth` runs `cdk synth -c environment=dev` for a local sanity check of
every stack.

**End-to-end tests.** There is no automated mobile e2e suite yet (Detox/
Maestro is a deferred follow-up); `local/smoke_test.py` (see `make smoke`
above) is what actually exercises the full upload -> extract -> synthesize
-> stitch -> status flow today, locally and against every deployed
environment. The constraint to keep in mind when adding real e2e coverage:
`get_speech_synthesizer()` gates real TTS engines on `ENVIRONMENT != "prod"`,
so only local compose's `SYNTHESIS_STUB_MODE=silent` ever produces real
audio bytes to assert against -- every other environment's books settle at
`PARTIAL`/`NO_AUDIO`.

## Repo layout

The general shape, not an exact listing (it will drift -- see the actual
directories for current contents):

- **`backend/`** -- the FastAPI app, DDD-flavoured (bounded contexts under
  `src/contexts/<name>/{domain,application,infrastructure,interface}`),
  packaged as a single Lambda container image. That same image backs the
  API Lambda, the pipeline Lambdas (extract/synthesize/stitch/DLQ-sweeper),
  and the streaming chat Lambda -- they differ only by container `cmd`.
- **`mobile/`** -- the Expo (React Native + TypeScript) Android client,
  Expo Router + NativeWind, talking to the backend over HTTPS.
- **`infra/`** -- the CDK app (Python), one stack per concern (auth,
  storage, the SQS pipeline, the API), synthesized per environment via CDK
  context rather than hand-maintained per-env config.
- **`local/`** -- docker-compose helper scripts (LocalStack/Cognito
  bootstrap, the stdlib-only smoke test used locally, in CI, and against
  every deployed environment).
- **`docs/`** -- operational runbooks (e.g. rollback) that outlive any one
  feature.
- **`.github/workflows/`** -- CI (unit tests + local smoke) and CD
  (per-PR ephemeral deploy/teardown, prod deploy, mobile release) pipelines.
- `docker-compose.yml`, `Makefile` at the root drive local dev.

## Deploy model

A single AWS account hosts prod and every ephemeral PR environment,
distinguished by resource name and (deliberately) by region: prod runs
closer to its one real user, while PR/staging environments share a
separate region to stay off prod's account-level service quotas. A CDK
context value, `environment`, drives every stack/resource name:

| `environment`   | when              | stack names (example)             |
|------------------|-------------------|------------------------------------|
| `dev`            | local default      | `BookloudApi-dev`                  |
| `pr-<N>`         | per-PR ephemeral   | `BookloudApi-pr-42`                |
| `staging`        | pre-prod CD gate   | `BookloudApi-staging`              |
| `prod`           | push to `main`     | `BookloudApi-prod`                 |

- **On every PR**: CI runs backend/mobile/infra unit tests plus a local
  compose smoke test, then deploys the full stack set suffixed `pr-<N>` and
  runs the smoke test against the live API. The stack is left standing
  (that's the point of an ephemeral environment) and a PR comment links to
  its API URL -- there is no per-PR mobile build; point a local
  `expo start` dev client at that URL to test against it.
- **On PR close/merge**: the `pr-<N>` stacks are torn down in reverse
  dependency order.
- **On push to `main`**: the same test-then-deploy sequence runs against an
  ephemeral `staging` stack first (full smoke test, login-gated), then
  deploys to `prod` only if that gate passes.
- **On a GitHub release**: the Android APK is built via EAS and attached to
  the release -- independent of the backend deploy pipeline above.

Removal policy: `RETAIN` for `environment == "prod"`, `DESTROY` (+
`auto_delete_objects`) otherwise -- PR/staging environments vanish
completely on teardown; prod data survives a stack deletion. See
`docs/rollback.md` for the procedure to undo a bad prod deploy.
