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
| `BOOK#<bookId>` | `CHAT#<msgId>` | role, content, anchoredChunk, createdAt |

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
- Stitcher Lambda triggered when `chunksDone == chunksTotal` (DynamoDB Stream or final-chunk check): concatenates audio, merges timestamp offsets, sets book status `READY`
- `GET /books/{id}/status` polling endpoint
- Tests: stitcher correctness (offset math), status endpoint states

### Phase 6 — Reader UI
- Book list sidebar (polls list + status)
- Center reading pane: renders extracted text, syncs highlight to `<audio>` `timeupdate` via word-boundary data (binary search into timestamp array)
- Play/pause/seek controls
- Tests: Playwright — upload, poll until ready, play, verify highlight follows audio position

### Phase 7 — Chat sidebar
- `POST /books/{id}/chat`, Lambda Function URL streaming response
- Context resolution: current chunk (from playback position) ± neighbors + recent chat turns → LLM call
- Frontend: right sidebar, opens during playback, renders streamed tokens
- Tests: context resolution unit tests, Playwright — ask a question, verify response references book content

### Phase 8 — CD hardening
- Wire the Playwright e2e suite (from phases 6-7) into both the per-PR ephemeral pipeline and the main-branch CD pipeline — the pipeline itself already existed since Phase 0, this phase just adds the real e2e gate now that there's a full flow to test
- CD: on merge to main, unit tests → e2e against a staging/prod-like ephemeral stack → CDK deploy to prod
- Rollback plan documented (CDK stack rollback / previous Lambda version alias)

## Phase checklist

- [x] Phase 0 — Repo, infra scaffolding & CI/CD bootstrap
- [x] Phase 1 — Auth (Cognito)
- [x] Phase 2 — Storage & data model
- [x] Phase 3 — Upload & extraction pipeline
- [ ] Phase 4 — TTS synthesis pipeline
- [ ] Phase 5 — Stitching & status polling
- [ ] Phase 6 — Reader UI
- [ ] Phase 7 — Chat sidebar
- [ ] Phase 8 — CD hardening
