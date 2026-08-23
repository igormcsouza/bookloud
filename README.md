# Bookloud

A personal Android app that reads uploaded PDFs aloud with word-level
highlighting synced to audio, plus a chat sidebar for asking questions about
the book, scoped to the section currently being read.

Built phase by phase per `IMPLEMENTATION_PLAN.md`, phases 1-7 land the
backend and (at the time) a Next.js web frontend; **issue #10** then drops
that web frontend entirely in favor of a native Expo/React Native Android
app (`mobile/`) against the same, unchanged backend API -- better fit for
background audio, lock-screen controls, and one-handed reading-while-
listening than a browser tab. The sections below describing the old Next.js
`frontend/` are kept as historical record of what phases 6-7 built; see
"Mobile app (Expo, issue #10)" for the current client.

Every non-public backend route requires a valid Cognito JWT; the client
authenticates directly against Cognito's plain JSON API (no backend-for-
frontend -- the app client has no secret, so there's nothing a server layer
would protect); local dev emulates Cognito with `jagregory/cognito-local`.
The backend's
`Library` bounded context owns `Book`/`Chunk` CRUD against the single DynamoDB
table plus the full pipeline: `POST /books` creates a book and returns a
presigned S3 upload; the PDF landing in the bucket fires an S3 event -> SQS ->
the extract Lambda (PyMuPDF extraction, running header/footer filtering,
chunking, `CHUNK#n` rows, book -> `EXTRACTED`); a per-chunk fan-out drives the
synthesize Lambda (TTS -> MP3 + word timings, chunk -> `DONE`, atomic
`chunksDone` increment); and the increment that completes the book publishes
one message to a third queue where the **stitch Lambda** concatenates every
`DONE` chunk's MP3 into a single `book.mp3`, writes a book manifest
(`book.json`) rebasing each chunk into book-global milliseconds, and flips the
book to `READY` or `PARTIAL`. `GET /books/{id}/status` is the one cheap poll a
client needs.

The client (originally the phase-6/7 Next.js frontend, now the `mobile/`
Expo app) has a reader screen showing the extracted text with the spoken
word highlighted, and player controls that play, pause, seek and change
speed. Uploading a PDF has a UI (the presigned POST has existed since phase 3
with nothing calling it), and a `PARTIAL` book can be repaired with
`POST /books/{id}/resynthesize` instead of being a dead end.

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
│                    s3_client.py (the one browser-reachable S3 client),
                    s3_pdf_storage.py (presigned POST + get_bytes),
                    s3_audio_delivery.py (presigned GET, SigV4),
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
                     + GET /books/{id}/status + (phase 6)
                     GET /books/{id}/audio + GET /books/{id}/manifest +
                     GET /books/{id}/chunks/{n}/audio +
                     GET /books/{id}/chunks/{n}/marks +
                     POST /books/{id}/resynthesize controllers,
                     extract_handler.py + synthesize_handler.py +
                     stitch_handler.py (the three SQS Lambda handlers), and
                     local_{extract,synthesize,stitch}_worker.py (their
                     LocalStack-dev poll-loop equivalents)
                     plus (phase 7) chat_controllers.py (streaming via a
                     secondary ASGI app inside the new ChatFunction)
```

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
runs edge-tts alone and logs a single INFO at startup. See `PLANS/phase-4.md`
for the full decisions log.

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

See `PLANS/phase-2.md` for the full design rationale and decisions log.

## Reader UI (historical: the phase-6/7 Next.js frontend)

**Superseded by issue #10's `mobile/` Expo app** -- kept here as a record of
what phases 6-7 built; the code below no longer exists in this repo. See
"Mobile app (Expo, issue #10)" further down for the current client.

Route group `app/(app)/` holds the two-pane shell — `/` is the library,
`/books/<id>` is the reader. Route groups don't appear in URLs, so `/login`
and `/signup` keep their own bare centred layout outside it, and
`middleware.ts`'s existing matcher already gates `/books/*`.

```
frontend/
├── app/(app)/layout.tsx       server: <BooksProvider><BookSidebar/>{children}
├── app/(app)/page.tsx         server: "/" — the library empty state
├── app/(app)/books/[bookId]/  server: <ReaderView bookId=…/>
├── components/                BookSidebar, BookListItem, UploadButton,
│                              ReaderView, ReadingPane, ChunkParagraph,
│                              PlayerBar, AudioNotice, HealthBadge,
│                              BooksProvider
├── hooks/                     useBookList, useBookStatus, usePlayback
└── lib/                       books.ts (API client), manifest.ts, marks.ts,
                               upload.ts
```

**Route shells are server components; every data-touching component is
`"use client"`.** Not a style preference: the id token lives in a browser
module variable (`lib/auth.ts`), and the only credential the SSR Lambda ever
sees is the httpOnly refresh cookie. A server component calling the API on the
user's behalf would need a Cognito `InitiateAuth` round trip plus the API call
on the render path of a Lambda competing for an account-wide budget of 10
concurrent executions — on every navigation, for data the client is about to
start polling anyway.

**No store, no data-fetching library, no new runtime dependency.** The runtime
dependencies are still exactly `next`, `react`, `react-dom`. Three hooks and
one small React context share the three things this screen actually shares: the
book list, the open book's status, and playback position. Revisit when phase
7's chat sidebar adds a third long-lived slice and a concrete invalidation
problem — not pre-emptively.

### Highlight sync

**The manifest is the timeline; the audio file is an optimization.** Position,
segment and word are derived from `book.json` plus the per-chunk marks, never
from `audio.duration` (which, with no Xing header, is a browser *estimate* for
a mixed-engine book).

- **`requestAnimationFrame` drives the highlight (~60 Hz), `timeupdate` drives
  the scrubber (~4 Hz).** This refines `IMPLEMENTATION_PLAN.md`'s phase-6
  bullet, which said `timeupdate`. Chrome fires `timeupdate` about every
  250 ms; at 150 wpm a word is ~400 ms and function words are 120-200 ms, so a
  250 ms sampler skips short words outright and visibly jerks on the rest.
- **Two binary searches, both `bisectRight(…) - 1`**: `segments[].t` (≤330
  entries) then the rebased `words[].t` (~300). Steady state is not a search
  at all — the loop keeps the current index and does one comparison
  (`stillInside`), falling back to the bisect only on a discontinuity.
- **A word is lit from `words[i].t` until `words[i+1].t`**, by start
  boundaries rather than `[t, t+d)`. The partition is total (no dark gap
  between words), `stillInside` is exactly the inverse of `locateWord`, and
  the "engines report word ends optimistically" clamping problem disappears.
- **Rebasing is one addition, applied once at cache insert**: `t_global =
  segment.t + word.t`. `s`/`e` stay **chunk-relative** — that asymmetry is
  what lets `ChunkParagraph` slice the active paragraph with no global-offset
  bookkeeping.
- **No per-word `<span>`s.** One `<p>` per chunk; only the active chunk renders
  `text.slice(0,s)` + `<mark>` + `text.slice(e)`. `React.memo` keeps the other
  paragraphs untouched, and `setState` fires only when the word index actually
  changes (~2-3 Hz, not 60).
- **Marks are fetched lazily, one segment at a time, with a one-segment
  prefetch and a bounded cache.** Fetching all of them up front is ~6.6 MB
  across 330 requests before the first note plays. A 404 (the normal case for
  every chunk in a PR environment) is **negatively cached** — without that, the
  rAF loop would re-request a missing marks object 60 times a second.

### Graceful degradation

**If chunk text exists, it renders.** There is no state in which an error
screen replaces the reading pane while text is available.

| book state | reading pane | player | notice |
|---|---|---|---|
| `UPLOADED`/`EXTRACTING` | skeleton | absent | "Reading your PDF…" |
| `EXTRACTED`/`STITCHING` | **full text** | disabled | "Preparing audio — n%" + bar |
| `FAILED` | — | absent | "We couldn't read this PDF (reason)." + Re-upload |
| `READY` | full | enabled | — |
| `PARTIAL`, no reason | full | **enabled** | "n of m sections have no audio — playback skips them." |
| `PARTIAL`/`NO_AUDIO`, chunks `EXTERNAL_TTS_DISABLED` | full | absent | "Text-to-speech is turned off in this environment. You can still read the book." + Try audio again |
| `PARTIAL`/`NO_AUDIO`, chunks `ALL_ENGINES_FAILED` | full | absent | "Every text-to-speech engine failed for this book. You can still read it." + Try audio again |
| `PARTIAL`/`STITCH_FAILED` | full | disabled | "Audio couldn't be assembled." + Try audio again |
| manifest object gone | full | absent | "Audio information is unavailable. You can still read the book." |

Rows 6 and 7 are not edge cases: because real TTS is prod-only, `PARTIAL`/
`NO_AUDIO` is the *only* terminal state a book ever reaches in local dev and in
every ephemeral PR environment.

### Polling

- **Reader**: `GET /books/{id}/status` every 2 s, backing off to 5 s after
  60 s, stopping on the server's `terminal` flag, on a 404, or after a
  15-minute hard cap.
- **Sidebar**: `GET /books` every 5 s **only while some book is non-terminal**,
  and not at all when every book is terminal — the common steady state should
  generate zero background traffic.
- Both suspend on `document.hidden` and fire one immediate poll on becoming
  visible. That is a correctness requirement, not a nicety: a forgotten tab at
  2 s/poll is ~43,000 requests/day against an API Lambda with ~5 executions of
  headroom.

### Known ceiling

`GET /books/{id}/chunks` returns every chunk's full text in one response. The
hard limit is Lambda's 6 MB response cap, at roughly **3,300 chunks (~2,700
pages)**. Not paginated, deliberately — a cursor plus a virtualized scroll
container interacts badly with the slice-the-active-paragraph rendering above,
for a limit no personal library will hit. The reader logs a console warning
above 2,000 chunks so the day it matters is not a mystery.

## Mobile app (Expo, issue #10)

`mobile/` is an Expo (React Native + TypeScript) Android app, replacing the
Next.js frontend above against the same, unchanged backend API -- no backend
or infra changes were needed. Expo Router + NativeWind, matching the stack
of this user's other Expo apps.

```
mobile/
├── app/(auth)/sign-in.tsx      username/password + NEW_PASSWORD_REQUIRED,
│                                one screen (Cognito's challenge flow)
├── app/(app)/library/          index.tsx (book list, upload FAB, retry/
│                                re-upload), add.tsx (5-step upload progress)
├── app/(app)/reader/[bookId]/  word-level highlight sync, mini-player,
│                                the "Ask" chat bottom sheet
├── components/                 BookCard, StatusChip, MiniPlayer, ChatSheet,
│                                Field, PrimaryButton
├── hooks/                      usePlayback (expo-av, rewritten from the web
│                                rAF/HTMLAudioElement version), useBookList,
│                                useBookStatus, useChat, useReadingAnchor
└── lib/                        auth.ts, api.ts, books.ts, manifest.ts,
                                 marks.ts, chat.ts, upload.ts, storage.ts
```

`manifest.ts`/`marks.ts`/`books.ts` port near-verbatim from the old
`frontend/lib/`; `usePlayback` is the one full rewrite (expo-av's
lower-frequency status callbacks instead of `requestAnimationFrame` against
`HTMLAudioElement`); `chat.ts`'s SSE transport uses `react-native-sse`
instead of `fetch().body.getReader()`, which RN/Hermes doesn't reliably
support.

Background audio uses `expo-av` with `staysActiveInBackground` for
lock-screen/Now-Playing presence; a fully custom lock-screen transport
(remote play/pause/seek) is native-module territory beyond expo-av's surface
and is a known follow-up, not claimed here.

Local dev: `cd mobile && npx expo start`, pointed at a running `make up`
backend or a deployed `pr-N` API via `EXPO_PUBLIC_API_BASE_URL`/
`EXPO_PUBLIC_CHAT_BASE_URL` (see `mobile/.env.example`). Real Android builds
are triggered by publishing a GitHub release (`mobile-release.yml`, EAS
Build, APK attached to the release -- sideload distribution, no Play
Console). PR environments deploy backend only; there is no per-PR mobile
build.

## Auth model

Username + password sign-in (not email-based). **Accounts are admin-provisioned
only -- there is no public signup.** The Cognito user pool has
`self_sign_up_enabled=False`, so Cognito itself rejects the `SignUp` API
regardless of what the client does; see "Provisioning a new reader account"
below. The app talks to Cognito's plain JSON API directly (`mobile/lib/
auth.ts`) -- the app client has no secret, so there is nothing a
backend-for-frontend layer would protect, unlike the old Next.js frontend's
route-handler BFF. The refresh token lives in `expo-secure-store`; the id
token (~1h) is held there too and attached as `Authorization: Bearer` on
calls to the backend API, which never validates it itself -- API Gateway's
Cognito JWT authorizer does, forwarding the verified claims to the Lambda.
See `PLANS/phase-1.md` for the full design (and why this topology, not
Amplify Auth or a FastAPI-side `/auth/*`); §11 covers the admin-only
revision.

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

Gated on `ENVIRONMENT=local` exactly (`get_speech_synthesizer()`'s docstring) -- a `pr-N`/CI stack can never reach it even by accident, so the "no automated environment calls a live endpoint" guarantee (`PLANS/phase-4.md` Q21) is untouched. `edge-tts` needs no API key, so this is a manual opt-in with no quota/cost risk, just the usual flakiness of an unofficial endpoint. Don't run `make smoke` in this mode -- it asserts the deterministic `--expect-synthesis silent` outcome and will correctly complain about the mismatch.
```

Run the Expo app against that backend separately -- `cd mobile && npx expo
start` (fixed at port `18541`), with `EXPO_PUBLIC_API_BASE_URL`/
`EXPO_PUBLIC_CHAT_BASE_URL`/`EXPO_PUBLIC_COGNITO_ENDPOINT` in `mobile/.env`
pointed at `http://<your-LAN-IP>:1854{0,2,3}` (a phone/emulator can't reach
`localhost` on the host). See "Mobile app" above.

`make up` seeds a local Cognito pool (via `jagregory/cognito-local`) with two
users:
- **`dev` / `devpassword`** -- permanent password, no forced change, for
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

**End-to-end tests (historical: the phase-6/7 Next.js frontend's Playwright
suite).** Issue #10 removed `frontend/e2e/` along with the rest of the
Next.js app, and did not replace it -- a mobile e2e suite (Detox/Maestro) is
an explicitly deferred follow-up. Kept below as a record of what existed and
why, since the underlying constraint (`get_speech_synthesizer()` gates on
`ENVIRONMENT != "prod"`, so only local compose's `SYNTHESIS_STUB_MODE=silent`
ever produces real audio to test against) still applies to whatever replaces
it.

| spec (no longer in the repo) | local compose (`E2E_AUDIO=1`) | `deploy-pr` | asserted |
|---|---|---|---|
| `e2e/upload-and-read.spec.ts` | yes | yes | login -> upload -> poll to terminal -> the text renders. **No audio assertion at all.** |
| `e2e/degraded.spec.ts` | skipped (the local book is `READY`) | yes | on a `PARTIAL`/`NO_AUDIO` book: text readable, no `<audio src>` mounted, the notice copy, and "Try audio again" -> `202` -> back to terminal |
| `e2e/playback.spec.ts` | yes | not created | play, the highlight advances, pause, seek across a segment boundary and back |

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
│               extract, synthesize, and stitch Lambdas, plus the streaming
│               ChatFunction, differing only by their container `cmd` or
│               Lambda handler.
├── mobile/     Expo (React Native + TypeScript) Android app, replacing the
│               Next.js frontend (issue #10) against the same backend API.
│               Expo Router + NativeWind. app/(auth)/, app/(app)/library/,
│               app/(app)/reader/[bookId]/, components/, hooks/ (usePlayback
│               rewritten for expo-av), lib/ (auth, api, books, manifest,
│               marks, chat, upload -- most ported near-verbatim from the
│               old frontend/lib/). Built to APK via EAS on GitHub release
│               (mobile-release.yml), not part of the PR/prod deploy
│               pipeline below.
├── infra/      Python CDK app: AuthStack (Cognito, self_sign_up_enabled=
│               False, PreSignUp trigger kept but unreachable), StorageStack
│               (DynamoDB + S3), PipelineStack (extract/synthesize/stitch
│               SQS queues + DLQs, the three pipeline Lambdas, and the
│               S3 -> SQS notification), ApiStack (HTTP API + Lambda + Cognito JWT
│               authorizer). Dependency order: Storage -> Pipeline -> Api
│               (Auth feeds into Api too). No FrontendStack -- issue #10
│               removed it along with the Next.js app it deployed.
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
├── .github/workflows/   ci.yml (reusable: backend/mobile/infra tests +
│                        local-smoke), deploy-pr.yml (backend deploy + smoke
│                        test against a pr-N stack), destroy-pr.yml,
│                        deploy-prod.yml, mobile-release.yml (EAS Android
│                        build on GitHub release)
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

- **On every PR**: `ci.yml` runs backend/mobile/infra unit tests plus a local
  compose smoke test; `deploy-pr.yml` then deploys all four stacks suffixed
  `pr-<N>` to the same AWS account and runs `local/smoke_test.py` against
  the live API. The stack is left standing (that's the point of an
  ephemeral environment) and a PR comment links to its API URL -- there is
  no per-PR mobile build; point a local `expo start` dev client at that URL
  to test against it.
- **On PR close/merge**: `destroy-pr.yml` deletes the four `pr-<N>` stacks in
  reverse dependency order (`Api -> Pipeline -> Auth -> Storage`).
- **On push to `main`**: `deploy-prod.yml` runs the same test-then-deploy
  sequence against the `prod` stacks.
- **On a GitHub release**: `mobile-release.yml` builds the Android APK via
  EAS and attaches it to the release -- independent of the backend deploy
  pipeline above.

Removal policy: `RETAIN` for `environment == "prod"`, `DESTROY` (+
`auto_delete_objects`) otherwise -- PR environments vanish completely on
teardown; prod data survives a stack deletion.

See `PLANS/phase-0.md` for the full rationale, including the container-image
Lambda packaging (needed from phase 3 onward for PyMuPDF/edge-tts), the
OpenNext + CloudFront wiring, and the open questions still pending human
sign-off (AWS account ID, GitHub secrets).
