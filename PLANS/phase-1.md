# Phase 1 — Auth (Cognito)

Goal: every non-public backend route requires a valid Cognito JWT; the frontend has working
signup/login/logout/session-refresh; local dev has a real (emulated) Cognito. The user pool itself
already exists from Phase 0 (`infra/stacks/auth_stack.py`, username-only sign-in) — this phase is
almost entirely application code plus one authorizer wiring.

Reference implementations (patterns copied deliberately):
- `/home/yngvarr/Projects/cashlytics` — **primary reference**: `HttpUserPoolAuthorizer` wiring
  (`infra/stacks/backend_stack.py` §"HTTP API with Cognito JWT authorizer"), claims-from-`aws.event`
  FastAPI dependency (`backend/src/auth/services.py`), `WithGatewayClaims` ASGI test wrapper
  (`backend/tests/conftest.py`), `jagregory/cognito-local` compose service + bootstrap script
  (`docker-compose.yml`, `backend/src/auth/bootstrap.py`), cookie/refresh frontend auth
  (`frontend/lib/auth.ts`, `frontend/middleware.ts`).
- What is **different** here: cashlytics's pool signs in with **email alias** and uses
  **admin-created users only** (no self-signup, `CfnUserPoolUser` in CDK) with a
  `NEW_PASSWORD_REQUIRED` first-login challenge, and an `admin` **group** for RBAC. Bookloud's pool
  is **username-only**, `self_sign_up_enabled=True`, no groups, no admin-provisioned users — so we
  need a real `SignUp` flow (cashlytics has none) and we do *not* port `groups_from_claims` /
  `require_admin`. Cashlytics also runs its local backend under RIE + a hand-rolled API Gateway
  proxy (`local/apigw-proxy/proxy.py`); bookloud's Phase 0 deliberately runs plain uvicorn with hot
  reload, so we emulate the authorizer with an ASGI middleware instead of a proxy container (§4.3).

---

## 1. Auth architecture decision

### 1.1 The three options

| # | Shape | Verdict |
|---|-------|---------|
| A | Browser → Cognito directly (Amplify Auth or `@aws-sdk/client-cognito-identity-provider` or raw JSON API), tokens held in JS/cookies | Rejected (see below) |
| B | Browser → **Next.js route handlers** (`/api/auth/*`) → Cognito; refresh token in an httpOnly cookie, id token returned to JS and held in memory | **Recommended** |
| C | Browser → FastAPI `/auth/*` endpoints → Cognito | Rejected |

**C is rejected** because the FastAPI app sits behind the very JWT authorizer we are adding: login
routes would have to be punched back out as explicit public routes on the HTTP API (`/auth/login`,
`/auth/signup`, `/auth/refresh` each needing their own `HttpNoneAuthorizer` route + a matching
public-path list duplicated in three places), the API Lambda would need `cognito-idp` IAM grants,
and it buys nothing: the browser still ends up holding a token. It also adds a cold-start-prone
container Lambda hop to every login.

**A is rejected** in its pure form because the refresh token (30-day credential) would have to live
somewhere JS can read it — `localStorage` or a non-httpOnly cookie — which is exactly the XSS
exposure worth avoiding. Sub-variants: **Amplify Auth** additionally drags in a large dependency
with its own config/bootstrapping layer and defaults to `localStorage`; **`@aws-sdk/client-cognito-idp`**
is a reasonable typed client but ~100 KB in the browser bundle for three API calls.

**B is recommended.** The Next.js SSR Lambda already exists (Phase 0's `FrontendStack`), so a
backend-for-frontend costs zero new infrastructure. Concretely it gives:

- **Refresh token never reaches JS.** It is set as an `httpOnly; SameSite=Lax` cookie by the route
  handler. An XSS bug can steal at most a ~1 h id token, not a 30-day session.
- **Middleware route-gating works.** `middleware.ts` reads the httpOnly cookie server-side — no
  duplicate "session marker" cookie needed.
- **No full API proxy.** Only login/signup/refresh/logout (rare) go through Next; ordinary API
  calls stay browser → API Gateway with an `Authorization: Bearer` header. This matters for
  **phase 7** (Lambda Function URL response streaming — proxying a stream through a Next route
  handler is doable but adds real complexity) and keeps **phase 3**'s presigned-POST flow a single
  hop (browser → backend for the presign, browser → S3 for the bytes).
- **Cognito config stops being build-time.** Because only the server reads `COGNITO_CLIENT_ID` /
  `COGNITO_REGION` / `COGNITO_ENDPOINT`, they are plain **runtime** env vars on the SSR Lambda —
  no `NEXT_PUBLIC_*` values baked into the bundle, so the CI ordering headache cashlytics has
  (deploy auth → read outputs → rebuild frontend) does not apply, and local dev does not need the
  `NEXT_PUBLIC_COGNITO_ENDPOINT=http://localhost:9229` hack cashlytics needs.
- **No SDK dependency.** The route handlers call Cognito's plain JSON API over `fetch`
  (`X-Amz-Target: AWSCognitoIdentityProviderService.<Action>`), copied from cashlytics's
  `frontend/lib/auth.ts` — the app client has no secret, so no SigV4 signing is required.

Cost/trade-off to accept: the id token lives in a JS module variable, so a full page reload costs
one `POST /api/auth/refresh` (~150-300 ms) before the first API call; multiple tabs each refresh
independently (fine — Cognito refresh tokens are reusable and not rotated by default).

### 1.2 Which token is sent to the API

**The id token**, not the access token. CDK's `HttpUserPoolAuthorizer` sets
`jwtConfiguration.audience = [clientId]`, which matches the id token's `aud` claim; Cognito access
tokens carry `client_id` instead of `aud` and are rejected. Consequence for the backend: the
username claim is **`cognito:username`** (an access token would use `username`). The dependency
reads `cognito:username` with a `username` fallback so it keeps working if this ever changes.

`sub` is the canonical user id: **phase 2's `USER#<id>` partition key must use `sub`**, not the
username.

---

## 2. Infra changes (CDK)

### 2.1 `infra/stacks/api_stack.py` — the JWT authorizer (the Phase 0 deferral)

Add imports and two constructor kwargs, then attach the authorizer to the catch-all only:

```python
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_cognito as cognito

# new kwargs: user_pool: cognito.IUserPool, user_pool_client: cognito.IUserPoolClient

authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
    "CognitoAuthorizer", user_pool, user_pool_clients=[user_pool_client],
)

for path in public_paths:                       # ["/health", "/", "/docs", "/redoc", "/openapi.json"]
    http_api.add_routes(path=path, methods=route_methods, integration=integration,
                        authorizer=apigwv2.HttpNoneAuthorizer())

http_api.add_routes(path="/{proxy+}", methods=route_methods, integration=integration,
                    authorizer=authorizer)
```

Notes for the implementer:
- No `default_authorizer` on the `HttpApi` — each route states its own, exactly as cashlytics does.
  `HttpNoneAuthorizer()` on the public routes is **required** (not decorative): once any authorizer
  exists on the API, being explicit is what keeps `/health` reachable by the smoke test.
- The existing `route_methods` list (GET/HEAD/POST/PUT/DELETE, **no OPTIONS**) stays as is — that is
  what lets CORS preflights bypass the authorizer. Do not add OPTIONS.
- Do **not** add `COGNITO_*` env vars to the API Lambda: the backend never validates tokens itself
  and never calls `cognito-idp` in this phase.
- Update the class docstring: the "Phase 1 adds the authorizer" note is now done.

### 2.2 `infra/stacks/auth_stack.py` — PreSignUp auto-confirm trigger

Problem: the pool is username-only with `email` optional and **no auto-verified attributes**, so
Cognito has no channel to deliver a confirmation code. A self-signed-up user can therefore land in
`UNCONFIRMED` with no way out, and `InitiateAuth` fails with `UserNotConfirmedException`.

Fix (deterministic, no email dependency): a tiny PreSignUp trigger.

```python
pre_signup_fn = lambda_.Function(
    self, "PreSignUpAutoConfirm",
    runtime=lambda_.Runtime.NODEJS_22_X,
    handler="index.handler",
    timeout=cdk.Duration.seconds(5),
    code=lambda_.Code.from_inline(
        "exports.handler = async (event) => {"
        "  if (event.triggerSource === 'PreSignUp_SignUp') {"
        "    event.response.autoConfirmUser = true;"
        "  }"
        "  return event;"
        "};"
    ),
)
# on the UserPool(...) call:
lambda_triggers=cognito.UserPoolTriggers(pre_sign_up=pre_signup_fn),
```

- Adding `LambdaConfig` to an existing pool is an **in-place CloudFormation update**, not a
  replacement — sign-in aliases are untouched. The implementer must still run `cdk diff` against a
  PR env and confirm no `AWS::Cognito::UserPool` replacement before this reaches prod.
- Guarding on `triggerSource === 'PreSignUp_SignUp'` means admin-created users
  (`PreSignUp_AdminCreateUser`) are unaffected.
- Alternative if the human rejects the extra Lambda: set
  `auto_verify=cognito.AutoVerifiedAttrs(email=True)` and make email required at signup, using
  Cognito's default email sender (50/day) for the confirmation code. That is a mutable pool
  property, so it is still a Phase 1 decision, not a Phase 0 mistake. See §10 Q2.
- The frontend still implements a `confirm_required` branch (§5.4) so the flow is correct if a
  future pool config does require confirmation, and so local `cognito-local` signup works whatever
  its default behaviour turns out to be.

### 2.3 `infra/stacks/frontend_stack.py` — Cognito config for the SSR Lambda

New kwargs `cognito_client_id: str`, `cognito_region: str`; add to the server Lambda's `environment`
dict alongside the existing `Config.ENV_API_BASE_URL`:

```python
Config.ENV_COGNITO_CLIENT_ID: cognito_client_id,
Config.ENV_COGNITO_REGION: cognito_region,   # pass self.region from app.py
```

No `COGNITO_ENDPOINT` in AWS (empty/unset ⇒ `lib/cognito.ts` derives
`https://cognito-idp.<region>.amazonaws.com`). These are **not** `NEXT_PUBLIC_*` — deliberately
server-only, so no frontend rebuild is coupled to the auth stack.

### 2.4 `infra/stacks/config.py`

Add, next to the existing `ENV_API_BASE_URL` (keep the "must stay in lockstep" comment, now also
pointing at `frontend/lib/cognito.ts`):

```python
ENV_COGNITO_CLIENT_ID = "COGNITO_CLIENT_ID"
ENV_COGNITO_REGION    = "COGNITO_REGION"
ENV_COGNITO_ENDPOINT  = "COGNITO_ENDPOINT"   # local dev only (cognito-local); unset in AWS
```

### 2.5 `infra/app.py`

- `ApiStack(..., user_pool=auth.user_pool, user_pool_client=auth.user_pool_client, ...)`
- `FrontendStack(..., cognito_client_id=auth.user_pool_client.user_pool_client_id,
  cognito_region=auth.region, ...)`
- Update the comment block: the dependency graph is now `Storage, Auth, Pipeline → Api → Frontend`,
  with `Auth → Frontend` as well. Teardown order in `destroy-pr.yml`
  (Frontend → Api → Pipeline → Auth → Storage) is already correct for this and needs no change.

### 2.6 `infra/tests/test_synth.py` additions

- `AuthStack`: keep the existing username-only assertion; add
  (a) `LambdaConfig.PreSignUp` present on the pool, (b) exactly 1 `AWS::Lambda::Function` in the
  stack, (c) `ExplicitAuthFlows` on the `AWS::Cognito::UserPoolClient` contains
  `ALLOW_USER_PASSWORD_AUTH` **and** `ALLOW_REFRESH_TOKEN_AUTH` (CDK appends the latter
  automatically — assert it so a CDK upgrade can't silently break the refresh flow).
- `ApiStack` (existing `@pytest.mark.docker` test; CI's `test-infra` job runs Docker-marked tests
  since it invokes plain `pytest tests`): assert 1 `AWS::ApiGatewayV2::Authorizer` of
  `AuthorizerType: JWT`; the `/{proxy+}` route has `AuthorizationType: JWT` and an `AuthorizerId`;
  the `/health` route has `AuthorizationType: NONE`. This is the regression test for the whole
  phase — it is what proves "protected route rejection without token" at the infra layer.
- `FrontendStack`: server Lambda `Environment.Variables` contains `COGNITO_CLIENT_ID`.

---

## 3. Backend changes

New package `backend/src/auth/` (top-level like `health/`, not under `contexts/` — auth is
cross-cutting, has no persisted entity, and mirrors cashlytics's `src/auth/`).

### 3.1 `src/auth/dependencies.py` — the `get_current_user` dependency

```python
_bearer_scheme = HTTPBearer(auto_error=False)   # OpenAPI "Authorize" button only; never rejects

@dataclass(frozen=True)
class CurrentUser:
    sub: str          # canonical user id -> phase 2's USER#<id> partition key
    username: str
    email: str | None
    claims: dict

def claims_from_request(request: Request) -> dict | None:
    event = request.scope.get("aws.event") or {}
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    return (authorizer.get("jwt") or {}).get("claims")

def get_current_user(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    claims = claims_from_request(request)
    if not claims:
        raise HTTPException(401, "Not authenticated")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(401, "Invalid token claims")
    return CurrentUser(
        sub=sub,
        username=claims.get("cognito:username") or claims.get("username") or sub,
        email=claims.get("email"),
        claims=claims,
    )
```

Rationale to carry into the module docstring (adapted from cashlytics): **the backend never
validates a token.** API Gateway's JWT authorizer does that before the Lambda is invoked and
forwards the verified claims in `requestContext.authorizer.jwt.claims`; Mangum surfaces the whole
Lambda event as `request.scope["aws.event"]`. `_bearer_scheme` exists purely so `/docs` renders the
Authorize button; `auto_error=False` so it can never itself 401.

The 401 raised here is a **defence-in-depth** path — in a correctly deployed environment API Gateway
already returned 401 and the Lambda was never invoked. It is the real enforcement point only in
local dev.

### 3.2 `src/auth/controllers.py` — `GET /me`

```python
router = APIRouter(tags=["auth"])

@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"sub": user.sub, "username": user.username, "email": user.email}
```

`/me` is the phase's concrete protected route: it is what the frontend calls to confirm a session,
what the unit tests assert 401/200 on, and what `local/smoke_test.py` hits to prove the deployed
authorizer rejects anonymous requests.

### 3.3 `src/auth/local_dev.py` — authorizer emulation for local dev

Locally the backend is plain uvicorn (Phase 0 chose the `dev` Dockerfile target with hot reload,
deliberately *not* cashlytics's RIE + `apigw-proxy` pair), so there is no API Gateway and no
`aws.event`. Without something, every protected route 401s locally from Phase 3 onward.

Pure-ASGI middleware, **mounted only when `settings.environment == "local"`**:

```python
class LocalAuthMiddleware:
    """Decode (WITHOUT verifying) a Bearer token and inject the claims into
    scope["aws.event"], exactly as API Gateway's JWT authorizer does in AWS.
    Local dev only — see should_enable(). Signature verification is API
    Gateway's job in AWS; here the token comes from cognito-local, whose
    signatures are fake anyway."""
```

- `should_enable(environment: str) -> bool: return environment == "local"` — a named, unit-tested
  function. `"local"` is a value the Lambda **never** has (`ApiStack` always sets
  `ENVIRONMENT=dev|pr-N|prod`), which is the safety property.
- Behaviour: no/!Bearer header → pass through untouched (so `/health` still works and `/me` still
  401s); malformed base64/JSON → pass through untouched; `exp` in the past → pass through untouched;
  otherwise inject `{"requestContext": {"authorizer": {"jwt": {"claims": payload}}}}`.
- Decoding is stdlib `base64.urlsafe_b64decode` + `json.loads` on segment 1 of the JWT — copy the
  logic from cashlytics's `local/apigw-proxy/proxy.py::_decode_claims` (minus the `[a b]` list
  flattening, which only mattered for `cognito:groups`; bookloud has no groups).

### 3.4 `src/main.py`

```python
from src.auth.controllers import router as auth_router
from src.auth.local_dev import LocalAuthMiddleware, should_enable

app.include_router(auth_router)
if should_enable(settings.environment):
    app.add_middleware(LocalAuthMiddleware)
```

### 3.5 Backend tests

`backend/tests/conftest.py` — add, ported from cashlytics:

```python
CLAIMS = {"sub": "11111111-2222-3333-4444-555555555555",
          "cognito:username": "reader", "email": "reader@example.com"}

class WithGatewayClaims:           # ASGI wrapper: what API Gateway + Mangum produce
    ...                            # injects scope["aws.event"] = {"requestContext": {...}}

@pytest.fixture
def authed_client(): ...           # TestClient(WithGatewayClaims(app, CLAIMS))
```
Keep the existing anonymous `client` fixture — it *is* the "no token" case.

New/changed test files:
- `tests/auth/test_dependencies.py` — `claims_from_request` for: no `aws.event`, empty
  `requestContext`, empty `authorizer`, populated claims. `get_current_user` for: no claims → 401,
  claims without `sub` → 401, id-token shape (`cognito:username`) → correct `CurrentUser`,
  access-token shape (`username`) → fallback works, missing username → falls back to `sub`.
- `tests/auth/test_local_dev.py` — `should_enable("local") is True`; `should_enable("prod")`,
  `"dev"`, `"pr-7"` all False. Middleware: valid unsigned JWT injects claims; expired `exp` does
  not; garbage token does not; absent header does not; non-http scope (`lifespan`) passes through.
  Build tokens in-test with `base64.urlsafe_b64encode(json.dumps(payload))` — no PyJWT dependency.
- `tests/auth/test_controllers.py` — `authed_client.get("/me")` → 200 with `sub`/`username`/`email`;
  `client.get("/me")` → 401 `{"detail": "Not authenticated"}`; `client.get("/health")` → still 200
  (public route unaffected).
- `tests/test_lambda_function.py` (extend) — reuse `_api_gateway_v2_event` with a new
  `claims=None` parameter that injects `requestContext.authorizer.jwt.claims`; assert
  `handler(event_with_claims)` → 200 on `/me` and `handler(event_without)` → 401. This is the Mangum
  harness check that the `scope["aws.event"]` plumbing survives the real adapter.
- Keep `--cov-fail-under=90`; the new code is trivially coverable.

---

## 4. Local dev / Cognito emulation

**DECIDED: `jagregory/cognito-local` as a compose service** (cashlytics already solved this
exact problem this way), *not* a real dev-tier Cognito pool. Reasons: `make up` / CI's `local-smoke`
job must work with zero AWS credentials (that is a Phase 0 invariant — all four `ci.yml` jobs run
credential-free); a shared real dev pool would make local tests order-dependent and account-bound;
LocalStack Pro is a paid licence for one service. This answers Phase 0's Q12.

### 4.1 `docker-compose.yml`

```yaml
  cognito-local:
    image: jagregory/cognito-local:latest
    ports: ["9229:9229"]
```
- `backend`: add `COGNITO_ENDPOINT_URL=http://cognito-local:9229` (used only by the bootstrap
  script, which runs inside this container because it already has boto3), add the bind mount
  `./local:/local-shared`, add `cognito-local` to `depends_on`.
- `frontend`: add **runtime** `environment:` (not build args — these are server-only):
  `COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID}`, `COGNITO_REGION=us-east-1`,
  `COGNITO_ENDPOINT=http://cognito-local:9229`; add `cognito-local` to `depends_on`.
  **No `frontend/Dockerfile` change is needed** — nothing new is baked at build time.

### 4.2 `local/cognito_bootstrap.py` (new)

Runs inside the backend container (`docker compose exec -T backend python /local-shared/cognito_bootstrap.py`),
boto3 against `COGNITO_ENDPOINT_URL`. Kept out of `backend/src/` on purpose: it is dev tooling, and
`src/` is coverage-gated. Idempotent, modelled on cashlytics's `src/auth/bootstrap.py`:

1. get-or-create user pool `bookloud-local` — **no `UsernameAttributes`/`AliasAttributes`**, so
   sign-in is username-only, matching the real pool; relaxed password policy (min 8, no classes),
   matching the non-prod CDK policy.
2. get-or-create app client `WebClient` with
   `ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]`, no secret.
3. get-or-create user `dev` via `admin_create_user(MessageAction="SUPPRESS")` +
   `admin_set_user_password(Password="devpassword", Permanent=True)` — a confirmed, known-good login
   for `make smoke` and manual dev.
4. write `/local-shared/.cognito.env`:
   `COGNITO_CLIENT_ID=…`, `COGNITO_USER_POOL_ID=…`, `COGNITO_REGION=us-east-1`.
5. print `Cognito ready: pool=… client=… dev user: dev / devpassword`.

### 4.3 `local/setup.sh`

Append after the existing LocalStack provisioning: retry the bootstrap invocation up to ~20× with a
2 s sleep (the retry *is* the readiness check — cognito-local's own health endpoint is not worth
depending on), then keep the existing "wait for backend `/health`" step.

### 4.4 `Makefile`

- `up`: add `cognito-local` to the `docker compose up` service list (setup.sh already handles the
  bootstrap).
- `ui`: `set -a; . local/.cognito.env; set +a; docker compose --profile ui up -d --build frontend`
  (cashlytics's exact trick).
- `smoke`: source `local/.cognito.env` and pass `--cognito-endpoint http://localhost:9229
  --cognito-client-id "$$COGNITO_CLIENT_ID" --login-username dev --login-password devpassword`.
- New `token` target: prints an id token for the `dev` user (stdlib `urllib` one-liner against
  cognito-local) so `curl -H "Authorization: Bearer $(make -s token)" localhost:8000/me` works.

### 4.5 `.gitignore` / env examples

- `.gitignore`: add `local/.cognito.env`.
- `.env.example`: add `COGNITO_ENDPOINT_URL=http://localhost:9229`, `COGNITO_REGION=us-east-1`, and
  a note that `make up` writes the generated ids to `local/.cognito.env`.
- `frontend/.env.example`: add `COGNITO_CLIENT_ID=`, `COGNITO_REGION=us-east-1`,
  `COGNITO_ENDPOINT=http://localhost:9229` with a comment that these are **server-side only**
  (deliberately not `NEXT_PUBLIC_*`).

---

## 5. Frontend changes

### 5.1 `lib/cognito.ts` (server-only)

Raw Cognito JSON API over `fetch` — no SDK, ported from cashlytics's `frontend/lib/auth.ts::cognito()`:

```ts
const CLIENT_ID = process.env.COGNITO_CLIENT_ID ?? "";
const REGION    = process.env.COGNITO_REGION ?? "us-east-1";
const ENDPOINT  = process.env.COGNITO_ENDPOINT || `https://cognito-idp.${REGION}.amazonaws.com`;

export class CognitoError extends Error { constructor(readonly code: string, message: string) {…} }

export async function cognito(target: string, body: unknown): Promise<any>  // X-Amz-Target header
export async function initiateAuthPassword(username, password)
export async function initiateAuthRefresh(refreshToken)
export async function signUp(username, password, email?)   // UserAttributes only when email given
export async function confirmSignUp(username, code)
export async function revokeToken(refreshToken)            // best-effort; may 400 on cognito-local
export function httpStatusForCognitoError(code: string): number
```
`httpStatusForCognitoError` mapping (this is what makes "invalid credentials" a clean 401 rather
than a 500): `NotAuthorizedException | UserNotFoundException | UserNotConfirmedException → 401`,
`UsernameExistsException → 409`, `InvalidPasswordException | InvalidParameterException |
CodeMismatchException | ExpiredCodeException → 400`, `TooManyRequestsException |
LimitExceededException → 429`, anything else → 502. The error `code` comes from the response's
`__type` field (`"…#NotAuthorizedException"` — take the part after `#`).

Because the pool has `prevent_user_existence_errors=True`, Cognito already returns a generic
`NotAuthorizedException` for both bad username and bad password; the route handler surfaces the
fixed string **"Incorrect username or password."**

### 5.2 `lib/session.ts`

```ts
export const REFRESH_COOKIE = "bookloud_refresh";
export const REFRESH_MAX_AGE = 30 * 24 * 3600;              // matches Cognito's default validity
export function setRefreshCookie(res: NextResponse, req: Request, token: string): void
export function clearRefreshCookie(res: NextResponse): void
```
Options: `httpOnly: true, sameSite: "lax", path: "/", maxAge: REFRESH_MAX_AGE`, and
`secure` **only when the request is HTTPS** — `req.headers.get("x-forwarded-proto") === "https" ||
new URL(req.url).protocol === "https:"`. (Cashlytics's comment applies verbatim: browsers drop
`Secure` cookies set from a non-HTTPS origin, which would make the compose stack on
`http://localhost:3000` — or a LAN IP — bounce forever.) Behind CloudFront, `x-forwarded-proto` is
`https`.

**Mandate `NextResponse.cookies.set/delete`, not `cookies()` from `next/headers`** — it keeps every
route handler a pure `(Request) => Promise<Response>` function that vitest can call directly with no
Next internals mocked.

### 5.3 Route handlers — `app/api/auth/*/route.ts`

| Route | Request | Success | Failure |
|---|---|---|---|
| `POST /api/auth/login` | `{username, password}` | `200 {idToken, expiresIn, username}` + sets refresh cookie | `401 {detail:"Incorrect username or password."}`, `400` on missing fields |
| `POST /api/auth/signup` | `{username, password, email?}` | `200 {status:"ok", idToken, expiresIn, username}` (auto-logs in when `UserConfirmed`) or `200 {status:"confirm_required"}` | `409` username taken, `400` invalid password/param |
| `POST /api/auth/confirm` | `{username, code}` | `200 {status:"ok"}` | `400` bad/expired code |
| `POST /api/auth/refresh` | *(cookie only)* | `200 {idToken, expiresIn}` | `401` + clears cookie (no cookie, or Cognito rejected it) |
| `POST /api/auth/logout` | — | `204` + clears cookie | never fails (RevokeToken is best-effort in a try/catch) |

Every handler wraps its Cognito call in `try/catch (CognitoError)` → `httpStatusForCognitoError`.
`expiresIn` is Cognito's `AuthenticationResult.ExpiresIn` (seconds).

### 5.4 `lib/auth.ts` (browser)

```ts
let idToken: string | null = null;
let expiresAt = 0;
let inflight: Promise<string | null> | null = null;

export async function login(username, password): Promise<void>
export async function signUp(username, password, email?): Promise<"ok" | "confirm_required">
export async function confirmSignUp(username, code): Promise<void>
export async function logout(): Promise<void>          // POST /api/auth/logout, clears memory
export async function getIdToken(): Promise<string | null>
```
`getIdToken()`: returns the in-memory token while `Date.now() < expiresAt - 60_000`; otherwise
single-flights a `POST /api/auth/refresh` (the `inflight` promise prevents a refresh stampede when
several components fetch at once); on 401 clears memory and returns `null`. On a *network* failure
(fetch rejects) it returns `null` **without** clearing anything — same distinction cashlytics draws
between "Cognito rejected us" and "we're offline".

`login`/`signUp` store `{idToken, expiresIn}` from the response into the module state.

### 5.5 `lib/api.ts` (extend, keep `apiUrl` unchanged)

```ts
export async function authFetch(path: string, init: RequestInit = {}): Promise<Response>
```
Attaches `Authorization: Bearer <await getIdToken()>` when a token exists; on a `401` response,
clears the in-memory token and does `window.location.href = "/login"` (guarded by
`typeof window !== "undefined"`). Note in a comment that a 401 here comes from **API Gateway**, not
FastAPI, and its body is `{"message":"Unauthorized"}` — never parse it for a `detail` field.

### 5.6 Pages, middleware, components

- `app/login/page.tsx` — client component. Fields: **username** (`autoComplete="username"`),
  **password** (`autoComplete="current-password"`), both `required`. Submit → `lib/auth.login` →
  `router.replace("/")`. Error → `role="alert"` box. Button disabled while submitting
  ("Signing in…"). Link to `/signup`.
- `app/signup/page.tsx` — client component. Fields: **username**, **password**, **confirm password**,
  **email** (`type="email"`) — `required` **only when running against prod** (read the frontend's
  `ENVIRONMENT` value, injected the same way as the backend's `Settings.environment`; in `dev`/`pr-*`/
  `local` it stays optional so PR-env and local signup testing isn't blocked on an email address).
  The route handler enforces this server-side too (§10 Q3) since client-side `required` alone is not
  a real guarantee.
  Client-side check: passwords match, else `role="alert"` **without** calling the API. On `"ok"` →
  `router.replace("/")`; on `"confirm_required"` → swap the form for a single "Confirmation code"
  field → `confirmSignUp` → `login` with the password still in state → `router.replace("/")`.
  (This branch mirrors the `NEW_PASSWORD_REQUIRED` pattern already established in cashlytics's login
  page; in AWS the PreSignUp trigger makes it unreachable, but it keeps local `cognito-local` signup
  working whatever its confirmation default is.) Link to `/login`.
- `components/AuthCard.tsx` — shared shell (title, optional subtitle, `role="alert"` error slot,
  children) plus exported `inputClass` / `labelClass` strings, so the two pages don't duplicate
  Tailwind. Visual language copied from the existing `app/page.tsx` (slate-950 bg, indigo-300
  headings, slate-900 card).
- `components/UserBadge.tsx` — client component on the home page: `authFetch(apiUrl("/me"))` →
  renders `Signed in as <username>` + a "Sign out" button (`logout()` then
  `router.replace("/login")`). This makes the whole loop visible in the PR environment and gives
  phase 6/8's Playwright suite an anchor.
- `app/page.tsx` — add `<UserBadge />` next to the existing health badge. Leave `HealthBadge` alone.
- `middleware.ts` — port cashlytics's verbatim, minus the `AUTH_ENABLED` bypass:
  ```ts
  const PUBLIC_PATHS = ["/login", "/signup"];
  // loggedIn = request.cookies.has(REFRESH_COOKIE)
  // !loggedIn && !public -> redirect("/login");  loggedIn && public -> redirect("/")
  export const config = { matcher: ["/((?!_next|api/auth|.*\\..*).*)"] };
  ```
  **Keep cashlytics's `redirect()` helper and its comment intact** — it is load-bearing: Next's
  non-edge middleware adapter throws "Invalid URL" on a bare relative `Location`, and behind
  CloudFront the Lambda never sees the real `Host`, so the helper must build an absolute URL from
  `x-forwarded-host` (the CloudFront Function in `frontend_stack.py` already sets that header) with
  `request.nextUrl.host` as the fallback.
  Dropping cashlytics's `AUTH_ENABLED` escape hatch is deliberate: bookloud's compose stack always
  has cognito-local wired, so a "sometimes auth is off" code path would only be a footgun. Note it
  in the file header.

### 5.7 Frontend tests (vitest)

- `__tests__/login.test.tsx` — mock `@/lib/auth` + `next/navigation`: successful submit calls
  `login(username, password)` and `router.replace("/")`; rejected login renders the
  `role="alert"` message and does **not** navigate (**"invalid credentials"** coverage); the button
  is disabled while in flight.
- `__tests__/signup.test.tsx` — success → replace("/"); mismatched passwords → alert and
  `signUp` **not called**; `"confirm_required"` → code field appears, `confirmSignUp` then `login`
  called in order; duplicate-username error renders the alert.
- `__tests__/auth.test.ts` — `getIdToken` returns the cached token without fetching; refreshes when
  empty/near-expiry; concurrent callers trigger exactly **one** `/api/auth/refresh` (single-flight);
  returns `null` and clears state on a 401; `logout` POSTs and clears memory.
- `__tests__/api.test.ts` (extend) — `authFetch` sets the `Authorization` header when a token
  exists, omits it when `null`, and on a 401 sets `window.location.href = "/login"`.
- `__tests__/auth-routes.test.ts` — import the `POST` handlers directly, `vi.stubGlobal("fetch", …)`
  a Cognito response: login success returns the id token and a `Set-Cookie` with `HttpOnly` and
  **without** `Secure` for an `http://` request but **with** `Secure` when `x-forwarded-proto: https`;
  `NotAuthorizedException` → 401 + `"Incorrect username or password."`; refresh with no cookie → 401;
  signup `UsernameExistsException` → 409; logout → 204 + `Max-Age=0`.
- `__tests__/middleware.test.ts` (`// @vitest-environment node` docblock, `NextRequest` from
  `next/server`) — no cookie + `/` → 307 to `/login`; no cookie + `/login` → pass through; cookie +
  `/login` → 307 to `/`; cookie + `/` → pass through; `x-forwarded-host` is honoured in the
  redirect target. **This is the frontend half of "protected route rejection without token."**

---

## 6. CI/CD and smoke test

### 6.1 `local/smoke_test.py` (extend, stdlib only)

New optional args: `--cognito-endpoint URL`, `--cognito-client-id ID`, `--login-username U`,
`--login-password P`, `--auth-signup`.

New checks (all skipped unless their inputs are supplied, except #1):
1. **Always:** `GET {api}/me` with no `Authorization` → **401** (`_get` already returns the status
   for `HTTPError`, so no retry-loop change is needed; assert on the code, not the body, because
   API Gateway's body differs from FastAPI's).
2. With `--cognito-client-id`: `InitiateAuth USER_PASSWORD_AUTH` with a deliberately wrong password
   → non-2xx with `NotAuthorizedException` (**"invalid credentials"**).
3. With `--auth-signup`: `SignUp` a throwaway user `smoke-<sha8>-<epoch>` / random password →
   `InitiateAuth` → `GET {api}/me` with the id token → **200** and `username` matches (**"signup" +
   "login" + authenticated access**).
4. With `--login-username/--login-password`: same as #3 but against an existing user (local path,
   using the seeded `dev` user, so CI's `local-smoke` never depends on cognito-local's signup
   confirmation semantics).

Cognito calls are plain `POST {endpoint}` with `Content-Type: application/x-amz-json-1.1` and an
`X-Amz-Target` header — `urllib` handles it, no dependency.

### 6.2 Workflows

- `.github/workflows/deploy-pr.yml` — the `deploy-backend` step **already** emits
  `user_pool_id` / `user_pool_client_id` outputs (currently unused). Add
  `--cognito-client-id "${{ steps.deploy-backend.outputs.user_pool_client_id }}"
  --cognito-region "${{ secrets.AWS_REGION }}" --auth-signup` to the smoke-test step. No other
  change: no new build-time env var (the SSR Lambda gets Cognito config at runtime from CDK).
- `.github/workflows/deploy-prod.yml` — smoke step gets **no** `--auth-signup` (never create
  throwaway users in the prod pool); the anonymous-401 check runs there by default.
- `.github/workflows/ci.yml` — unchanged; `local-smoke` picks up the new `make smoke` args and the
  new compose service for free.

---

## 7. Test plan → phase spec mapping

| Phase-spec requirement | Covered by |
|---|---|
| **signup** | `__tests__/signup.test.tsx`; `__tests__/auth-routes.test.ts` (signup handler, 409 path); `smoke_test.py --auth-signup` against the real PR-env pool |
| **login** | `__tests__/login.test.tsx`; `__tests__/auth.test.ts` (token storage/refresh); `__tests__/auth-routes.test.ts` (login handler + cookie flags); `smoke_test.py` login → `/me` 200 |
| **invalid credentials** | `__tests__/login.test.tsx` (rejected login renders alert, no navigation); `auth-routes.test.ts` (`NotAuthorizedException` → 401 + fixed message); `smoke_test.py` wrong-password check |
| **protected route rejection without token** | `tests/auth/test_controllers.py` (`client.get("/me")` → 401); `tests/test_lambda_function.py` (Mangum event without claims → 401); `infra/tests/test_synth.py` (`/{proxy+}` route `AuthorizationType: JWT`, `/health` `NONE`); `__tests__/middleware.test.ts` (no cookie → redirect to `/login`); `smoke_test.py` anonymous `GET /me` → 401 against real API Gateway |

Nothing on the backend needs to *perform* signup/login (that is Cognito's job via the Next BFF), so
the backend's test surface is exactly: claims extraction, the 401/200 boundary, and the local-dev
middleware. No test anywhere calls real Cognito except the smoke test against a deployed PR env.

---

## 8. Concrete file list

**`infra/`**
- `stacks/api_stack.py` (change) — `HttpUserPoolAuthorizer` on `/{proxy+}`, `HttpNoneAuthorizer` on the five public paths, two new constructor kwargs.
- `stacks/auth_stack.py` (change) — PreSignUp auto-confirm Lambda + `lambda_triggers` on the pool.
- `stacks/frontend_stack.py` (change) — `COGNITO_CLIENT_ID` / `COGNITO_REGION` env on the SSR Lambda.
- `stacks/config.py` (change) — `ENV_COGNITO_CLIENT_ID`, `ENV_COGNITO_REGION`, `ENV_COGNITO_ENDPOINT`.
- `app.py` (change) — pass the pool/client into `ApiStack`, the client id/region into `FrontendStack`.
- `tests/test_synth.py` (change) — authorizer/route-authorization, PreSignUp trigger, explicit auth flows, frontend env assertions.

**`backend/`**
- `src/auth/__init__.py` (new) — package marker.
- `src/auth/dependencies.py` (new) — `CurrentUser`, `claims_from_request`, `get_current_user`, `HTTPBearer` doc scheme.
- `src/auth/controllers.py` (new) — `GET /me`.
- `src/auth/local_dev.py` (new) — `LocalAuthMiddleware`, `should_enable`.
- `src/main.py` (change) — include the auth router, conditionally mount the local middleware.
- `tests/conftest.py` (change) — `CLAIMS`, `WithGatewayClaims`, `authed_client` fixture.
- `tests/auth/__init__.py` (new) — package marker.
- `tests/auth/test_dependencies.py` (new) — claims extraction + 401 paths.
- `tests/auth/test_local_dev.py` (new) — middleware behaviour + environment gate.
- `tests/auth/test_controllers.py` (new) — `/me` 200 authed / 401 anonymous, `/health` still public.
- `tests/test_lambda_function.py` (change) — Mangum event with/without authorizer claims.

**`frontend/`**
- `lib/cognito.ts` (new) — server-only Cognito JSON API client + error→HTTP-status mapping.
- `lib/session.ts` (new) — refresh-cookie name/options, set/clear helpers.
- `lib/auth.ts` (new) — browser session: login/signUp/confirm/logout/getIdToken (in-memory id token, single-flight refresh).
- `lib/api.ts` (change) — add `authFetch` (Bearer header + 401 → `/login`).
- `app/api/auth/login/route.ts` (new) — `InitiateAuth USER_PASSWORD_AUTH`, sets the refresh cookie.
- `app/api/auth/signup/route.ts` (new) — `SignUp` (+ auto-login when confirmed).
- `app/api/auth/confirm/route.ts` (new) — `ConfirmSignUp` fallback path.
- `app/api/auth/refresh/route.ts` (new) — `InitiateAuth REFRESH_TOKEN_AUTH` from the cookie.
- `app/api/auth/logout/route.ts` (new) — clears the cookie, best-effort `RevokeToken`.
- `app/login/page.tsx` (new) — username + password form.
- `app/signup/page.tsx` (new) — username + password + confirm + optional email, with the confirm-code branch.
- `components/AuthCard.tsx` (new) — shared card shell + input/label class strings.
- `components/UserBadge.tsx` (new) — `/me` badge + sign-out on the home page.
- `app/page.tsx` (change) — render `<UserBadge />`.
- `middleware.ts` (new) — cookie-based route gate, absolute-URL redirect helper.
- `.env.example` (change) — server-side `COGNITO_*` vars.
- `__tests__/login.test.tsx`, `__tests__/signup.test.tsx`, `__tests__/auth.test.ts`, `__tests__/auth-routes.test.ts`, `__tests__/middleware.test.ts` (new); `__tests__/api.test.ts` (change).

**`local/`**
- `cognito_bootstrap.py` (new) — idempotent cognito-local pool/client/dev-user provisioning; writes `local/.cognito.env`.
- `setup.sh` (change) — run the bootstrap with retries after LocalStack provisioning.
- `smoke_test.py` (change) — anonymous `/me` 401, wrong-password, signup/login → `/me` 200 checks.

**Root**
- `docker-compose.yml` (change) — `cognito-local` service; backend `COGNITO_ENDPOINT_URL` + `./local:/local-shared`; frontend runtime `COGNITO_*`.
- `Makefile` (change) — `up`/`ui`/`smoke` updates, new `token` target.
- `.gitignore` (change) — `local/.cognito.env`.
- `.env.example` (change) — Cognito local dev vars.
- `README.md` (change) — auth model (BFF + httpOnly refresh cookie), local login credentials (`dev`/`devpassword`), new make targets.
- `.github/workflows/deploy-pr.yml`, `deploy-prod.yml` (change) — smoke-test auth args.
- `IMPLEMENTATION_PLAN.md` (change) — tick Phase 1.
- `PLANS/phase-1.md` — this document.

---

## 9. Implementation sequence

1. **Infra first** (`config.py` → `auth_stack.py` → `api_stack.py` → `frontend_stack.py` → `app.py` → `test_synth.py`). `pytest infra/tests` + `cdk synth -c environment=pr-0` must pass, and `cdk diff` on a PR env must show **no** user-pool replacement.
2. **Backend** (`src/auth/*` → `main.py` → tests). `uv run pytest` green at ≥90 % coverage.
3. **Local Cognito** (`docker-compose.yml` → `local/cognito_bootstrap.py` → `setup.sh` → `Makefile`). `make up` must print "Cognito ready"; `make token` must return a JWT; `curl` with and without that token against `/me` must give 200 / 401.
4. **Frontend server side** (`lib/cognito.ts` → `lib/session.ts` → route handlers → `auth-routes.test.ts`).
5. **Frontend client side** (`lib/auth.ts` → `lib/api.ts` → pages/components → `middleware.ts` → remaining tests). `npx tsc --noEmit` + `npm test` green.
6. **Smoke test + workflows**, then `make e2e` end to end locally.
7. Update `README.md` and tick the checklist in `IMPLEMENTATION_PLAN.md`.

Deliberately **out of scope** for Phase 1 (flag if the implementer feels pulled toward them):
password reset / forgot-password (needs an email channel), Cognito groups/roles, MFA, hosted UI or
social IdPs, per-user data isolation in DynamoDB (that is phase 2, keyed on `CurrentUser.sub`),
Playwright specs (phase 6/8).

---

## 11. Addendum — user provisioning switched to admin-only (post-plan revision)

After the plan above was implemented (PR #2, branch `phase-1`), the human decided user accounts
should be **admin-provisioned only** — no public self-signup. Revised, before merge:

- **`infra/stacks/auth_stack.py`**: `self_sign_up_enabled=False` on the user pool. This is the real
  enforcement — Cognito's public `SignUp` API rejects every call with `NotAuthorizedException:
  "Sign up isn't allowed for this user pool"` regardless of what the frontend does, satisfying "stop
  it, enforce on backend too." The PreSignUp auto-confirm Lambda trigger **stays wired** (harmless —
  unreachable once self-signup is off, since `SignUp` is rejected before the trigger would fire) —
  human chose "keep code but disable" over deleting it, in case self-signup is ever revisited.
- **Frontend `app/login/page.tsx`**: remove the "Sign up" link. **`app/signup/page.tsx`,
  `app/api/auth/{signup,confirm}/route.ts`, and their tests are left in the repo unreached** — no nav
  path to them; hitting `/signup` directly still renders the page, but submitting now surfaces
  Cognito's rejection through the existing `httpStatusForCognitoError` mapping (`NotAuthorizedException
  → 401`) rather than succeeding. No route removed, no test deleted.
- **New: forced first-login password change.** Admin creates a user (AWS Console or
  `aws cognito-idp admin-create-user` / `admin-set-user-password` without `--permanent`) with a
  temporary password. That user's first `InitiateAuth` returns `ChallengeName:
  NEW_PASSWORD_REQUIRED` + a `Session` token instead of tokens. New pieces:
  - `lib/cognito.ts`: `respondToNewPasswordChallenge(session, username, newPassword)` →
    `RespondToAuthChallenge` with `ChallengeResponses: {USERNAME, NEW_PASSWORD}`.
  - `app/api/auth/login/route.ts`: when Cognito's response has `ChallengeName ===
    "NEW_PASSWORD_REQUIRED"`, return `200 {status:"new_password_required", session}` instead of
    setting the refresh cookie.
  - New `app/api/auth/new-password/route.ts`: `{username, session, newPassword}` →
    `respondToNewPasswordChallenge` → on success, sets the refresh cookie and returns
    `{idToken, expiresIn, username}` exactly like a normal login.
  - `lib/auth.ts`: `login()` returns a discriminated result (`{status:"ok"}` vs
    `{status:"new_password_required", session}`); new `completeNewPassword(username, session,
    newPassword)`.
  - `app/login/page.tsx`: on `new_password_required`, swap the form for a "choose a new password"
    + confirm-password step, matching `AuthCard`'s existing visual language; on success,
    `router.replace("/")`.
  - **Out of scope, per human's explicit call:** no general "change password while logged in"
    self-service page — first-login forced change only.
- **Local dev** (`local/cognito_bootstrap.py`): keep the existing `dev`/`devpassword` user exactly as
  is (`AdminSetUserPassword(..., Permanent=True)` — no forced-change friction for routine local
  work/`make smoke`). Add a **second** seeded user, `newuser` / `TempPass123!`, created with
  `Permanent=False` specifically so the `NEW_PASSWORD_REQUIRED` path has something to exercise
  locally and in an added smoke-test check (`local/smoke_test.py`: login as `newuser` → assert
  challenge response → `RespondToAuthChallenge` with a fresh password → assert `/me` 200 afterward).
- **Tests to update**: `infra/tests/test_synth.py` (assert `self_sign_up_enabled=False` — this
  inverts whatever the original Phase 1 assertion said); new frontend tests for the challenge
  branch in `__tests__/login.test.tsx` and `__tests__/auth-routes.test.ts`; new backend/local-dev
  coverage isn't needed (this flow is entirely Cognito + frontend, the backend dependency/middleware
  are unchanged).
- **README**: document the provisioning process — how the human adds a new reader (console or CLI
  `admin-create-user` + `admin-set-user-password` without `--permanent`), and that first login
  prompts for a new password.
- Section 8's file list gains: `app/api/auth/new-password/route.ts` (new),
  `__tests__/new-password-route.test.ts` or folded into `auth-routes.test.ts` (new assertions),
  `local/cognito_bootstrap.py` (change: second seeded user), `local/smoke_test.py` (change: new
  challenge-flow check), `README.md` (change: provisioning instructions).

---

## 10. Open questions / assumptions — human decisions needed before implementation

**Q1 — Auth topology (blocking). DECIDED: Next.js BFF (option B).**

**Q2 — Self-signup confirmation (blocking). DECIDED: PreSignUp Lambda auto-confirm.**

**Q3 — Email at signup. DECIDED: required in prod**, optional in non-prod. Non-alias attribute
either way (sign-in stays username-only). The signup route handler (`app/api/auth/signup/route.ts`)
must enforce this server-side — not just as an HTML `required` attribute — by checking
`settings`/`NODE_ENV`-equivalent (an `ENVIRONMENT` env var on the frontend, same pattern as the
backend's `Settings.environment`) and returning 400 if email is missing while `environment === "prod"`.
Add `ENV_ENVIRONMENT`/`ENVIRONMENT` to the frontend's runtime env vars in
`infra/stacks/frontend_stack.py` if not already present, so the route handler can read it.

**Q4 — Routes and naming.** Assumed `/login` and `/signup` as separate pages, `/me` as the backend
identity route, cookie name `bookloud_refresh`. Confirmed by proceeding.

**Q5 — Session length / "remember me". DECIDED: no checkbox**, every login gets the full 30-day
refresh cookie, id token ~1 h.

**Q6 — Local Cognito emulation. DECIDED: `jagregory/cognito-local`** in docker-compose, with
`LocalAuthMiddleware` mounted only when `ENVIRONMENT == "local"`.

**Q7 — Seeded local user.** `dev` / `devpassword`, created by `local/cognito_bootstrap.py`.

**Q8 — Throwaway users in PR environments. DECIDED: acceptable** (pools are destroyed with the PR
env). `--auth-signup` must never run against prod — enforced by only passing that flag in
`deploy-pr.yml`, never in `deploy-prod.yml`.

**Q9 — Visual design.** Assumed the auth pages reuse `app/page.tsx`'s existing slate/indigo dark
palette, no new design work this phase.

**Q10 — Does anything stay public?** `/health`, `/`, `/docs`, `/redoc`, `/openapi.json` remain
public (Phase 0's list; the smoke test depends on `/health`).
