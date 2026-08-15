# PDF Reader App — Implementation Plan

This document drives the build. It is meant to be read by an orchestrating agent (or you, manually) and worked through phase by phase.

## Agent workflow

For each phase below:

1. **Plan** — spin a worker on **Opus**. Give it this document, the current repo state, and the target phase. It produces a concrete implementation plan (files to create/change, interfaces, data shapes, test cases) as a markdown plan, appended to `PLANS/phase-N.md`. It does not write implementation code.
2. **Approve** — a human reviews `PLANS/phase-N.md` and either approves or sends it back with notes.
3. **Implement** — once approved, spin a worker on **Sonnet** with the approved plan. It implements the phase: code, unit tests, and updates to this document's checklist. It should not deviate from the approved plan without flagging it.
4. **Explore** — at any point, either worker may spin a short-lived **Haiku** worker for narrow research/exploration tasks (e.g. "what does edge-tts's WordBoundary event payload look like", "check DynamoDB atomic counter syntax in boto3") — output feeds back into the plan or implementation, Haiku itself never writes final code.

Track phase status in the checklist at the bottom of this file. Each phase is its own branch/PR — do not start phase N+1's planning step until phase N is implemented, tested, and merged.

## Project summary

A personal webapp that reads uploaded PDFs aloud with word-level highlighting synced to audio, plus a chat sidebar for asking questions about the book, scoped to the section currently being read.

**Frontend:** Next.js (SSR) deployed to Lambda + CloudFront.
UI: left sidebar = list of uploaded books; center = reading pane + play controls; right sidebar = chat, opens during playback.

**Backend:** FastAPI on Lambda, DDD structure, DynamoDB (single table), S3 for PDFs/audio/word-timestamp data, SQS for the processing pipeline.

**Auth:** Cognito user pool, username + password login (not email-based) — configure sign-in with username as the identifier, not email/phone alias, at pool creation (cannot be changed after the fact).

**TTS:** edge-tts (free, natural neural voices, built-in word-boundary timestamps) as primary engine; Google Cloud TTS (free tier: 4M chars/month Standard, 1M chars/month WaveNet) as fallback.

**Key architecture decisions:**
- Book/audio processing status is surfaced via **polling** (`GET /books/{id}/status`), not WebSockets or SSE.
- Chat responses are **streamed** token-by-token (Lambda Function URL response streaming).
- Chunk synthesis runs **in parallel** — one SQS-triggered Lambda invocation per chunk, fan-out/fan-in via an atomic `chunksDone` counter on the book record.

**Testing/deploy requirements (apply across all phases):**
- Unit tests for all backend logic (pytest) and frontend logic (vitest/jest) — written alongside each phase, not deferred.
- End-to-end tests with Playwright covering the full user flow (upload → wait for ready → play → highlight sync → chat).
- `docker-compose` for local dev (LocalStack for AWS services, matching the podcast-platform project's pattern).
- CI: run unit tests on every PR; spin up an ephemeral per-PR environment (CDK deploy to a PR-scoped stack) for integration/e2e testing against real infra.
- CD: on merge to main, run unit tests + e2e, then deploy to prod via CDK. Tear down ephemeral PR stacks on merge/close.

## Data model (DynamoDB, single table)

| PK | SK | Attributes |
|---|---|---|
| `USER#<id>` | `BOOK#<bookId>` | title, status, chunksTotal, chunksDone, pageCount, createdAt |
| `BOOK#<bookId>` | `CHUNK#<n>` | text, charStart, charEnd, audioKey, marksKey, status |
| `BOOK#<bookId>` | `CHAT#<timestamp>#<msgId>` | role, content, anchoredChunk, createdAt |
| `USER#<id>` | `QUOTA#<YYYY-MM>` | requestsCount |

## Phases

### Phase 0 — Repo, infra scaffolding & CI/CD bootstrap
This phase's real job is to prove the deploy pipeline works *before* any feature code exists, so every phase from here on gets a genuine ephemeral deploy — not just a unit-test gate.

- Monorepo layout (`/frontend`, `/backend`, `/infra`)
- CDK app skeleton: `AuthStack`, `StorageStack`, `ApiStack`, `PipelineStack`, `FrontendStack` — stacks exist and deploy successfully, even with near-empty resources (e.g. `ApiStack` can just be a Lambda that returns 200 on `/health`)
- `docker-compose.yml` with LocalStack, backend, frontend
- CI workflow: on every PR, deploy all stacks suffixed with the PR number (`pdf-reader-pr-42-*`) to a sandbox AWS account/region; run unit tests against it; tear the stack down when the PR closes or merges
- CD workflow: on merge to main, run unit tests, deploy stacks to prod (no e2e yet — no UI/flow exists to test)
- A trivial end-to-end smoke test (hit `/health` on the ephemeral deploy) proves the whole loop — upload code, CDK synth/deploy, teardown — works end to end

Every later phase's PR automatically gets a real ephemeral deploy for free, growing the stacks incrementally instead of validating against mocks until phase 8.

### Phase 1 — Auth (Cognito)
- `AuthStack`: Cognito user pool configured for username/password sign-in (username as identifier, email optional/non-alias attribute)
- Signup/login API endpoints or direct Amplify/Cognito SDK usage from Next.js
- Frontend login/signup pages
- Tests: signup, login, invalid credentials, protected route rejection without token

### Phase 2 — Storage & data model
- `StorageStack`: S3 buckets (source PDFs, audio, marks), DynamoDB table with the schema above
- Backend repository layer (DDD: `Library` context) for book/chunk CRUD against DynamoDB
- Tests: repository unit tests against LocalStack DynamoDB

### Phase 3 — Upload & extraction pipeline
- Presigned POST endpoint for direct browser-to-S3 upload
- S3 event → SQS → extract Lambda: PyMuPDF text extraction, chunking, writes `CHUNK#n` records with status `PENDING`, sets book status `EXTRACTED`
- Tests: extraction Lambda unit tests with sample PDFs (fixtures), chunking edge cases (headers/footnotes)

### Phase 4 — TTS synthesis pipeline (parallel)
- Fan-out: one SQS message per chunk after extraction
- Synthesize Lambda: calls edge-tts, falls back to Google Cloud TTS on failure, writes audio + word-boundary JSON to S3, updates chunk status `DONE`, atomically increments `chunksDone`
- Reserved concurrency tuning to avoid rate-limit issues
- Tests: synthesize Lambda unit tests (mocked TTS calls), fallback behavior test

### Phase 5 — Stitching & status polling
- Stitcher Lambda triggered when `chunksDone == chunksTotal` (final-chunk check → a dedicated `stitch_queue`; DynamoDB Streams rejected in `PLANS/phase-5.md` §4.1 because an ESM filter structurally cannot compare two attributes): concatenates audio, merges timestamp offsets, sets book status `READY`
  - "Merges timestamp offsets" was **refined** into a *segment manifest*, not a merged word array (`PLANS/phase-5.md` §7.2/Q7): `marks/<u>/<b>/book.json` maps each chunk to its book-global start/duration, char range and byte range, and phase 6 rebases a word with one addition. A merged array for a 330-chunk book would be ~4 MB to download before the first note plays.
  - Terminal status is `READY` only when `chunksFailed == 0`; otherwise the new **`PARTIAL`** (+ `failureReason` `NO_AUDIO`/`STITCH_FAILED`). The stitcher never writes book-level `FAILED`, which stays reserved for extraction — it is re-claimable, so reusing it would let a redelivered S3 event wipe good text (§3).
- `GET /books/{id}/status` polling endpoint, with a server-computed `terminal` flag and `progress.percent`
- Tests: stitcher correctness (offset math), status endpoint states

### Phase 6 additions carried over from phase 5's open questions
- **Audio delivery** (`PLANS/phase-5.md` OQ-3, deferred from phase 5): phase 5 exposes `audioKey`/`manifestKey` as S3 keys but no way to fetch the bytes. Decide presigned GET vs CloudFront origin, Range-request support (the manifest's `b0`/`b1` exist for exactly this) and expiry vs a long `<audio>` session, then ship it. Deferred because nothing in the repo could validate the guess and no environment CI reaches has audio to fetch.
- **Retry synthesis for a `PARTIAL` book** (`PLANS/phase-5.md` OQ-6): `POST /books/{id}/resynthesize` — reset `FAILED` chunks to `PENDING`, re-publish the fan-out for those indexes only, reset the book to `EXTRACTED`. Phase 5 deliberately left `PARTIAL` out of `_CLAIMABLE_STATUSES`/`_REISSUABLE_STATUSES` (adding it would reintroduce the exact hazard `PARTIAL` was created to prevent), so today a prod book whose engines all failed is a dead end until this exists. Lands in phase 6 because that is where a UI can trigger it.

### Phase 6 — Reader UI
- Book list sidebar (polls list + status)
- Center reading pane: renders extracted text, syncs highlight to `<audio>` playback via word-boundary data (binary search into timestamp array)
  - **Refined** in `PLANS/phase-6.md` §6.3/Q9: the highlight is driven by **`requestAnimationFrame`**, not `timeupdate`. Chrome fires `timeupdate` about every 250 ms; at 150 wpm a word is ~400 ms and function words are 120-200 ms, so a 250 ms sampler skips short words outright and visibly jerks on the rest. `timeupdate` keeps a real but narrower job — driving the scrubber. The binary search is unchanged, and is now **two levels**: `segments[].t` in the manifest, then the rebased `words[].t` in that chunk's marks.
- Play/pause/seek controls (plus playback-rate control, `PLANS/phase-6.md` OQ-6)
- **Frontend upload flow** — the presigned POST has existed since phase 3 with no UI to call it
- **Audio delivery** (`PLANS/phase-5.md` OQ-3): `GET /books/{id}/audio` returns an S3 presigned GET URL. Forced, not chosen — an `<audio>` request cannot carry an `Authorization` header, and proxying ~240 MB through Lambda's 6 MB response cap is arithmetic, not preference.
- **`POST /books/{id}/resynthesize`** (`PLANS/phase-5.md` OQ-6), `PARTIAL` only, without touching `_CLAIMABLE_STATUSES`/`_REISSUABLE_STATUSES`
- Tests: Playwright — upload, poll until terminal, play, verify highlight follows audio position. Three specs across two projects, because local compose is the only environment in existence with audio to play (`PLANS/phase-6.md` §13.4).
- **Also fixed here** (`PLANS/phase-6.md` §16 addendum): the recurring `destroy-pr` teardown failure — API routes collapsed from 25 enumerated per-method routes to 5 `ANY` routes, plus a bounded `DELETE_FAILED` retry in `destroy-pr.yml` that still fails loudly.

### Phase 7 — Chat sidebar
- `POST /books/{id}/chat`, Lambda Function URL streaming response
- Context resolution: current chunk (from playback position) ± neighbors + recent chat turns → LLM call
- Frontend: right sidebar, opens during playback, renders streamed tokens
- Tests: context resolution unit tests, Playwright — ask a question, verify response references book content

### Phase 8 — CD hardening
- Wire the Playwright e2e suite (from phases 6-7) into both the per-PR ephemeral pipeline and the main-branch CD pipeline — the pipeline itself already existed since Phase 0, this phase just adds the real e2e gate now that there's a full flow to test
- CD: on merge to main, unit tests → e2e against a staging/prod-like ephemeral stack → CDK deploy to prod
- Rollback plan documented (CDK stack rollback / previous Lambda version alias)
- Observability items deliberately deferred here by earlier phases:
  - CloudWatch alarm on extract/synthesize/stitch DLQ depth, plus a DLQ-consuming Lambda that marks stranded books `FAILED` instead of leaving them stuck (deferred from phase 3, `PLANS/phase-3.md` OQ-4). **Extended by phase 5 (`PLANS/phase-5.md` §4.3/OQ-4):** the same Lambda must also **re-publish a stitch message for any book whose counters are complete but which is still `EXTRACTED`**. That is phase 5's one residual hole — if `enqueue_book` fails on every attempt of the completing chunk's message, the chunk message DLQs and the book sits at 100% forever with nothing to notice. Phase 5's `STITCH_REQUEUED` re-entrancy branch covers every case except the exhausted-retry-budget one, and consolidating recovery here beats inventing a second sweeper.
  - Metric filter + alarm on the synthesizer fallback warning, so Google TTS silently becoming the primary engine (and burning the free tier) is noticed rather than discovered on a bill (deferred from phase 4, `PLANS/phase-4.md` OQ-E)

## Phase checklist

- [x] Phase 0 — Repo, infra scaffolding & CI/CD bootstrap
- [x] Phase 1 — Auth (Cognito)
- [x] Phase 2 — Storage & data model
- [x] Phase 3 — Upload & extraction pipeline
- [x] Phase 4 — TTS synthesis pipeline
- [x] Phase 5 — Stitching & status polling
- [x] Phase 6 — Reader UI
- [x] Phase 7 — Chat sidebar
- [ ] Phase 8 — CD hardening
