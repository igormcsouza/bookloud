# Phase 0 — Repo, infra scaffolding & CI/CD bootstrap

Goal: prove the whole loop (push code → CDK synth → ephemeral deploy → smoke test → teardown)
works *before* any feature code exists. Every later phase then gets a real ephemeral deploy for free.

Reference implementations in the author's own projects (patterns copied deliberately):
- `/home/yngvarr/Projects/cashlytics` — Python CDK, FastAPI Lambda container, OpenNext frontend
  stack, per-PR deploy/destroy GitHub workflows, local smoke test. **Primary reference.**
- `/home/yngvarr/Projects/jgautocar` — DDD backend layout (`contexts/<ctx>/{domain,application,
  infrastructure,interface}`, `shared_kernel/`).

---

## 1. Directory / file layout

```
bookloud/
├── .github/
│   ├── CODEOWNERS
│   └── workflows/
│       ├── ci.yml                  # reusable: unit tests + synth + local-stack smoke
│       ├── deploy-pr.yml           # PR opened/sync -> ephemeral deploy + smoke
│       ├── destroy-pr.yml          # PR closed/merged -> teardown
│       └── deploy-prod.yml         # push to main -> tests + prod deploy + smoke
├── .gitignore
├── .env.example
├── Makefile
├── README.md                       # updated: quickstart, layout, deploy model
├── IMPLEMENTATION_PLAN.md          # (exists) checklist updated at end of phase
├── PLANS/
│   └── phase-0.md                  # this document
├── docker-compose.yml
├── backend/
│   ├── .dockerignore
│   ├── Dockerfile                  # multi-target: `lambda` (default) and `dev`
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── lambda_function.py          # Mangum handler wrapping src.main:app
│   ├── src/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app, router registration, error handlers
│   │   ├── config.py               # env-driven Settings (single source of env var names)
│   │   ├── health/
│   │   │   ├── __init__.py
│   │   │   └── controllers.py      # GET /health
│   │   ├── shared_kernel/
│   │   │   ├── __init__.py
│   │   │   ├── domain/
│   │   │   │   ├── __init__.py
│   │   │   │   └── errors.py       # DomainError, NotFoundError, ConflictError
│   │   │   └── application/
│   │   │       ├── __init__.py
│   │   │       └── ports.py        # Clock, IdGenerator protocols
│   │   ├── infrastructure/
│   │   │   ├── __init__.py
│   │   │   └── aws.py              # boto3 client factory (honours AWS_ENDPOINT_URL)
│   │   └── contexts/
│   │       ├── __init__.py
│   │       └── .gitkeep            # library/ reading/ chat/ land in phases 2-7
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_health.py
│       ├── test_config.py
│       └── test_lambda_function.py
├── frontend/
│   ├── .dockerignore
│   ├── .env.example
│   ├── .gitignore
│   ├── Dockerfile                  # prod-equivalent `next start` image (compose `ui` profile)
│   ├── next.config.mjs
│   ├── next-env.d.ts
│   ├── package.json
│   ├── package-lock.json
│   ├── postcss.config.mjs
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   ├── vitest.config.ts
│   ├── vitest.setup.ts
│   ├── playwright.config.ts        # scaffolded now, first real spec in phase 6
│   ├── app/
│   │   ├── globals.css
│   │   ├── layout.tsx
│   │   └── page.tsx                # minimal landing page + API health badge
│   ├── lib/
│   │   └── api.ts                  # apiUrl() helper reading NEXT_PUBLIC_API_BASE_URL
│   ├── __tests__/
│   │   ├── page.test.tsx
│   │   └── api.test.ts
│   ├── e2e/
│   │   └── .gitkeep
│   └── public/
│       └── .gitkeep
├── infra/
│   ├── .gitignore                  # cdk.out/, *.outputs.json
│   ├── app.py                      # CDK app entrypoint, stack wiring
│   ├── cdk.json
│   ├── requirements.txt            # aws-cdk-lib, constructs
│   ├── requirements-dev.txt        # pytest
│   ├── stacks/
│   │   ├── __init__.py
│   │   ├── config.py               # naming helpers + env var name constants
│   │   ├── auth_stack.py
│   │   ├── storage_stack.py
│   │   ├── api_stack.py
│   │   ├── pipeline_stack.py
│   │   └── frontend_stack.py
│   └── tests/
│       ├── __init__.py
│       └── test_synth.py           # aws_cdk.assertions over every stack
└── local/
    ├── setup.sh                    # wait for LocalStack, create bucket/table/queues
    └── smoke_test.py                # stdlib-only smoke test (local + deployed)
```

---

## 2. CDK app structure

### 2.1 Language: **Python CDK** (`aws-cdk-lib` via pip)

Chosen over TypeScript CDK deliberately — flag for human sign-off (see §10, Q1):
- Both of the author's existing AWS projects (`cashlytics`, `jgautocar`) use Python CDK with the
  exact stack shapes this phase needs. Copying working code beats re-deriving it in another language.
- The backend is Python; one language for infra + backend means one lint/format/test toolchain,
  and infra unit tests run in the same pytest invocation style as backend tests.
- The only TS-specific tooling we need — OpenNext — is a CLI (`npx open-next build`) invoked from
  the frontend package, not a CDK construct, so it is language-agnostic.
- If the human prefers TS, the mapping is 1:1 (`aws-cdk-lib` L2 constructs are identical); switch
  `infra/` to `bin/bookloud.ts` + `lib/*-stack.ts` + `jest` snapshot tests. Nothing else changes.

Versions: Python 3.12, `aws-cdk-lib>=2.225.0`, `constructs>=10.4.2`, CDK CLI installed in CI via
`npm install -g aws-cdk`.

### 2.2 Environment / naming model

A single context value `environment` drives every name:

| environment value | when              | set by                                        |
|-------------------|-------------------|-----------------------------------------------|
| `dev`             | local default     | `Config.DEFAULT_ENVIRONMENT` in `stacks/config.py` |
| `pr-<N>`          | per-PR ephemeral  | `-c environment=pr-${{ github.event.pull_request.number }}` |
| `prod`            | main branch       | `-c environment=prod`                          |

Stack names: `Bookloud{Auth,Storage,Api,Pipeline,Frontend}-{environment}`
→ e.g. `BookloudApi-pr-42`, `BookloudFrontend-prod`.

(IMPLEMENTATION_PLAN.md writes this as `pdf-reader-pr-42-*`; the repo is named `bookloud`, so the
plan uses the `Bookloud*-pr-42` form — see §10, Q6.)

Resource naming rules, to avoid the classic per-PR collisions:
- **Explicitly named** (needed by local tooling / cross-service references, and short enough):
  DynamoDB table `bookloud-{env}`, SQS queues `bookloud-{env}-extract`, `bookloud-{env}-synthesize`
  (+ `-dlq`), Cognito pool `bookloud-{env}`, HTTP API `bookloud-{env}`.
- **CDK-auto-named**: all S3 buckets (global namespace + 63-char limit make explicit names a
  liability). Their names are surfaced via `CfnOutput` and injected into Lambdas as env vars.
- Removal policy: `RETAIN` when `environment == "prod"`, `DESTROY` + `auto_delete_objects=True`
  otherwise. PR environments must vanish completely on teardown.

Second context value `git_sha` (default `"local"`) is threaded into the API Lambda's env so the
smoke test can assert the deployed code matches the commit under test.

### 2.3 `infra/app.py` wiring

```
app = cdk.App()
env = cdk.Environment(account=CDK_DEFAULT_ACCOUNT, region=CDK_DEFAULT_REGION)
environment = app.node.try_get_context("environment") or Config.DEFAULT_ENVIRONMENT
git_sha     = app.node.try_get_context("git_sha") or "local"

storage  = StorageStack(app,  f"BookloudStorage-{environment}",  environment=..., env=env)
auth     = AuthStack(app,     f"BookloudAuth-{environment}",     environment=..., env=env)
pipeline = PipelineStack(app, f"BookloudPipeline-{environment}", environment=..., env=env)
api      = ApiStack(app,      f"BookloudApi-{environment}",
                    table=storage.table, pdf_bucket=storage.pdf_bucket,
                    audio_bucket=storage.audio_bucket, marks_bucket=storage.marks_bucket,
                    extract_queue=pipeline.extract_queue,
                    environment=..., git_sha=git_sha, env=env)
FrontendStack(app,            f"BookloudFrontend-{environment}",
                    api_base_url=api.http_api.url or "", environment=..., env=env)
app.synth()
```

Dependency graph (drives deploy order and, reversed, teardown order):
`Storage`, `Auth`, `Pipeline` (independent) → `Api` (imports Storage + Pipeline) → `Frontend`
(imports Api). **`AuthStack` is intentionally not referenced by `ApiStack` in Phase 0** — Phase 1
adds the JWT authorizer wiring.

### 2.4 Stack contents

**`StorageStack`** — everything here is fully specified in IMPLEMENTATION_PLAN.md's data model, so
there is no design risk in creating it now; anything *not* specified (GSIs, streams, lifecycle
rules) is deferred to phase 2+.
- DynamoDB table `bookloud-{env}`: PK `PK` (S), SK `SK` (S), `PAY_PER_REQUEST`, PITR on prod only.
- Three S3 buckets: `pdf_bucket`, `audio_bucket`, `marks_bucket`. All `BLOCK_ALL` public access,
  `S3_MANAGED` encryption, versioning off. CORS on `pdf_bucket` (`PUT`/`POST`/`GET`, `*` origins)
  so phase 3's presigned browser upload works without a stack change.
- Exposes `self.table`, `self.pdf_bucket`, `self.audio_bucket`, `self.marks_bucket`.
- Outputs: `TableName`, `PdfBucketName`, `AudioBucketName`, `MarksBucketName`.

**`AuthStack`** — created now on purpose: Cognito sign-in aliases are **immutable after pool
creation**, and IMPLEMENTATION_PLAN.md calls this out explicitly. Getting it right in Phase 0 means
Phase 1 is pure application code.
- `cognito.UserPool` name `bookloud-{env}`, `sign_in_aliases=SignInAliases(username=True)` (email
  and phone aliases **off**), `self_sign_up_enabled=True`, `standard_attributes`: email optional and
  not an alias, `account_recovery=NONE` for non-prod / `EMAIL_ONLY` for prod.
- Password policy: strict on prod (min 12, all classes), relaxed on non-prod (min 8, no class
  requirements) so PR envs can use a well-known dev password.
- One app client `WebClient` with `auth_flows=AuthFlow(user_password=True, user_srp=True)` and
  `prevent_user_existence_errors=True`.
- Exposes `self.user_pool`, `self.user_pool_client`. Outputs `UserPoolId`, `UserPoolClientId`.

**`ApiStack`** — the health-check Lambda + HTTP API.
- `lambda_.DockerImageFunction("ApiFunction", code=DockerImageCode.from_image_asset(backend_dir))`,
  memory 512 MB, timeout 30 s. Container image (not a zip) because phases 3-4 need PyMuPDF and
  edge-tts, which blow past the zip layer limits; establishing it now avoids a migration later.
- Environment variables (names centralised in `stacks/config.py` and mirrored in
  `backend/src/config.py`): `ENVIRONMENT`, `GIT_SHA`, `TABLE_NAME`, `PDF_BUCKET`, `AUDIO_BUCKET`,
  `MARKS_BUCKET`, `EXTRACT_QUEUE_URL`, `LOG_LEVEL`.
- Grants: `table.grant_read_write_data(fn)`, `*_bucket.grant_read_write(fn)`,
  `extract_queue.grant_send_messages(fn)`.
- `apigwv2.HttpApi` named `bookloud-{env}` with CORS preflight (`allow_origins=["*"]`,
  headers `Authorization, Content-Type`, methods GET/POST/PUT/DELETE/OPTIONS).
- Routes → `HttpLambdaIntegration`, methods `[GET, HEAD, POST, PUT, DELETE]` (OPTIONS deliberately
  excluded so preflight falls through to API Gateway's auto-response): explicit public routes
  `/health`, `/`, `/docs`, `/redoc`, `/openapi.json`, plus a `/{proxy+}` catch-all. No authorizer in
  Phase 0; Phase 1 adds `HttpUserPoolAuthorizer` on `/{proxy+}` only.
- Exposes `self.http_api`. Outputs `ApiUrl`, `ApiFunctionName`.

**`PipelineStack`** — queues only, no consumers yet.
- `extract_queue` (`bookloud-{env}-extract`) and `synthesize_queue` (`bookloud-{env}-synthesize`),
  each with a dead-letter queue (`-dlq`, `max_receive_count=3`), `visibility_timeout=5 min`,
  `retention_period=4 days`.
- Exposes `self.extract_queue`, `self.synthesize_queue`. Outputs both queue URLs.

**`FrontendStack`** — Next.js SSR on Lambda behind CloudFront (details in §4).
- Private S3 assets bucket + `lambda_.Function` (Node 22/24 runtime) from `.open-next/server-
  functions/default` + Function URL + CloudFront distribution with two behaviours.
- **Placeholder fallback**: if `frontend/.open-next/server-functions/default` does not exist at
  synth time, use `lambda_.Code.from_inline(...)` returning a "Bookloud is deploying…" page. This
  keeps `cdk synth` (and therefore `infra/tests/test_synth.py` and the CI synth check) working with
  no frontend build.
- Outputs `FrontendUrl` (`https://{distribution_domain_name}`), `AssetsBucketName`.

### 2.5 Infra unit tests (`infra/tests/test_synth.py`)

Uses `aws_cdk.assertions.Template`; runs with no AWS credentials. Note `DockerImageCode.from_image_asset`
requires Docker at synth; if that proves awkward in CI, gate the ApiStack assertions behind a
`pytest.mark.docker` and keep the rest.
Assertions:
- Every stack synthesises for `environment="pr-99"` and for `environment="prod"`.
- Stack names match `Bookloud*-{env}`.
- `StorageStack`: exactly 1 `AWS::DynamoDB::Table` with `KeySchema` PK/SK; 3 `AWS::S3::Bucket`.
- prod → `DeletionPolicy: Retain` on table and buckets; `pr-99` → `Delete`.
- `AuthStack`: `AWS::Cognito::UserPool` with `UsernameAttributes` absent and `AliasAttributes`
  absent (i.e. username-only sign-in) — this is the assertion that protects the immutable setting.
- `ApiStack`: 1 `AWS::Lambda::Function`, `AWS::ApiGatewayV2::Api`, and a route for `/health`.
- `PipelineStack`: 4 `AWS::SQS::Queue` (2 + 2 DLQ) with redrive policies.
- `FrontendStack`: 1 `AWS::CloudFront::Distribution` with 2 cache behaviours.

---

## 3. Backend skeleton

FastAPI, DDD-flavoured but nearly empty. Python 3.12, dependencies managed with **uv**
(`pyproject.toml` + `uv.lock`).

`pyproject.toml`:
- runtime deps: `fastapi>=0.115`, `mangum>=0.19`, `boto3>=1.35`, `uvicorn[standard]>=0.34`,
  `pydantic-settings>=2.6`
- dev group: `pytest>=8.3`, `pytest-cov>=6.0`, `httpx` (TestClient), `moto[dynamodb,s3,sqs]>=5.0`
- `[tool.pytest.ini_options]`: `pythonpath=["."]`, `testpaths=["tests"]`,
  `addopts = "--cov=src --cov=lambda_function --cov-report=term-missing --cov-fail-under=90"`

`src/config.py` — a `Settings` (pydantic-settings `BaseSettings`) object read once at import:
`environment` (`ENVIRONMENT`, default `"local"`), `git_sha` (`GIT_SHA`, default `"local"`),
`table_name`, `pdf_bucket`, `audio_bucket`, `marks_bucket`, `extract_queue_url`, `log_level`,
`aws_endpoint_url` (`AWS_ENDPOINT_URL`, empty in AWS, `http://localstack:4566` locally).
The env var *names* must stay in lockstep with `infra/stacks/config.py`; add a comment on both
sides saying so (separately deployed projects, cannot share an import).

`src/infrastructure/aws.py` — `client(service: str)` returning a boto3 client that passes
`endpoint_url=settings.aws_endpoint_url` when non-empty. Every later phase gets LocalStack support
for free.

`src/health/controllers.py`:
```python
router = APIRouter(tags=["health"])

@router.get("/health")
@router.get("/")
def health() -> dict:
    return {
        "status": "ok",
        "service": "bookloud-api",
        "environment": settings.environment,
        "commit": settings.git_sha,
    }
```
Returning the commit is what lets the smoke test prove the ephemeral stack is running *this PR's*
code rather than a stale deploy.

`src/main.py` — creates `app = FastAPI(title="Bookloud API")`, adds permissive `CORSMiddleware`,
includes `health.router`, registers a `botocore.exceptions.ClientError` handler that maps storage
failures to 503, and a `DomainError` handler mapping to 4xx.

**Lambda packaging** — container image, `backend/Dockerfile`, two targets:
- `FROM public.ecr.aws/lambda/python:3.12 AS lambda` (the default/final stage): copies
  `pyproject.toml`+`uv.lock`, runs `uv export --frozen --no-dev --no-emit-project` → `uv pip install
  --target ${LAMBDA_TASK_ROOT}`, copies source, `CMD ["lambda_function.handler"]`.
- `FROM python:3.12-slim AS dev`: same dependency install (plus dev group), `CMD ["uvicorn",
  "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]` — used by docker-compose so
  local dev is a plain HTTP server with hot reload rather than the RIE + proxy dance.
- Because CDK's `from_image_asset` builds the *last* stage by default, order the file so `lambda`
  is last, and have compose select `target: dev`.

`lambda_function.py` — `handler = Mangum(app)` over the same `app` object used by tests and local
dev. No forked Lambda-only app.

Tests: `test_health.py` (200, JSON shape, env/commit reflect `Settings`), `test_config.py`
(defaults and env overrides), `test_lambda_function.py` (invoke `handler` with a synthetic API
Gateway v2 event dict, assert 200 and body).

---

## 4. Frontend skeleton

Next.js 15 (App Router) + React 19 + TypeScript + Tailwind, Node 24. Test runner **vitest**
(jsdom + Testing Library); **Playwright** config scaffolded now, first real spec in phase 6.

`app/page.tsx` — a single server component: the Bookloud title, a one-line description, and a
client child (`components/HealthBadge.tsx`, or inline in `page.tsx`) that fetches
`${NEXT_PUBLIC_API_BASE_URL}/health` and renders `API: ok (<commit>)` or `API: unreachable`. This
gives the Phase 0 smoke test something real to assert on and visually proves frontend→backend
wiring in the PR environment.

`lib/api.ts` — `export const apiUrl = (path: string) =>
  `${(process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "")}${path}``.

`__tests__/page.test.tsx` — renders the page, asserts the heading; `__tests__/api.test.ts` — asserts
`apiUrl` trims trailing slashes and falls back to localhost.

### Packaging for Lambda + CloudFront: **OpenNext** (`open-next` v3, `npx open-next build`)

Justification:
- Next.js 15 App Router SSR cannot run on Lambda from `next build` output alone; OpenNext is the
  de-facto adapter that produces a Lambda-ready `index.handler` bundle plus a static asset tree.
- It is a *build CLI*, not a CDK construct, so `FrontendStack` stays plain `aws-cdk-lib` — no
  third-party construct dependency, no CDK-language lock-in, and full control over the CloudFront
  behaviours (needed in phase 7 for streaming chat responses).
- Alternatives rejected: `cdk-nextjs-standalone` (TS-only construct, wraps OpenNext anyway, less
  control); SST (whole-framework buy-in); static export (no SSR, contradicts the project spec);
  Amplify Hosting (separate deploy model, breaks the "one `cdk deploy` per env" story).

Build → deploy contract:
1. CI runs `npx open-next build` inside `frontend/` with `NEXT_PUBLIC_API_BASE_URL` set from the
   already-deployed `ApiStack` output.
2. Output must contain `frontend/.open-next/server-functions/default/` — CI asserts this explicitly
   and fails loudly if missing (silent fallback to the placeholder page is the worst failure mode).
3. `cdk deploy BookloudFrontend-{env} --exclusively` after `rm -rf infra/cdk.out`, forcing a fresh
   synth that picks up the real bundle.

CloudFront layout (two behaviours):
- `/_next/static/*` → S3 origin via **Origin Access Control** (bucket stays private),
  `CACHING_OPTIMIZED`.
- default (everything else) → Lambda **Function URL** origin (`AuthType.NONE`),
  `CACHING_DISABLED`, `ALLOW_ALL` methods, `ALL_VIEWER_EXCEPT_HOST_HEADER` origin request policy.
- A CloudFront Function on viewer-request copies `Host` into `x-forwarded-host`, because Function
  URLs reject a forwarded `Host` that isn't their own domain and the SSR layer still needs to know
  the public domain to build absolute URLs.
- Static assets uploaded with `s3deploy.BucketDeployment` from `.open-next/assets`, guarded by
  `os.path.isdir` so synth works without a build.

`frontend/Dockerfile` (compose only, not the Lambda path): multi-stage `node:24-slim`, `npm ci` →
`npm run build` → runtime stage running `npm run start` on :3000, with
`ARG/ENV NEXT_PUBLIC_API_BASE_URL`.

---

## 5. `docker-compose.yml`

LocalStack (community edition) covers everything Phase 0-5 touches: S3, SQS, DynamoDB. Cognito is
LocalStack **Pro** only — Phase 1 will need to add `jagregory/cognito-local` alongside it (noted in
§10, Q12). Phase 0 needs no local auth.

Services:

| service      | image / build                       | host port | notes |
|--------------|--------------------------------------|-----------|-------|
| `localstack` | `localstack/localstack:latest`       | 4566      | `SERVICES=s3,sqs,dynamodb`, `DEBUG=0`, `AWS_DEFAULT_REGION=us-east-1`, healthcheck `curl -sf http://localhost:4566/_localstack/health` |
| `backend`    | `build: ./backend`, `target: dev`    | 8000      | `depends_on: localstack (service_healthy)`; env below; bind-mounts `./backend:/app` for hot reload |
| `frontend`   | `build: ./frontend`                  | 3000      | `profiles: ["ui"]`, build arg `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`, `depends_on: backend` |

`backend` environment:
```
ENVIRONMENT=local
GIT_SHA=local
LOG_LEVEL=DEBUG
AWS_ENDPOINT_URL=http://localstack:4566
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=local
AWS_SECRET_ACCESS_KEY=local
TABLE_NAME=bookloud-local
PDF_BUCKET=bookloud-local-pdfs
AUDIO_BUCKET=bookloud-local-audio
MARKS_BUCKET=bookloud-local-marks
EXTRACT_QUEUE_URL=http://localstack:4566/000000000000/bookloud-local-extract
```

`local/setup.sh` (idempotent, run by `make up` after compose is healthy): polls
`http://localhost:4566/_localstack/health` until `s3`/`sqs`/`dynamodb` report `available`, then uses
a one-shot `amazon/aws-cli` container (or `docker compose exec localstack awslocal`) to create the
DynamoDB table (PK/SK), the three buckets, and the two queues + DLQs with the names above, then
polls `http://localhost:8000/health` until it answers.

`Makefile` targets: `up` (compose up core + `local/setup.sh`), `ui` (compose `--profile ui up`),
`seed` (re-run setup.sh), `smoke` (`python3 local/smoke_test.py --api-url http://localhost:8000`),
`logs`, `down` (`--profile ui down -v --remove-orphans`), `test` (backend pytest + frontend vitest +
infra pytest), `synth` (`cd infra && cdk synth -c environment=dev`),
`e2e` (`up` → `ui` → `smoke` with both URLs → `down`).

---

## 6. CI workflow

### `.github/workflows/ci.yml` — reusable (`on: workflow_call`)

Four parallel jobs, all `runs-on: ubuntu-latest`, none needing AWS credentials:

1. **`test-backend`**: checkout → `actions/setup-python@v6` (3.12) → `astral-sh/setup-uv` (cache
   key `backend/uv.lock`) → `uv sync --group dev` (wd `backend`) → `uv run pytest --tb=short`.
2. **`test-frontend`**: checkout → `actions/setup-node@v7` (node 24, npm cache on
   `frontend/package-lock.json`) → `npm ci` → `npx tsc --noEmit` → `npm test` (vitest run).
3. **`test-infra`**: checkout → setup-python 3.12 → pip cache → `pip install -r infra/requirements.txt
   -r infra/requirements-dev.txt` → `pytest infra/tests` → `npm i -g aws-cdk` →
   `cdk synth -c environment=pr-0 -q` (wd `infra`) as a belt-and-braces synth check.
4. **`local-smoke`**: checkout → `make up` → `make smoke` → `docker compose logs` on failure →
   `make down` (`if: always()`). Proves the compose stack is not bit-rotted, on every PR, for free.

### `.github/workflows/deploy-pr.yml` — ephemeral environment

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]
    branches: [main]
permissions: { contents: read, pull-requests: write, id-token: write }
concurrency:
  group: pr-env-${{ github.event.pull_request.number }}
  cancel-in-progress: false      # queue, never race, cdk deploy against one stack
```

Job `ci`: `uses: ./.github/workflows/ci.yml`.

Job `deploy` (`needs: ci`, `environment: { name: development, url: <frontend url> }`,
`env: ENV_NAME: pr-${{ github.event.pull_request.number }}`):

1. `actions/checkout@v7`
2. `aws-actions/configure-aws-credentials@v6` with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
   `AWS_REGION` secrets (see §10, Q3 for the OIDC alternative).
3. `actions/setup-python@v6` (3.12) and `actions/setup-node@v7` (24, npm cache).
4. `npm install -g aws-cdk`; `pip install -r infra/requirements.txt` (with `actions/cache` on
   `~/.cache/pip` keyed by `hashFiles('infra/requirements.txt')`).
5. `cdk bootstrap aws://${{ secrets.AWS_ACCOUNT_ID }}/${{ secrets.AWS_REGION }}` (idempotent).
6. **Deploy the non-frontend stacks** (wd `infra`), id `deploy-backend`:
   ```
   cdk deploy "BookloudStorage-${ENV_NAME}" "BookloudAuth-${ENV_NAME}" \
              "BookloudPipeline-${ENV_NAME}" "BookloudApi-${ENV_NAME}" \
     --exclusively --require-approval never \
     -c environment=${ENV_NAME} -c git_sha=${GITHUB_SHA} \
     --outputs-file cdk-backend-outputs.json
   ```
   Then a small inline `python3` heredoc parses the outputs file and appends
   `api_url=`, `user_pool_id=`, `user_pool_client_id=` to `$GITHUB_OUTPUT`.
   `--exclusively` is load-bearing: without it, naming `BookloudApi-*` would drag its dependencies
   in with whatever context the *next* command supplies.
7. `npm ci` (wd `frontend`).
8. `npx open-next build` (wd `frontend`) with `NEXT_PUBLIC_API_BASE_URL: ${{ steps.deploy-backend.outputs.api_url }}`.
9. **Verify OpenNext output**: fail with a directory listing if
   `.open-next/server-functions/default` is missing.
10. `rm -rf cdk.out` then
    `cdk deploy "BookloudFrontend-${ENV_NAME}" --exclusively --require-approval never
    -c environment=${ENV_NAME} -c git_sha=${GITHUB_SHA} --outputs-file cdk-frontend-outputs.json`;
    parse `FrontendUrl` into `steps.frontend-url.outputs.url`.
11. **Smoke test** (see §8):
    `python3 local/smoke_test.py --api-url "$API_URL" --frontend-url "$FRONTEND_URL" --expect-commit "$GITHUB_SHA" --expect-environment "$ENV_NAME"`
12. `actions/github-script@v9`: upsert a single PR comment (find the existing bot comment
    containing the marker string and update it rather than spamming) with a table of the frontend
    URL, API URL, env name, and deployed SHA.

The stack is **left standing** after a successful run — that is the point of the ephemeral env.

### `.github/workflows/destroy-pr.yml` — teardown

```yaml
on:
  pull_request:
    types: [closed]        # fires for both merged and closed-without-merge
    branches: [main]
permissions: { contents: read, pull-requests: write }
concurrency:
  group: pr-env-${{ github.event.pull_request.number }}   # same group as deploy: queue behind it
  cancel-in-progress: false
```
Job `destroy` (`environment: development`, `env: ENV_NAME: pr-<N>`):
1. `configure-aws-credentials`.
2. Delete stacks with plain CloudFormation calls — no checkout, no synth, no frontend build needed,
   because stack names are deterministic. **Reverse dependency order**:
   `BookloudFrontend-` → `BookloudApi-` → `BookloudPipeline-` → `BookloudAuth-` → `BookloudStorage-`.
   For each: `aws cloudformation describe-stacks` guard (skip if absent) →
   `delete-stack` → `wait stack-delete-complete`. `set -euo pipefail`.
   Budget ~15-25 min wall clock; CloudFront distribution deletion dominates. Set
   `timeout-minutes: 60` on the job.
3. Comment on the PR that `pr-<N>` was destroyed.

---

## 7. CD workflow

### `.github/workflows/deploy-prod.yml`

```yaml
on:
  push:
    branches: [main]
permissions: { contents: read, id-token: write }
concurrency:
  group: deploy-prod
  cancel-in-progress: false   # never kill an in-flight prod deploy: half-applied stacks
```

Job `ci`: `uses: ./.github/workflows/ci.yml`.

Job `deploy` (`needs: ci`, `environment: { name: production, url: <frontend url> }`,
`env: ENV_NAME: prod`): byte-for-byte the same step sequence as `deploy-pr.yml` steps 1-11, with
`ENV_NAME=prod`, minus the PR-comment step. No Playwright e2e — per IMPLEMENTATION_PLAN.md there is
no UI flow to test until phase 8; the smoke test is the gate.

Notes to the implementer:
- Keep the two deploy workflows structurally identical so phase 8 can factor them into one reusable
  `deploy.yml` with an `environment_name` input without a rewrite. Do **not** factor it now — the
  duplication is deliberate and cheap at this size.
- `environment: production` in GitHub lets the human add a required-reviewer gate later without a
  code change.

---

## 8. The smoke test

**Location**: `local/smoke_test.py`. Python 3, **standard library only** (`urllib`, `json`,
`argparse`, `time`) so it runs from a clean checkout, inside CI, with no install step.

**Interface**:
```
python3 local/smoke_test.py \
  [--api-url URL]            # default http://localhost:8000
  [--frontend-url URL]       # optional; skipped if omitted
  [--expect-commit SHA]      # optional
  [--expect-environment ENV] # optional
  [--timeout SECONDS]        # default 180
```

**What it does**:
1. `GET {api-url}/health`, retrying on connection error / non-2xx with 5s backoff up to
   `--timeout` (a freshly deployed API Gateway + a cold container Lambda can take a while).
   Asserts: status 200; JSON body has `status == "ok"` and `service == "bookloud-api"`;
   `environment == --expect-environment` when given; `commit == --expect-commit` when given.
   The commit assertion is the one that proves the deploy actually shipped *this* code.
2. If `--frontend-url` given: `GET {frontend-url}/` with the same retry loop (CloudFront needs a
   minute to propagate on first create). Asserts 200 and that the body contains `Bookloud` and does
   **not** contain the placeholder string `is deploying` — that catches the "OpenNext bundle missing,
   CDK silently used the inline placeholder" failure.
3. Prints one line per check (`OK  GET .../health -> 200 (commit abc1234)`), exits 0 on success and
   1 with the offending response body and status on failure.

**Invoked from**:
- `make smoke` → local compose stack (API only, or both with `make e2e`).
- `ci.yml` job `local-smoke` → against compose.
- `deploy-pr.yml` step 11 → against the ephemeral deploy, with `--expect-commit ${{ github.sha }}`
  and `--expect-environment pr-<N>`.
- `deploy-prod.yml` → against prod, with `--expect-environment prod`.

One script, four call sites: exactly the "prove the loop" job Phase 0 exists to do.

---

## 9. Concrete file list

**Root**
- `docker-compose.yml` — LocalStack + backend (dev target) + frontend (`ui` profile).
- `Makefile` — `up ui seed smoke logs down test synth e2e`.
- `.env.example` — documents `AWS_REGION`, `ENVIRONMENT`, `NEXT_PUBLIC_API_BASE_URL` for local use.
- `.gitignore` — Python + Node + CDK (`cdk.out/`, `.open-next/`, `*.outputs.json`, `.venv/`,
  `node_modules/`, `.next/`, `.pytest_cache/`, `.coverage`).
- `README.md` (update) — what Bookloud is, repo layout, `make up` quickstart, deploy/env model.
- `IMPLEMENTATION_PLAN.md` (update) — tick Phase 0 in the checklist at the end of the phase.
- `PLANS/phase-0.md` — this plan.

**`.github/`**
- `workflows/ci.yml` — reusable unit-test + synth + local-smoke workflow.
- `workflows/deploy-pr.yml` — per-PR ephemeral deploy + smoke + PR comment.
- `workflows/destroy-pr.yml` — per-PR teardown in reverse dependency order.
- `workflows/deploy-prod.yml` — main-branch tests + prod deploy + smoke.
- `CODEOWNERS` — repo owner on everything.

**`backend/`**
- `Dockerfile` — multi-target image: `dev` (uvicorn) and `lambda` (RIE base, default).
- `.dockerignore` — excludes `tests/`, `.venv/`, `__pycache__/`, `.pytest_cache/`.
- `pyproject.toml` — deps, dev group, pytest/coverage config.
- `uv.lock` — locked dependency graph (generated, committed).
- `lambda_function.py` — `handler = Mangum(src.main:app)`.
- `src/main.py` — FastAPI app, CORS, router registration, exception handlers.
- `src/config.py` — pydantic-settings `Settings`; single source of env var names.
- `src/health/controllers.py` — `GET /health` and `GET /` returning status/env/commit.
- `src/health/__init__.py` — package marker.
- `src/shared_kernel/domain/errors.py` — `DomainError`, `NotFoundError`, `ConflictError`.
- `src/shared_kernel/application/ports.py` — `Clock`, `IdGenerator` protocols.
- `src/infrastructure/aws.py` — boto3 client factory honouring `AWS_ENDPOINT_URL` (LocalStack).
- `src/contexts/.gitkeep` — reserved for `library/`, `reading/`, `chat/` in later phases.
- `tests/conftest.py` — `TestClient` fixture, env var isolation fixture.
- `tests/test_health.py` — health route shape and values.
- `tests/test_config.py` — settings defaults and env overrides.
- `tests/test_lambda_function.py` — Mangum handler against a synthetic API Gateway v2 event.

**`frontend/`**
- `package.json` — scripts `dev build start lint test test:e2e`; deps next/react/tailwind; devDeps
  vitest, testing-library, playwright, open-next, typescript.
- `package-lock.json` — lockfile (generated, committed; required by `npm ci` in CI).
- `next.config.mjs` — `reactStrictMode: true`.
- `tsconfig.json`, `next-env.d.ts` — TS config with `@/*` path alias.
- `postcss.config.mjs`, `tailwind.config.ts`, `app/globals.css` — Tailwind wiring.
- `app/layout.tsx` — root layout, metadata title "Bookloud".
- `app/page.tsx` — landing page + API health badge (client fetch of `/health`).
- `lib/api.ts` — `apiUrl()` helper over `NEXT_PUBLIC_API_BASE_URL`.
- `vitest.config.ts`, `vitest.setup.ts` — jsdom env, excludes `e2e/**`.
- `playwright.config.ts` — `testDir: ./e2e`, `baseURL` from `E2E_BASE_URL`; no specs yet.
- `__tests__/page.test.tsx`, `__tests__/api.test.ts` — the two Phase 0 frontend unit tests.
- `Dockerfile`, `.dockerignore` — compose-only production-equivalent Next.js image.
- `.env.example`, `.gitignore` — local env template; ignores `.next/`, `.open-next/`.
- `e2e/.gitkeep`, `public/.gitkeep` — reserved dirs.

**`infra/`**
- `app.py` — CDK app: reads context, instantiates and wires the five stacks.
- `cdk.json` — `"app": "python app.py"` + feature flags.
- `requirements.txt` — `aws-cdk-lib>=2.225.0`, `constructs>=10.4.2`.
- `requirements-dev.txt` — `pytest>=8.3`.
- `.gitignore` — `cdk.out/`, `*.outputs.json`, `__pycache__/`.
- `stacks/config.py` — `Config` constants, `table_name()`, `queue_name()`, env var name constants.
- `stacks/storage_stack.py` — DynamoDB single table + 3 S3 buckets.
- `stacks/auth_stack.py` — Cognito pool (username sign-in, immutable) + web client.
- `stacks/api_stack.py` — Docker image Lambda + HTTP API + routes + grants.
- `stacks/pipeline_stack.py` — extract/synthesize SQS queues + DLQs.
- `stacks/frontend_stack.py` — OpenNext Lambda + Function URL + private S3 + CloudFront + OAC.
- `stacks/__init__.py` — package marker.
- `tests/test_synth.py` — `aws_cdk.assertions` coverage of all five stacks, prod and pr envs.

**`local/`**
- `setup.sh` — wait for LocalStack health, create table/buckets/queues, wait for backend `/health`.
- `smoke_test.py` — the stdlib smoke test described in §8.

---

## 10. Open questions / assumptions — human decisions needed before implementation

**Q1 — CDK language (blocking). DECIDED: Python CDK.**

**Q1-decision:** Human confirmed Python CDK, as recommended.

**Q2 — AWS account(s) and region (blocking).** Assumed: **one** AWS account hosting both prod and
all `pr-*` environments, distinguished purely by stack/resource name. Need: account ID, and region.
Recommendation: **`us-east-1`** — CloudFront ACM certificates must live there if a custom domain is
ever added. Decide also whether PR envs should go to a *separate* sandbox account (cleaner blast
radius, more setup: two bootstrap targets, two credential sets).

**Q3 — GitHub → AWS authentication. DECIDED: long-lived access keys.** Human will create an IAM
user (in a "Projects" group) and add its keys as GitHub secrets. Workflows use
`aws-actions/configure-aws-credentials` with `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/
`AWS_REGION`/`AWS_ACCOUNT_ID` secrets, no OIDC. **Implementer note:** this IAM user + secrets setup
requires the human's AWS login — do not attempt it; flag clearly when the implementation reaches
the point where the deploy workflows need those secrets to actually run (code/workflow files can
still be written and unit-tested without them).

**Q4 — Required secrets/variables.** With the assumption in Q3, the repo needs exactly:
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_ACCOUNT_ID`. Also create the two
GitHub *Environments* `development` and `production` (the workflows reference them for the
deployment URL badge and for optional approval gates). No app secrets exist yet — Google Cloud TTS
credentials (phase 4) and the LLM API key (phase 7) will be added then.

**Q5 — Create the Cognito pool in Phase 0? DECIDED: yes**, real pool with username sign-in, as
recommended.

**Q6 — Stack name prefix.** IMPLEMENTATION_PLAN.md says `pdf-reader-pr-42-*`; the repo is
`bookloud`. This plan uses `Bookloud{Auth,Storage,Api,Pipeline,Frontend}-pr-42`. Confirm the
prefix now — renaming stacks after the first prod deploy means a full recreate.

**Q7 — Scope: how "near-empty" should the stacks be? DECIDED: full resources now** (table, buckets,
queues), as recommended.

**Q8 — Per-PR cost and quota.** Each PR environment creates a CloudFront distribution, a Cognito
user pool, a DynamoDB table, 3 S3 buckets, 4 SQS queues, 2 Lambdas, an ECR image, and an HTTP API.
Default quotas that bite: CloudFront distributions per account (200 — fine), Cognito user pools per
account (1,000 — fine), and **ECR image storage** (accumulates per PR deploy; add a lifecycle policy
later). Teardown takes ~15-25 min, dominated by CloudFront. Accept, or restrict the PR environment
to backend stacks only (skip `FrontendStack` on PRs) for speed?

**Q9 — Orphaned PR stacks.** If `destroy-pr.yml` fails or a PR is deleted without closing, stacks
linger and bill. Recommend a scheduled sweeper workflow (nightly: list stacks matching
`Bookloud*-pr-*`, cross-reference open PRs, delete the rest). Deferred out of Phase 0 unless wanted
now.

**Q10 — Test strictness.** Assumed `--cov-fail-under=90` on the backend. 100% is trivially
achievable at Phase 0 size but gets painful around the TTS/pipeline phases. Confirm 90, or set a
different number.

**Q11 — Toolchain versions.** Assumed Python 3.12 (backend + CDK + Lambda runtime), Node 24
(frontend + OpenNext Lambda runtime), Next 15 / React 19, uv for Python deps, npm for Node.
Flag any of these to pin differently.

**Q12 — LocalStack edition.** Community edition covers S3/SQS/DynamoDB, which is all Phase 0-5
needs. **Cognito is LocalStack Pro only** — Phase 1's local auth will therefore need
`jagregory/cognito-local` added as a sixth compose service, or a Pro licence. No decision needed
now, but budget for it in Phase 1.

**Q13 — Branch protection.** The workflows assume PRs target `main` and that `main` is protected
with `ci` as a required check. Set that up, or skip `CODEOWNERS`.
