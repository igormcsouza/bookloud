# Bookloud

A personal webapp that reads uploaded PDFs aloud with word-level highlighting
synced to audio, plus a chat sidebar for asking questions about the book,
scoped to the section currently being read.

Built phase by phase per `IMPLEMENTATION_PLAN.md`. This repo is currently at
**Phase 1**: Cognito auth. Every non-public backend route requires a valid
Cognito JWT; the frontend has working signup/login/logout/session-refresh
via a Next.js backend-for-frontend; local dev emulates Cognito with
`jagregory/cognito-local`.

## Auth model

Username + password sign-in (not email-based). The browser never talks to
Cognito directly: `POST /api/auth/{login,signup,confirm,refresh,logout}`
(Next.js route handlers) do, over Cognito's plain JSON API. The refresh
token (30-day validity) lives in an `httpOnly; SameSite=Lax` cookie
(`bookloud_refresh`) the browser can't read; the id token (~1h) is held in a
JS module variable and attached as `Authorization: Bearer` on calls to the
backend API, which never validates it itself -- API Gateway's Cognito JWT
authorizer does, forwarding the verified claims to the Lambda. See
`PLANS/phase-1.md` for the full design (and why this topology, not Amplify
Auth or a FastAPI-side `/auth/*`).

## Quickstart (local dev)

Requires Docker, Python 3.12+, Node 24, and [uv](https://docs.astral.sh/uv/).

```bash
make up      # LocalStack + cognito-local + backend (uvicorn, hot reload) on :8000
make ui      # + Next.js frontend on :3000 (optional)
make smoke   # smoke test against the running stack
make down    # tear everything down
```

`make up` seeds a local Cognito pool (via `jagregory/cognito-local`) with a
known-good user: **`dev` / `devpassword`** -- log in with it at
`http://localhost:3000/login` after `make ui`, or run
`curl -H "Authorization: Bearer $(make -s token)" localhost:8000/me` to hit
the backend directly.

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
│               Lambda container image. GET /health + GET /me (auth) today;
│               book/chunk CRUD, the extraction/TTS pipeline, and chat land
│               in phases 2-7. src/auth/ is cross-cutting, not a context.
├── frontend/   Next.js 15 (App Router) + Tailwind, deployed via OpenNext to
│               Lambda + CloudFront. Landing page, login/signup, and the
│               auth BFF route handlers today; the reader UI and chat
│               sidebar land in phases 6-7.
├── infra/      Python CDK app: AuthStack (Cognito + PreSignUp trigger),
│               StorageStack (DynamoDB + S3), ApiStack (HTTP API + Lambda +
│               Cognito JWT authorizer), PipelineStack (SQS), FrontendStack
│               (CloudFront + Lambda + S3).
├── local/      docker-compose helper scripts: setup.sh seeds LocalStack +
│               bootstraps cognito-local, cognito_bootstrap.py provisions
│               the local Cognito pool/client/dev user, smoke_test.py is the
│               stdlib-only smoke test used locally, in CI, and against
│               every deployed environment.
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
