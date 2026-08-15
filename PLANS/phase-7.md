# Phase 7 — Chat sidebar

Goal: a right-hand sidebar in the reader where the user asks questions about the section they are currently on, and the answer streams in token by token. Three things make this phase structurally different from every phase before it:

1. **An LLM is an external service**, so `PLANS/phase-4.md` §0's rule applies to it in full: real calls happen **only** when `ENVIRONMENT == "prod"`, the gate is checked first and unconditionally, and every other environment gets a deterministic offline stub. §5 works out what that stub must *do* rather than merely refuse, because a raise-only stub would leave the entire streaming transport unexercised in every environment that exists.
2. **Streaming cannot go through API Gateway.** `IMPLEMENTATION_PLAN.md` specifies a Lambda Function URL with response streaming, and a Function URL is a *different endpoint* from the HTTP API — with no Cognito JWT authorizer in front of it. §4 decides how it authenticates, and it is the first time this repo validates a token itself.
3. **There is no API key yet, and prod must work without one.** §5.3 specifies exactly what a prod deployment with `OPENAI_SECRET_NAME=""` shows the user, and it is a banner and a readable book — not an error.

**Verified against the current repo (post `562a8ee`, phase 6 merged):**

- `interface/controllers.py` exposes 11 routes, all under `ApiStack`'s `/{proxy+}` with `HttpUserPoolAuthorizer`. The two **buffered** chat routes this phase adds (`GET`/`DELETE /books/{id}/chat`) inherit that authorizer with no `api_stack.py` route change. The **streaming** route does not, and that is the whole of §4.
- `src/auth/dependencies.py`'s module docstring is explicit: *"The backend **never validates a token**. API Gateway's Cognito JWT authorizer does that before the Lambda is invoked, and forwards the verified claims in `requestContext.authorizer.jwt.claims`."* Phase 7 is the first time that stops being true — for exactly one function. §4.3 keeps `get_current_user` **byte-identical** by having the new verifier inject claims into `scope["aws.event"]`, exactly as `LocalAuthMiddleware` already does.
- `src/auth/local_dev.py`'s `LocalAuthMiddleware` is the template for that: pure-ASGI, decodes a bearer token, writes `scope["aws.event"]["requestContext"]["authorizer"]["jwt"]["claims"]`, and is mounted only when `environment == "local"`. The new `FunctionUrlAuthMiddleware` is its verifying sibling.
- `domain/repository.py`'s module docstring already states the rule this phase needs verbatim: *"`ChunkRepository` performs no access control. Its methods take a bare `book_id: str` with no user in the key (chunk items are keyed `PK=BOOK#<bookId>`, which structurally cannot carry ownership). Every caller **must** already have authorized `book_id` via `BookRepository.get(user_id, book_id)`."* `CHAT#` items have the identical shape and get the identical rule (§7.3).
- `domain/chunking.py`'s `DEFAULT_TARGET_CHARS = 1800`, `DEFAULT_MAX_CHARS = 2600`. Every number in §6's token budget derives from these two, not from a guess.
- `infrastructure/secrets.py`'s `get_secret()` is module-cached ("once per cold Lambda container"). Reused verbatim for the OpenAI key — and the phase-4 §6.4/OQ-A correction (never call it eagerly against a secret that may not exist) is the load-bearing constraint on §5.2's ordering.
- `infrastructure/google_tts_synthesizer.py` calls a third-party REST API with **`urllib` and an injected HTTP callable**, not an SDK ("no gRPC, no 30 MB of deps" — phase-4 Q8). `tests/contexts/library/test_google_tts_synthesizer.py` injects a fake callable so the suite touches no network. §5.4 reuses that exact shape for OpenAI.
- `backend/Dockerfile` has two stages (`dev`, `lambda`); `pipeline_stack.py` builds three Lambdas from the *same* `lambda` image with different `cmd`s, with a comment explaining that `DockerImageAsset`'s hash comes from the source dir + build args + platform, **not** `cmd`. §9.2 explains why the streaming function is the one case that cannot share that image.
- `api_stack.py` carries the `# NO reserved_concurrent_executions, here or anywhere` comment and `infra/tests/test_synth.py` asserts `Match.absent()`. §9.4 does the concurrency arithmetic for the new function and adds the same assertion.
- `infra/app.py` defaults `google_tts_secret_name` to `""` **in every environment including prod**, with a nine-line comment explaining why a prod default would have been the bug. §5.2 copies that shape exactly for `openai_secret_name`.
- `frontend/`'s runtime dependencies are still exactly `next`, `react`, `react-dom`. Phase 6 §7.3 said: *"Where this would stop being true: phase 7's chat sidebar adds streamed messages and a third long-lived state slice. If that turns into cross-cutting invalidation, revisit **then**, with a concrete problem."* §8.2 revisits it and concludes: still no store.
- `deploy-pr.yml` builds the frontend with `NEXT_PUBLIC_API_BASE_URL` taken from the deployed `ApiUrl` **stack output**, then runs `local/smoke_test.py` and the `chromium` Playwright project against the real deploy. The chat Function URL follows the identical path (a new `ChatUrl` output → build env → smoke arg).
- `storage_stack.py`'s table has no TTL attribute and no GSI. §7.4 explains why this phase adds neither.

---

## 1. End-to-end flow

Note the **two hosts**. Everything buffered goes through API Gateway as it always has; exactly one route — the streaming one — goes to a Lambda Function URL.

```
browser (client components)                      API Gateway -> ApiFunction            DynamoDB / OpenAI
  |
  | GET /books/{id}/chunks        (phase 6) ---->| ListBookChunks ------------------->| Query CHUNK#*
  | GET /books/{id}/chat          (NEW) -------->| ListBookChat --------------------->| Query CHAT#*  (desc, limit 50)
  |<-- {messages:[...], chat:{enabled,reason,model,dailyLimit,usedToday}}
  |
  |  ... user opens the sidebar, types a question ...
  |
  |                                                Lambda Function URL (RESPONSE_STREAM)
  | POST /books/{id}/chat  --------------------->| ChatFunction (LWA + uvicorn + FastAPI)
  |   Authorization: Bearer <cognito id token>    |  1. FunctionUrlAuthMiddleware: verify RS256 vs JWKS
  |   {question, anchorChunk, positionMs}         |     -> inject claims into scope["aws.event"]   (401)
  |                                               |  2. _load_owned_book(user.sub, book_id)        (404)
  |                                               |  3. book.chunks_total > 0                      (409)
  |                                               |  4. quota: ADD count :one IF count < 50        (429)
  |                                               |  5. Query CHUNK# window + CHAT# history
  |                                               |  6. resolve_context()  (PURE)
  |                                               |  7. get_chat_model()   <- ENVIRONMENT gate
  |<== event: meta   {model, enabled, reason, anchorChunk, windowChunks}
  |<== event: delta  {text}     x N   <---------- |  8. OpenAI /v1/chat/completions  stream:true
  |<== event: delta  {text}                       |     (prod + secret set)   OR   StubChatModel
  |<== event: done   {finishReason, usage}        |  9. finally: save_turn(user msg, assistant msg)
  |                                               |                                  -> 2 x CHAT# items
  |
  | DELETE /books/{id}/chat  (API GW) ----------->| ClearBookChat -------------------->| BatchWrite delete
```

Five invariants the phase rests on:

1. **Every error that can be known before the first byte is returned as a real HTTP status.** Once one byte of the body is out, the status is 200 forever. §4.5 makes "do all the failable work first" a structural rule, not a habit.
2. **The environment gate is checked before the model is constructed, and the secret is read only after the gate passes.** Phase-4 §0's `get_speech_synthesizer()` shape, applied verbatim (§5.2). An `OPENAI_SECRET_NAME` accidentally set on a PR stack still cannot produce a network call.
3. **The anchor is "where the user is looking", not "where the audio is."** Playback position is one source of that; scroll position is the other, and in every environment CI can reach it is the *only* one, because no book there has audio (phase-4 §0). §6.2.
4. **We re-frame the model's stream; we never proxy it.** The browser sees our four event types and nothing of OpenAI's wire format (§4.6). This is what lets §5's stub be indistinguishable from the real thing at the transport layer, which is what makes the transport testable at all.
5. **The API key exists in exactly one place at runtime — a local variable inside one adapter instance — and in exactly one place at rest — Secrets Manager.** It is never in `Settings`, never in a Lambda environment variable, never in a log record, never in a `repr()`, never in an error surfaced to the client, and never in a CloudFormation template. §5.5 makes each of those a test.

---

## 2. Decisions up front

| # | Question | Decision | § |
|---|---|---|---|
| Q1 | **How does the streaming endpoint authenticate?** | **`AuthType: NONE` on the Function URL, Cognito id token verified inside the Lambda** (RS256 against the pool's JWKS, `iss`/`aud`/`exp`/`token_use` all checked), with the verified claims injected into `scope["aws.event"]` so `get_current_user` and every use case below it are **unchanged**. Rejected: `AWS_IAM` + a Cognito identity pool (a second identity system and browser SigV4, for one route); a bespoke HMAC "chat ticket" minted by the API (inventing a token format when a verified one already exists). | §4.3 |
| Q2 | **Won't `AuthType: NONE` + a public URL get abused?** | The URL is public; the *expensive path* is not. Auth is the first thing that runs, before any DynamoDB read and long before any model call, against a module-cached JWKS — an unauthenticated POST costs ~5 ms of Lambda time and nothing else. The money guard is a **per-user, per-day atomic counter** (`USER#<sub>` / `CHATQUOTA#<date>`, conditional `count < 50`), checked before context resolution. | §4.7 |
| Q3 | **CORS on the streaming endpoint** | **Function-URL-native CORS** (`FunctionUrlCorsOptions`), not FastAPI's `CORSMiddleware` and not an API Gateway route. Lambda answers `OPTIONS` **without invoking the function**, so there is structurally no authorizer, no route match, and no handler for a preflight to fail against. This is the direct answer to phase-6 §16's measured `HTTP/2 401` on every preflight: that bug required a route *and* an authorizer to both match `OPTIONS`; here neither exists. | §4.4 |
| Q4 | **A new Lambda, against a 10-execution account cap?** | **Yes, one — `ChatFunction` — and it is unavoidable.** The API Lambda runs Mangum behind API Gateway, which buffers; the Python managed runtime cannot stream at all; response streaming needs the Lambda Web Adapter as the image ENTRYPOINT, which is mutually exclusive with the RIC entrypoint the other four functions use. Arithmetic: `2 synthesize + 1 extract + 2 stitch = 5` (only while a book processes) `+ 1 API + 1 SSR + 1 chat = 8` of 10, and chat is single-user and serialized by the UI. **No `reserved_concurrent_executions` anywhere**, asserted. | §9.2, §9.4 |
| Q5 | **Which image?** | A **new `lambda-stream` Dockerfile stage**, `FROM lambda`, adding only the LWA binary and a uvicorn ENTRYPOINT. **Not the shared `lambda` stage** — an extension in `/opt/extensions/` is started for *every* function built from that image, and LWA would then race the RIC for the Runtime API on Extract/Synthesize/Stitch/Api. Because the new stage is `FROM lambda`, the pushed delta is one small layer. | §9.2 |
| Q6 | **Which LLM, and where does the key live?** | **OpenAI Chat Completions, `gpt-4.1-mini`** (see Q7), called over **`urllib` + REST with an injected HTTP callable** — `GoogleTtsSynthesizer`'s exact shape (phase-4 Q8), so the suite touches no network and no runtime dependency is added. Key in **Secrets Manager as `bookloud/openai-api-key`, created out of band**; `OPENAI_SECRET_NAME` defaults to `""` in **every** environment including prod; CDK does only `from_secret_name_v2` + `grant_read`. Never through GitHub Actions, never a CloudFormation parameter. | §5.2, §5.4 |
| Q7 | **Which model?** | **`gpt-4.1-mini`.** With §6's context design (~4,100 input tokens, ~350 output) that is **≈ $0.0022 per question**, ≈ $1.30/month at 20 questions/day. `gpt-4o-mini` is ~2.6× cheaper and measurably weaker at grounded extraction; `gpt-4.1` is ~5× dearer. `OPENAI_MODEL` is an env var, so changing it is one line and a deploy. See **OQ-2**. | §6.5 |
| Q8 | **What does the stub do outside prod?** | **It streams.** A raise-only stub (phase-4's `StubSynthesizer`) would mean *no environment in existence* ever exercises the Function URL, `InvokeMode: RESPONSE_STREAM`, LWA, the SSE re-framing, the browser's `ReadableStream` reader, or the incremental render. So `StubChatModel` emits a deterministic, **network-free**, canned answer that quotes the **real resolved anchor text** — which turns "verify the response references book content" into a genuine context-resolution assertion in a PR environment. | §5.1 |
| Q9 | **What does prod with no key do?** | Same class, different reason. `reason: "NOT_CONFIGURED"` streams **one honest sentence** ("Chat isn't set up for this deployment yet…"), the sidebar renders a persistent banner and **disables the composer**, and the book stays fully readable and playable. Nothing 5xxs. `GET /books/{id}/chat` reports `enabled: false` before the first question so the composer is disabled on open, not after a wasted turn. | §5.3 |
| Q10 | **Context resolution** | **Anchor chunk ± 2 neighbours (5 chunks, ~9,000 chars, ~2,400 tok)**, clipped at the book's ends rather than shifted, with a 12,000-char hard ceiling that evicts **farthest neighbour first** and never the anchor. Plus **the last 3 user/assistant pairs**, each truncated to 1,000 chars. Total ≈ **4,100 input tokens**. | §6.1, §6.5 |
| Q11 | **Anchoring when nothing is playing** | Priority: (1) `usePlayback().state.chunkIndex` when `>= 0`; (2) **the topmost chunk paragraph intersecting the viewport**, via one `IntersectionObserver` over the reading pane; (3) `0`. Case (2) is not a fallback — it is the *normal* case, because in local compose the user is usually paused and in every PR environment there is no audio at all. Server clamps to `[0, chunksTotal-1]` and never trusts the client's number. | §6.2 |
| Q12 | **Chat persistence — when to write** | **Both messages, once, after the stream ends**, from the generator's `finally` (which also runs on client disconnect). One write, no dangling half-turn, and the persisted transcript always matches what the user actually saw — including a partial answer, which is stored with `finishReason: "ERROR"`/`"TRUNCATED"` rather than silently dropped. Named cost: a hard Lambda crash mid-stream loses the turn. | §7.2 |
| Q13 | **Chat SK layout** | **`CHAT#<createdAt>#<msgId>`**, refining `IMPLEMENTATION_PLAN.md`'s `CHAT#<msgId>`. A bare UUID sorts randomly, so "the last 6 messages" would need a full partition scan and a client-side sort on every question. An ISO-8601 UTC prefix makes the SK chronological, so recent history is one `Query(ScanIndexForward=False, Limit=N)`. The uuid suffix keeps same-microsecond writes distinct. **OQ-6.** | §7.1 |
| Q14 | **Per-book or per-book-per-user history?** | **Per book**, keyed `BOOK#<bookId>` exactly as the spec says — and that is per-user *only because a book has exactly one owner today* (`USER#<sub>` / `BOOK#<bookId>`). Authorization is therefore identical to `ChunkRepository`'s: the port performs none, and `_load_owned_book` is the single gate. A denormalised `userId` attribute is written on every item now, so the day books become shareable the filter exists without a migration. | §7.3 |
| Q15 | **Abuse / cost control** | Four layers: fail-fast auth before any I/O; ownership before any model call; a **per-user daily cap of 50** enforced by one conditional `UpdateItem` *before* context resolution; and a 2,000-char question cap rejected by pydantic. No reserved concurrency (forbidden), so the counter is the real guard. | §4.7 |
| Q16 | **SSE or NDJSON to the browser?** | **SSE (`text/event-stream`)**, four event types (`meta`, `delta`, `done`, `error`), parsed by ~40 lines of our own code over `fetch` + `ReadableStream` (`EventSource` cannot POST or set headers). Chosen over NDJSON for one reason that matters here: `text/event-stream` is the media type every intermediary treats as "do not buffer", and buffering is the failure mode no local test can detect. | §4.6 |
| Q17 | **Errors after headers are sent** | An `event: error` frame followed by `event: done` with `finishReason: "ERROR"`; the client keeps the partial text and renders an inline chip beneath it. A stream that ends with **no** `done` at all is a third case (`TRUNCATED`) the client must handle explicitly, because a dropped connection produces no frame to react to. | §4.5 |
| Q18 | **New bounded context?** | **No.** Chat reads the Library's own `Book`/`Chunk` aggregates and adds one aggregate (`ChatMessage`) that only exists relative to a book. Same answer as phase-3 OQ-1, phase-4 Q19, phase-5 Q15, phase-6 Q18. `src/main.py`'s docstring, which anticipates a `chat/` package, is updated to say so. | §3 |
| Q19 | **State management, revisited** | Phase-6 Q12 deferred this to "revisit **then**, with a concrete problem." The concrete problem is one hook holding a message array and a streaming buffer, consumed by one component. **Still React state, still no store.** The one borrowed pattern is phase 6's: deltas accumulate in a ref and flush to state on `requestAnimationFrame`, so a fast token stream cannot out-render the browser. | §8.2 |
| Q20 | **New runtime dependencies** | **One: `pyjwt[crypto]`** (backend), for RS256 verification — there is no stdlib RSA. Deliberately **not** the `openai` SDK (Q6). Deliberately **no** new frontend dependency. `pyjwt[crypto]` pulls `cryptography`, ~4 MB, into an image that already carries PyMuPDF and edge-tts. | §9.5 |

---

## 3. DDD placement — stays inside `contexts/library/`

Same argument as every prior phase. New/changed layout (⊕ new, Δ changed):

```
backend/src/
├── chat_app.py                          ⊕  the streaming ASGI app (§4.2)
├── auth/
│   ├── jwt_verifier.py                  ⊕  CognitoJwtVerifier (§4.3)
│   └── function_url.py                  ⊕  FunctionUrlAuthMiddleware (§4.3)
├── config.py                            Δ  + openai_*, cognito_* settings
└── contexts/library/
    ├── domain/
    │   ├── chat.py                      ⊕  ChatMessage, ChatContext, ChatModel port,
    │   │                                   resolve_context() [PURE], chat_availability()
    │   └── repository.py                Δ  + ChatRepository, ChatQuotaRepository
    ├── application/
    │   └── chat.py                      ⊕  AskBookQuestion, ListBookChat, ClearBookChat
    ├── infrastructure/
    │   ├── openai_chat_model.py         ⊕  OpenAiChatModel (urllib + REST, streaming)
    │   ├── openai_prompt.py             ⊕  render_messages() [PURE]
    │   ├── stub_chat_model.py           ⊕  StubChatModel (offline, deterministic)
    │   ├── dynamodb_chat_repository.py  ⊕
    │   ├── chat_mapper.py               ⊕
    │   ├── dynamodb_chat_quota_repository.py ⊕
    │   └── keys.py                      Δ  + CHAT_PREFIX, sk_chat, sk_chat_quota
    └── interface/
        ├── chat_controllers.py          ⊕  POST /books/{id}/chat  (Function URL only)
        ├── controllers.py               Δ  + GET/DELETE /books/{id}/chat (API GW)
        ├── dependencies.py              Δ  + get_chat_model, get_chat_repository,
        │                                   get_chat_quota_repository
        └── schemas.py                   Δ  + chat_message_to_dict, chat_listing_to_dict,
                                            sse_frame()
```

`ChatMessage` is a new aggregate, but it is meaningless without a book, its partition key *is* the book, and every operation on it starts by authorizing that book. That is a new entity in an existing context, not a new context.

---

## 4. The crux: streaming, and what a Function URL costs you

### 4.1 Why the existing API cannot do this

Three independent walls, any one of which is fatal:

| wall | detail |
|---|---|
| **API Gateway HTTP APIs buffer.** | There is no streaming integration for a Lambda proxy integration. The client sees nothing until the Lambda returns. `IMPLEMENTATION_PLAN.md` states this as the reason for the Function URL; it is correct. |
| **The Python managed runtime cannot stream.** | Lambda response streaming is implemented for the Node.js managed runtimes and, for everything else, through a custom-runtime/adapter path. The `public.ecr.aws/lambda/python:3.12` RIC returns one buffered payload. |
| **Mangum is buffered by construction.** | `lambda_function.handler` wraps the ASGI app and returns a single `{"statusCode":…, "body": …}` dict. A `StreamingResponse` under Mangum is fully consumed before the handler returns. |

So streaming needs (a) a Function URL with `InvokeMode: RESPONSE_STREAM`, and (b) a process that speaks HTTP incrementally — the **AWS Lambda Web Adapter** running uvicorn, which is the documented path for FastAPI on Lambda with `AWS_LWA_INVOKE_MODE=response_stream`.

Options considered and rejected:

| option | verdict |
|---|---|
| **A. New `ChatFunction` + Function URL + LWA** | **Chosen.** |
| B. Add a Function URL to the existing `ApiFunction` | **Impossible.** LWA replaces the runtime entrypoint; the same image cannot both run the RIC (for the API Gateway integration) and hand the Runtime API to LWA. |
| C. Stream from the Next.js SSR Lambda (which already has a Function URL) | **Rejected.** Node streams natively, but the chat logic needs `Book`/`Chunk` reads, context resolution and the API key — all of which would have to be reimplemented in TypeScript, in the *frontend* function, which would then hold the key. Wrong layer, duplicated domain, worse blast radius. |
| D. SSR route handler proxies to a backend Function URL and re-streams | **Rejected.** Two Lambdas concurrent per question against a 10-execution cap, one extra hop of buffering risk, and the auth problem is not solved — only moved. |
| E. Don't stream; return the whole answer from the existing API | **Genuinely viable, and it is OQ-1.** Zero new infra, zero new auth surface. Cost is a 5-15 s spinner per question. This plan ships streaming because that is what the phase specifies, but a human gets one word to collapse the phase. |

### 4.2 `src/chat_app.py` — a second ASGI app, deliberately

```python
"""The streaming chat app. NOT src/main.py, and it must never import the
library router.

Two reasons, both load-bearing:

1. This app is served by a Lambda **Function URL** whose AuthType is NONE.
   Every route it exposes is reachable by anyone on the internet until this
   process itself rejects them. Mounting the full library router here would
   put GET /books, the presigned-upload issuer and /resynthesize behind a
   home-grown verifier instead of API Gateway's Cognito authorizer, for no
   benefit -- those routes do not stream.
2. The two apps authenticate differently. main.py trusts
   requestContext.authorizer.jwt.claims because API Gateway put them there.
   This app has no API Gateway in front of it and must verify the token
   itself (auth/function_url.py). Sharing one app would mean one of those
   two paths is wrong somewhere.

PLANS/phase-7.md §4.2. tests/test_chat_app.py asserts the route set is
exactly {"/health", "/books/{book_id}/chat"} so an accidental
`include_router(library_router)` fails a unit test rather than exposing the
API.
"""
app = FastAPI(title="Bookloud Chat")
app.include_router(health_router)
app.include_router(chat_router)

if should_enable(settings.environment):          # local only, unchanged helper
    app.add_middleware(LocalAuthMiddleware)
else:
    app.add_middleware(FunctionUrlAuthMiddleware)
```

No `CORSMiddleware`: in AWS the Function URL's own CORS config answers preflights (§4.4); in local compose the browser talks to `http://localhost:8001` and this app *does* need CORS — so `CORSMiddleware` is added under the same `should_enable(settings.environment)` branch as `LocalAuthMiddleware`. Stated explicitly because "the two environments answer preflight in different places" is exactly the asymmetry that produced phase-6's bug, and here it is deliberate and documented rather than accidental.

### 4.3 In-Lambda JWT verification

```python
# src/auth/jwt_verifier.py

class InvalidToken(Exception):
    """Never carries the token, a claim value, or a key. Its str() is one of a
    small closed set of reasons, safe to log."""


class CognitoJwtVerifier:
    """Verifies a Cognito **id** token the way API Gateway's
    HttpUserPoolAuthorizer does, for the one function that does not sit
    behind it (PLANS/phase-7.md §4.3).

    The five checks, all of them required:
      * RS256 signature against the pool's JWKS
      * exp (with no leeway -- clock skew on Lambda is not a thing)
      * iss == https://cognito-idp.<region>.amazonaws.com/<pool_id>
      * aud == <user pool client id>
      * token_use == "id"

    The last one is not optional garnish: an *access* token from the same
    pool is signed by the same keys and would otherwise pass. It carries
    `client_id` rather than `aud`, so the aud check happens to catch it too
    -- checking token_use makes that intentional rather than incidental, and
    matches auth/dependencies.py's docstring, which already records that the
    API accepts id tokens only.
    """

    def __init__(self, *, region: str, user_pool_id: str, audience: str,
                 fetch: Callable[[str], bytes] | None = None) -> None: ...

    def verify(self, token: str) -> dict: ...
```

**JWKS caching, and the one thing that could DoS us.** The keys are fetched once per container into a module-level cache, exactly like `secrets.get_secret`. A token whose `kid` is unknown triggers **one** refetch (Cognito rotates signing keys), and that refetch is rate-limited to **once per 300 s per container**: without the cooldown, an attacker sending tokens with random `kid`s would make every request hit Cognito's JWKS endpoint, turning a cheap rejection into an outbound request per attempt. Tested (§13.2).

**Claims injection.** The middleware does exactly what `LocalAuthMiddleware` does, minus the trust:

```python
# src/auth/function_url.py
class FunctionUrlAuthMiddleware:
    """Pure-ASGI. Verifies the Bearer token and writes the claims into
    scope["aws.event"] in API Gateway's own shape, so
    auth/dependencies.py's claims_from_request() and get_current_user()
    are UNCHANGED and every use case below them cannot tell which app it is
    running in. On a missing or invalid token it short-circuits with a 401
    JSON body -- it never falls through to the route, because a route that
    reached get_current_user() with no claims would 401 anyway, just after
    doing work."""
```

`/health` is exempt (it is what LWA's readiness probe hits).

### 4.4 CORS, and why phase-6's bug cannot recur here

Phase-6 §16's correction is the sharpest lesson in this repo: an `ANY` route matched `OPTIONS`, a matched route **takes precedence over HttpApi's automatic CORS response**, so every preflight went to the JWT authorizer and came back `HTTP/2 401` — measured, not theorised, and invisible to every local test because compose talks to FastAPI directly.

The Function URL cannot reproduce that shape, for a structural reason:

> When CORS is configured on a Function URL, **Lambda answers the preflight itself and does not invoke the function.** There is no route table, no authorizer, and no handler in the path. The preflight cannot 401 because nothing capable of returning 401 runs.

```python
fn.add_function_url(
    auth_type=lambda_.FunctionUrlAuthType.NONE,
    invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
    cors=lambda_.FunctionUrlCorsOptions(
        allowed_origins=["*"],
        allowed_methods=[lambda_.HttpMethod.POST],
        allowed_headers=["authorization", "content-type"],
        max_age=cdk.Duration.hours(1),
    ),
)
```

`allowed_origins=["*"]` matches `main.py`'s existing `CORSMiddleware(allow_origins=["*"])` and the same justification (the CloudFront domain differs per environment). Note it is **not** paired with credentials: the browser sends the id token in an `Authorization` header, not a cookie, so `allow_credentials` is not needed and is not set — wildcard origin plus credentials is rejected by browsers anyway.

**Still measured, not assumed.** §12.2's smoke test issues a real `OPTIONS` with `Origin` and `Access-Control-Request-Headers: authorization` against the deployed URL and asserts a 2xx with `access-control-allow-origin` present and **not** a 401. That assertion exists precisely because the last three phases each shipped a bug only a real deploy caught.

### 4.5 Ordering: everything that can fail must fail before the first byte

Once the response body starts, the status line is spent. So `POST /books/{id}/chat` does all of this **before** returning the `StreamingResponse`:

| step | failure | status | code |
|---|---|---|---|
| 1. verify JWT (middleware) | missing/expired/wrong aud/access token | **401** | `UNAUTHENTICATED` |
| 2. validate body | question empty or > 2,000 chars | **422** | `QUESTION_TOO_LONG` / `QUESTION_REQUIRED` |
| 3. `_load_owned_book` | missing or **foreign** book | **404** | `BOOK_NOT_FOUND` |
| 4. `book.chunks_total > 0` | `FAILED`/`UPLOADED`/`EXTRACTING` book — nothing to ground an answer in | **409** | `NO_TEXT` |
| 5. quota `UpdateItem` conditional | daily cap reached | **429** + `Retry-After` | `QUOTA_EXCEEDED` |
| 6. read chunks + history, `resolve_context` | — | — | — |
| 7. `get_chat_model()` | `get_secret` raises (name set, secret absent/denied) | **503** | `LLM_UNAVAILABLE` |
| 8. **first byte** | everything after here is an SSE frame | 200 | — |

Step 7 is where phase-4's OQ-A correction is repaid: because the secret is read while *building* the model and before streaming, a prod stack pointed at a secret that does not exist returns one clean 503 with a fixed message — it does not half-stream and it does not leak the `ResourceNotFoundException`.

After the first byte, the only remaining failure surface is the model call itself:

```
event: error
data: {"code":"LLM_UNAVAILABLE","message":"The answer stopped early. Try asking again."}

event: done
data: {"finishReason":"ERROR"}
```

and the client keeps the partial text. **The third case has no frame at all:** if the connection drops, the reader simply ends. The client must therefore treat "reader finished without having seen `done`" as `finishReason: "TRUNCATED"` and render "Response interrupted — ask again", rather than assuming a clean end. This is the single most-forgotten branch in streaming clients and it gets its own vitest case (§13.1).

### 4.6 The wire protocol (ours, not OpenAI's)

We consume OpenAI's SSE and emit our own. Never a passthrough: proxying would put OpenAI's chunk schema, model ids and internal fields into the browser, couple the frontend to a vendor wire format, and make §5's stub impossible to make transport-identical.

```
event: meta
data: {"model":"stub","enabled":false,"reason":"NON_PROD","anchorChunk":12,
       "windowChunks":[10,11,12,13,14],"messageId":"a3f…"}

event: delta
data: {"text":"Ishmael "}

: keepalive

event: done
data: {"finishReason":"END_TURN","usage":{"inputTokens":4103,"outputTokens":312,
       "cachedInputTokens":2816},"messageId":"a3f…"}
```

- `meta` is always first and always sent, even for the stub — it is how the sidebar learns `enabled`/`reason`/`model` and which chunk the answer is about ("Answering about section 12").
- `: keepalive` comment lines every 15 s while waiting for the first upstream token, so an idle intermediary does not close the connection during a slow first token.
- `finishReason` ∈ `END_TURN | MAX_TOKENS | REFUSAL | DISABLED | ERROR`; the client adds `TRUNCATED` locally.
- `usage.cachedInputTokens` comes from OpenAI's `usage.prompt_tokens_details.cached_tokens` (§6.4) and is logged, so prefix-cache effectiveness is observable rather than assumed.

Response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`, `Connection: keep-alive`.

### 4.7 Rate limiting and abuse

| layer | mechanism | what it stops |
|---|---|---|
| Fail-fast auth | JWT verified in middleware against a module-cached JWKS, before any I/O | Anonymous flood: ~5 ms of Lambda time per rejected request, no DynamoDB, no network |
| JWKS cooldown | ≤ 1 JWKS refetch per 300 s per container | Random-`kid` tokens turning rejection into an outbound request each |
| Ownership | `_load_owned_book` before the model is even constructed | A valid token for user A spending user B's quota, or reading B's book |
| **Daily cap** | `USER#<sub>` / `CHATQUOTA#<YYYY-MM-DD>`, `ADD count :one` with `ConditionExpression="attribute_not_exists(#c) OR #c < :limit"`, default **50** | The actual money. One `UpdateItem`, atomic, no read-then-write race, and it runs *before* context resolution so an over-quota request costs one write and nothing else |
| Body cap | pydantic `max_length=2000` on `question` | A 6 MB question inflating input tokens |
| Anchor clamp | server clamps to `[0, chunks_total-1]` | A crafted `anchorChunk` reaching outside the book (it cannot — the window is sliced from a list — but clamping makes that explicit rather than incidental) |

**Not used: reserved concurrency.** Constraint 3 — this account's total Lambda concurrency is 10 and AWS rejects every possible reservation. The counter is the guard, and §13.3 asserts `Match.absent()` on the new function so nobody "fixes" this later with a six-minute deploy.

The quota item has no TTL. Deliberately: adding `time_to_live_attribute` to the table is a `storage_stack.py` change for 365 items per user per year at ~80 bytes each — 30 KB/year. Stated so it is a decision rather than an oversight.

---

## 5. The LLM: the environment gate, the key, and the stub

### 5.1 `StubChatModel` — offline, deterministic, and it *streams*

Phase-4 §0's rule, quoted so there is no ambiguity about scope: *"`SpeechSynthesizer` only talks to a real engine when `ENVIRONMENT == "prod"`. Local dev (`ENVIRONMENT=local`) and every ephemeral PR stack (`ENVIRONMENT=pr-N`) get a `StubSynthesizer` instead — no exceptions, no opt-in flag to turn real calls on in those environments."* **An LLM is an external service. The same rule, unmodified, applies.**

Phase 5's `SilentSynthesizer` is the shape of the *permitted* exception: an opt-in local generator is acceptable **only** because it is fully offline and never reaches a network. `StubChatModel` qualifies on the same terms — and unlike `SilentSynthesizer` it is not opt-in, it is the default everywhere but prod-with-a-key.

Where it differs from `StubSynthesizer`, and why:

> `StubSynthesizer` raises, and that is right for TTS: the chunk goes `FAILED`, the fan-in still completes, the book reaches `PARTIAL`, and phase-6 §9's degradation table renders it. Every part of the pipeline is still exercised.
>
> A raise-only chat stub exercises **nothing**. There would be no environment in existence — not compose, not `deploy-pr`, not `deploy-prod` (which never runs login-gated checks) — in which a single token ever crosses the Function URL. `InvokeMode: RESPONSE_STREAM`, the LWA entrypoint, the SSE re-framing, the browser's reader loop and the incremental render would all ship unexecuted. Given that the last three phases each shipped a bug only a real deploy caught, that is not a trade worth making.

```python
# infrastructure/stub_chat_model.py

class StubChatModel:
    """Returned by get_chat_model() whenever ENVIRONMENT != "prod", and in
    prod whenever OPENAI_SECRET_NAME is "" (its default everywhere).

    Streams. Never touches the network -- there is no urllib import in this
    module and §13.2 asserts it with a socket guard. The answer is built
    ENTIRELY from the resolved ChatContext, which is what makes
    "the response references book content" a real assertion about context
    resolution even in an environment with no API key (PLANS/phase-7.md
    §5.1, §13.4).
    """
    name = "stub"

    def __init__(self, reason: ChatDisabledReason) -> None: ...

    def stream(self, context: ChatContext) -> Iterator[ChatDelta]:
        if self._reason is ChatDisabledReason.NOT_CONFIGURED:
            yield from _chunked(PROD_NOT_CONFIGURED_MESSAGE)
            return
        anchor = context.anchor
        yield from _chunked(
            f"[Chat is turned off in this environment.] "
            f"You asked: {context.question} "
            f"I can see {len(context.chunks)} sections of "
            f"“{context.book_title}” around section {anchor.index}, "
            f"which begins: “{anchor.text[:80].strip()}…”"
        )
```

`_chunked` splits on word boundaries and yields ~6 words at a time, so the delta cadence resembles a real stream and the incremental-render assertions are meaningful. It sleeps for nothing — determinism beats realism here, and a sleeping stub would slow every Playwright run.

`finishReason` for both stub paths is `DISABLED`, which the UI renders differently from `END_TURN`.

### 5.2 The gate and the credential

```python
# interface/dependencies.py

def get_chat_model() -> ChatModel:
    """PLANS/phase-4.md §0's rule, applied to the LLM. The environment gate
    is checked FIRST and UNCONDITIONALLY: an OPENAI_SECRET_NAME accidentally
    set on a PR stack still cannot produce a network call, because
    OpenAiChatModel is never constructed outside prod and get_secret() is
    never reached.

    The second branch is PLANS/phase-4.md §6.4/OQ-A's correction, and it is
    the reason prod works today with no key at all: get_secret() is called
    EAGERLY while building the model, so a name pointing at a secret that
    does not exist would fail every request with ResourceNotFoundException.
    Defaulting the name to "" everywhere -- prod included -- means the only
    way to reach that call is to have deliberately created the secret and
    deliberately deployed with the context flag.
    """
    if settings.environment != "prod":
        return StubChatModel(ChatDisabledReason.NON_PROD)
    if not settings.openai_secret_name:
        return StubChatModel(ChatDisabledReason.NOT_CONFIGURED)
    return OpenAiChatModel(
        api_key=get_secret(settings.openai_secret_name),
        model=settings.openai_model,
        max_output_tokens=settings.openai_max_output_tokens,
    )
```

Three properties, each with a test in §13.2:

1. **`local`, `dev`, `pr-N` return the stub even when `openai_secret_name` is set.** Parametrized.
2. **`prod` with `openai_secret_name == ""` returns the stub.** This is the shipping configuration.
3. **`OpenAiChatModel` is constructed only for `prod` + non-empty name**, and only then is `get_secret` called at all.

`infra/app.py`, mirroring the existing `google_tts_secret_name` block verbatim:

```python
api = ApiStack(
    ...,
    # "" (unset) EVERYWHERE by default, prod included -- exactly what
    # PLANS/phase-4.md §6.4/OQ-A's correction established for the Google TTS
    # key, for exactly the same reason: get_chat_model() calls get_secret()
    # eagerly while BUILDING the model, so a prod default pointing at a
    # secret that does not exist yet would 503 every question. There is no
    # key in this account today, and prod must work without one (§5.3).
    #
    # Turning chat on is a deliberate two-step, in this order:
    #   1. aws secretsmanager create-secret --name bookloud/openai-api-key \
    #        --secret-string 'sk-...'          # out of band, by a human
    #   2. cdk deploy -c openai_secret_name=bookloud/openai-api-key
    # The value never enters this repo, this template, or a workflow.
    openai_secret_name=app.node.try_get_context("openai_secret_name") or "",
)
```

`infra/stacks/config.py` gains `OPENAI_SECRET_NAME = "bookloud/openai-api-key"` as a *documented name*, used only when the context flag is passed, alongside `ENV_OPENAI_SECRET_NAME`, `ENV_OPENAI_MODEL`, `DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"`, `ENV_CHAT_DAILY_LIMIT`, `CHAT_DAILY_LIMIT = 50`.

CDK, in `ApiStack`, identical in shape to `PipelineStack`'s Google block:

```python
if openai_secret_name:
    secret = secretsmanager.Secret.from_secret_name_v2(self, "OpenAiSecret", openai_secret_name)
    secret.grant_read(chat_fn)     # also grants kms:Decrypt where needed
```

CDK resolves a name to an ARN without reading the value; the template contains the ARN and nothing else. The **API** Lambda gets `OPENAI_SECRET_NAME` on its environment (it needs the name to compute `enabled` for `GET /books/{id}/chat`) but is deliberately **not** granted `secretsmanager:GetSecretValue` — asserted in §13.3, because "the API Lambda can read the key" would be a silent widening of the blast radius.

### 5.3 Prod with no key: exactly what the user sees

This is the shipping configuration on day one, so it is a specification, not a footnote.

| surface | behaviour |
|---|---|
| `GET /books/{id}/status`, `/chunks`, `/manifest`, `/audio`, `/resynthesize` | **Unchanged.** Reading and playback are untouched by chat's configuration. |
| `GET /books/{id}/chat` | `200` with `{"messages": [...], "chat": {"enabled": false, "reason": "NOT_CONFIGURED", "model": null, "dailyLimit": 50, "usedToday": 0}}` |
| Sidebar, on open | Renders the transcript (empty on a fresh book) plus a persistent notice: **"Chat isn't available on this deployment yet — no language-model key is configured."** The composer input and send button are **disabled**, so the user cannot spend a turn discovering it. |
| `POST /books/{id}/chat`, if called anyway | **200, a normal SSE stream.** `meta` with `enabled:false, reason:"NOT_CONFIGURED", model:null`, one short honest sentence as deltas, `done` with `finishReason:"DISABLED"`. No 4xx, no 5xx, no stack trace. |
| The one sentence | *"Chat isn't set up for this deployment yet — no language-model key is configured. You can keep reading and listening; ask again once a key is added."* |
| Persistence | The turn **is** written (`model: null`, `finishReason: "DISABLED"`), so the transcript matches the screen. It counts against the daily quota, which is harmless and keeps one code path. |
| The moment a key is added | `cdk deploy -c openai_secret_name=…`, one Lambda config update. **No frontend rebuild, no schema change, no data migration** — the sidebar re-reads `chat.enabled` on its next open and lights up. |

The property that matters: **nothing about the absence of a key is an error condition.** It is a value on a response, rendered as an affordance.

### 5.4 `OpenAiChatModel` — urllib, REST, injected transport

Shape borrowed wholesale from `infrastructure/google_tts_synthesizer.py`, which is this repo's one existing third-party HTTP adapter.

```python
# infrastructure/openai_chat_model.py

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_RETRYABLE = (429, 500, 502, 503, 504)


class OpenAiChatModel:
    """OpenAI Chat Completions with stream=true, over urllib.

    Not the `openai` SDK, for the same reason phase 4 called Google TTS's
    REST endpoint rather than google-cloud-texttospeech (PLANS/phase-4.md
    Q8): one fewer runtime dependency in an image shared by five Lambdas,
    and -- more usefully -- it reuses the injected-HTTP-callable test shape
    that already keeps the whole suite off the network
    (tests/.../test_google_tts_synthesizer.py). The cost is that retries are
    ours: exactly one, on 429/5xx, and ONLY before the first token, because
    after the first byte the client has already seen output and a silent
    replay would duplicate text on screen.

    The api_key lives in this instance and nowhere else. repr() is
    suppressed, the Authorization header is built at call time, and the only
    thing this adapter logs is (model, status, x-request-id, token counts,
    duration_ms) -- never a header, never the request body (which contains
    the reader's book), never the response body. PLANS/phase-7.md §5.5.
    """

    def __init__(self, *, api_key: str, model: str, max_output_tokens: int,
                 http: Callable[..., Any] | None = None) -> None: ...

    def __repr__(self) -> str:            # never the default dataclass repr
        return f"OpenAiChatModel(model={self._model!r})"

    def stream(self, context: ChatContext) -> Iterator[ChatDelta]: ...
```

Request body:

```json
{
  "model": "gpt-4.1-mini",
  "stream": true,
  "stream_options": {"include_usage": true},
  "max_completion_tokens": 700,
  "temperature": 0.2,
  "messages": [
    {"role": "system", "content": "<instructions>\n\n<book window>"},
    {"role": "user", "content": "…prior question…"},
    {"role": "assistant", "content": "…prior answer…"},
    {"role": "user", "content": "…this question…"}
  ]
}
```

- `max_completion_tokens`, not the deprecated `max_tokens`, so a future model swap does not 400.
- `stream_options.include_usage` makes the last frame before `[DONE]` carry `usage` including `prompt_tokens_details.cached_tokens` — which is how §6.4's prefix-cache claim becomes observable instead of theoretical.
- `temperature: 0.2` — grounded extraction, not prose generation.

Parsing OpenAI's stream. It is **not** the same framing as ours: there are no `event:` lines, only `data:` lines and a literal `data: [DONE]` sentinel.

```python
def _iter_frames(response) -> Iterator[dict]:
    """OpenAI SSE: `data: {json}` lines, blank-line separated, terminated by
    the literal `data: [DONE]`. Comment lines (`:`) and blank lines are
    skipped. A frame that does not parse as JSON is skipped with a WARNING
    and does NOT abort the stream -- a single malformed keepalive should not
    lose an answer already half-rendered."""
```

Mapping to our frames: `choices[0].delta.content` (which is absent on the first and last frames) → `delta`; `choices[0].finish_reason` → `stop→END_TURN`, `length→MAX_TOKENS`, `content_filter→REFUSAL`, anything else → `END_TURN`; the trailing `usage` object → the `done` frame.

`urllib.request.urlopen` returns an `http.client.HTTPResponse`, which is a buffered reader over the socket: `readline()` returns as soon as a newline arrives rather than waiting to fill a buffer, so iterating it line-by-line is genuinely incremental. Stated because "does urllib actually stream?" is the reasonable first objection to this choice, and the answer is yes.

### 5.5 Key hygiene — seven rules, each a test

A key in a CloudWatch log line is leaked exactly as thoroughly as one in a template.

1. **The value never enters `Settings`.** `config.py` carries `openai_secret_name` (a name) and `openai_model`. There is no `openai_api_key` field, so no settings dump, `/docs` schema, or `repr(settings)` can ever contain it.
2. **The value never enters a Lambda environment variable.** Only the *name* is set, and only on the two functions that need it (`ChatFunction` to read it, `ApiFunction` to test it for emptiness).
3. **`__repr__` is overridden** on `OpenAiChatModel`. Test: `repr(model)` and `str(model)` do not contain the key.
4. **Nothing logs headers or bodies.** On an upstream error the adapter logs `status` and OpenAI's `x-request-id` header — **not** the response body, which can echo request content, and not the request body, which contains the reader's book.
5. **The user-facing error message is a fixed string.** `ChatUnavailable`'s message is a constant; the upstream body never reaches the browser.
6. **A log-scrubbing regression test** (§13.2): run a full fake-transport stream with `caplog` at DEBUG and assert no record's `getMessage()` contains the key sentinel.
7. **A synth test** (§13.3) asserts `OPENAI_SECRET_NAME` is `""` in every environment by default *and* that no CFN resource property, output, or environment variable in any stack matches a key-shaped pattern (`sk-`). Cheap, and it is the check that would have caught someone "temporarily" hardcoding a key.

---

## 6. Context resolution

### 6.1 The window

The real shapes, quoted:

```python
# domain/chunking.py
DEFAULT_TARGET_CHARS = 1800
DEFAULT_MAX_CHARS = 2600
```

```python
# domain/chunk.py  (the fields the window uses)
@dataclass
class Chunk:
    book_id: str
    index: int          # ordinal from extraction; identity is (book_id, index)
    user_id: str
    text: str
    char_start: int
    char_end: int
    ...
    page_start: int = 0   # 1-based, inclusive
    page_end: int = 0
```

The reading pane already holds every chunk (`GET /books/{id}/chunks`, phase-6 §10.1), and the manifest's `segments[].s`/`e` are book-global character offsets — but the chat context is built **server-side from the chunk rows**, not from the manifest, because a `PARTIAL` book with no audio has an empty `segments` array and must still be chattable. The manifest is the *audio* timeline; the chunk table is the *text*.

```python
# domain/chat.py  (PURE -- no boto3, no urllib, no openai)

CONTEXT_NEIGHBOURS = 2        # anchor +/- 2  -> up to 5 chunks
MAX_CONTEXT_CHARS = 12_000    # hard ceiling on the assembled window
MAX_HISTORY_PAIRS = 3         # 3 user/assistant pairs -> 6 messages
MAX_HISTORY_CHARS = 1_000     # per message, tail-truncated
MAX_QUESTION_CHARS = 2_000

@dataclass(frozen=True)
class ContextChunk:
    index: int
    text: str
    page_start: int
    page_end: int
    is_anchor: bool

@dataclass(frozen=True)
class ChatContext:
    book_id: str
    book_title: str
    anchor_chunk: int
    chunks: tuple[ContextChunk, ...]
    history: tuple[ChatMessage, ...]
    question: str

    @property
    def anchor(self) -> ContextChunk: ...
    @property
    def window_indexes(self) -> tuple[int, ...]: ...


def resolve_context(
    *, book: Book, chunks: Sequence[Chunk], history: Sequence[ChatMessage],
    anchor_chunk: int | None, question: str,
) -> ChatContext:
    """PLANS/phase-7.md §6.1. Pure and total: no I/O, and it raises only for
    a book with zero chunks (which the controller rejects with a 409 first)."""
```

Five rules, each with a reason:

1. **Clamp, don't trust.** `anchor = min(max(anchor_chunk or 0, 0), len(chunks) - 1)`. `None` → 0.
2. **Window = `chunks[max(0, a-2) : a+3]`, clipped at the ends, not shifted.** At chunk 0 the user gets 3 chunks, not 5 shifted forward. Deliberate: shifting would silently pull in text the reader has not reached, and a reader mid-novel would notice a "no spoilers" promise being broken by the tool that made it. Same at the tail.
3. **Ceiling: 12,000 chars.** `DEFAULT_MAX_CHARS = 2600` means five chunks can reach 13,000. Over the ceiling, drop neighbours **farthest-first, symmetrically** until it fits; the anchor is never dropped and is truncated (head-kept) only in the pathological case where it alone exceeds the ceiling.
4. **History: last 6 messages** (`MAX_HISTORY_PAIRS * 2`) by `created_at` ascending, each truncated to 1,000 chars with a `…` marker. Then two normalisations that exist because the transcript can be ragged: **drop a trailing `user` message that has no `assistant` after it** (a turn whose model call died), and **drop a leading `assistant`** (the API requires the first message to be `user`).
5. **Question: `MAX_QUESTION_CHARS`**, enforced by pydantic at the wire and re-asserted here so the pure function is total on its own.

**Why ±2 and not more.** A chunk is ~1,800 chars ≈ 300 words ≈ 2 minutes of narration. Five chunks ≈ 1,500 words ≈ 10 minutes of listening — a scene, a section, an argument. ±1 (3 chunks, ~6 min) too often cuts the paragraph the question is about; ±4 (9 chunks, ~4,300 tok) nearly doubles the per-question cost for context the question rarely reaches. **OQ-4.**

**What this deliberately is not.** There is no retrieval over the whole book — no embeddings, no keyword index, no vector store. A question about chapter 1 asked while listening to chapter 20 gets an honest "that isn't in the section you're on." That is a *stated capability boundary*, encoded in the system prompt, not a bug. **OQ-9.**

### 6.2 Anchoring — the case that matters is the one without audio

`anchoredChunk` "comes from playback position" is true in prod for a `READY` book and false everywhere else. In `deploy-pr` **every** book is `PARTIAL`/`NO_AUDIO` (phase-4 §0) and there is nothing playing; in local compose the user is usually paused while typing a question.

So the client resolves the anchor in priority order:

```ts
// hooks/useReadingAnchor.ts
/** Where the reader is, in chunk indexes.
 *
 *  1. `usePlayback().state.chunkIndex` when >= 0 -- authoritative while audio
 *     is loaded, playing or paused (phase-6 §7.4 keeps it updated from the rAF
 *     loop, so a paused player still reports the last spoken chunk).
 *  2. Otherwise the topmost `ChunkParagraph` intersecting the viewport, from a
 *     single IntersectionObserver over the reading pane.
 *  3. Otherwise 0.
 *
 *  Case 2 is NOT a fallback. It is the normal case: no book in any PR
 *  environment has audio at all, and locally the player is usually paused
 *  while the user types. PLANS/phase-7.md §6.2. */
export function useReadingAnchor(chunkCount: number): number;
```

Implementation notes: one `IntersectionObserver` with `rootMargin: "-10% 0px -70% 0px"` so the "current" paragraph is the one near the top of the reading area rather than whichever is largest; it writes to a **ref**, not state (phase-6 Q11's rule — scrolling must not re-render the reading pane), and `useChat` reads the ref at send time. `ReadingPane`/`ChunkParagraph` gain a `data-chunk-index` attribute, which is the observer's only coupling to the DOM.

Server-side: the anchor is clamped and the resolved window is echoed back in the `meta` frame, so the sidebar can render "Answering about section 12" and a Playwright test can assert which section was used without guessing.

### 6.3 The prompt

Two pure render functions in `infrastructure/openai_prompt.py` (adapter-shaped, so not in `domain/`, but pure and directly unit-tested):

```python
def render_messages(context: ChatContext) -> list[dict]: ...
```

System content — one string, stable instructions first, then the book window:

```
You are a reading companion for one book. The reader is partway through it and
has asked about the section they are on.

Answer from the book excerpt below. It is a window around where the reader
currently is -- a few thousand words, not the whole book. When the excerpt does
not contain the answer, say so plainly and say what it does cover; if you add
anything from outside the excerpt, label it as outside the book.

Do not reveal or summarise anything from later in the book than the excerpt.

Keep answers to the length the question needs, usually two or three sentences.
Quote the book only when the exact wording is the point.

<book title="Moby-Dick" sections="10-14" of="330">
<section index="10" pages="41-43">…1800 chars…</section>
<section index="11" pages="43-45">…</section>
<section index="12" pages="45-47" reading-here="true">…</section>
<section index="13" pages="47-49">…</section>
<section index="14" pages="49-51">…</section>
</book>
```

Four properties of this prompt worth naming:

- **No pressure language.** No `CRITICAL`, no `MUST`, no `NEVER`. It states what to do at normal volume.
- **The scope boundary is explicit** ("not the whole book", "do not reveal anything later"), because that boundary is a real product decision (§6.1) and the model is the only thing that can enforce it in prose.
- **Length is stated once**, positively.
- **No self-verification instruction.** Asking a model to double-check its answer buys latency and tokens on a two-sentence reading-comprehension answer.

The XML-ish framing is deliberate: it makes the section boundaries and the `reading-here` marker unambiguous at a cost of ~12 tokens per section.

### 6.4 Prefix caching, for free

OpenAI applies automatic prompt caching to prompts above ~1,024 tokens, keyed on the **longest common prefix** — no request-side parameter. That makes the ordering above load-bearing rather than aesthetic:

```
[ system: instructions + book window ]   ~2,810 tok   <- stable while the anchor doesn't move
[ history messages ]                     ~1,000 tok   <- grows by one pair per turn
[ this question ]                          ~260 tok   <- volatile, always last
```

Consecutive questions at the same anchor — the overwhelmingly common pattern, since the user pauses, asks, reads, asks again — reuse the ~2,810-token prefix at the cached rate. The saving is modest (§6.5) but it costs exactly zero code: it is a consequence of putting the stable content first, which we would do anyway. `usage.prompt_tokens_details.cached_tokens` is surfaced in the `done` frame and logged, so if the hit rate is ever zero we will see it rather than assume it.

The anchor moving invalidates the prefix. Not worth engineering around (quantising the window to a stride would trade real answer quality for pennies), and stated here so nobody later reports it as a bug.

### 6.5 Token budget and cost, with real numbers

Basis: `DEFAULT_TARGET_CHARS = 1800`; English prose runs ~3.8 chars/token on modern BPE tokenizers, so **~475 tokens per chunk**.

| component | chars | tokens |
|---|---:|---:|
| System instructions | ~1,100 | ~290 |
| `<book>` / `<section>` markup (5 sections) | ~330 | ~85 |
| Book window: 5 chunks × 1,800 | 9,000 | **~2,370** |
| History: 6 messages, ~650 chars avg | ~3,900 | ~1,030 |
| Question | ~400 | ~105 |
| **Input total (typical)** | | **~3,880** |
| Input total (worst case: 12,000-char window + 6 × 1,000-char history) | | **~5,600** |
| Output (`max_completion_tokens = 700`, typical answer 2-3 sentences) | | **~330** |

Cost at `gpt-4.1-mini` (**verify list prices at implementation time**; the figures below use $0.40 / 1M input, $1.60 / 1M output, cached input at 25% of input):

| scenario | input | output | **per question** |
|---|---:|---:|---:|
| Cold prefix (first question at a new anchor) | 3,880 × $0.40/M = $0.00155 | 330 × $1.60/M = $0.00053 | **$0.0021** |
| Warm prefix (follow-up at same anchor: 2,810 cached) | (1,070 × $0.40 + 2,810 × $0.10)/M = $0.00071 | $0.00053 | **$0.0012** |
| Worst case | 5,600 × $0.40/M = $0.00224 | 700 × $1.60/M = $0.00112 | **$0.0034** |

At a realistic mix (one cold + two warm per sitting), **~$0.0015/question**. Twenty questions a day → **~$0.03/day, ~$0.90/month**. The daily cap of 50 bounds the worst case at **~$0.17/day, ~$5/month** — which is the number that actually matters, because it is what a bug or a stuck client could cost.

Comparison for **OQ-2**:

| model | per question (cold) | 20 q/day | at the 50/day cap |
|---|---:|---:|---:|
| `gpt-4o-mini` | ~$0.00078 | ~$0.47/mo | ~$1.17/mo |
| **`gpt-4.1-mini`** (recommended) | ~$0.0021 | ~$1.26/mo | ~$3.15/mo |
| `gpt-4.1` | ~$0.0104 | ~$6.24/mo | ~$15.60/mo |

`gpt-4.1-mini` is the recommendation: this task is grounded extraction and summarisation over ~2,400 tokens of supplied text, which is squarely where the mini tier is strong, and the delta to the full model is ~5× cost for a difference the reader is unlikely to notice on "what is this section about?". `gpt-4o-mini` saves another $0.79/month and is noticeably weaker at following the "say so when the excerpt doesn't cover it" instruction — which is the one behaviour that makes this feature trustworthy.

---

## 7. Data model and persistence

### 7.1 Item shapes

```
PK  BOOK#<bookId>
SK  CHAT#<createdAt>#<msgId>          e.g. CHAT#2026-08-12T09:15:03.123456+00:00#a3f1...
```

| attribute | type | notes |
|---|---|---|
| `role` | `"user"` \| `"assistant"` | spec field |
| `content` | str | spec field |
| `anchoredChunk` | int | spec field — the *clamped* anchor, not the client's |
| `createdAt` | str | spec field, ISO-8601 UTC; also the SK prefix |
| `userId` | str | ⊕ denormalised owner sub — see §7.3 |
| `positionMs` | int \| None | user messages only; the durable record of where playback was |
| `model` | str \| None | assistant only; `null` for stub turns |
| `finishReason` | str \| None | assistant only |
| `inputTokens` / `outputTokens` / `cachedInputTokens` | int \| None | assistant only; cost visibility without a metrics stack |

```
PK  USER#<sub>
SK  CHATQUOTA#<YYYY-MM-DD>
count  int
```

`keys.py` additions, alongside the existing `CHUNK_INDEX_WIDTH` warning:

```python
CHAT_PREFIX = "CHAT#"
CHAT_QUOTA_PREFIX = "CHATQUOTA#"

def sk_chat(created_at: str, message_id: str) -> str:
    """``CHAT#<createdAt>#<msgId>`` -- a REFINEMENT of
    IMPLEMENTATION_PLAN.md's ``CHAT#<msgId>`` (PLANS/phase-7.md §7.1, OQ-6).

    A bare uuid4 sorts randomly, so "the last six messages" would mean
    reading the whole partition and sorting client-side on every question.
    The ISO-8601 UTC prefix makes the SK chronological, so recent history is
    one Query(ScanIndexForward=False, Limit=N). The uuid suffix keeps two
    messages written in the same microsecond distinct.

    ``created_at`` MUST be the fixed-width form SystemClock produces
    (``datetime.now(UTC).isoformat()``); a value that drops the microseconds
    or the offset would sort wrongly against one that doesn't.
    test_keys.py asserts monotonicity and width."""
```

**Fixed-width caveat, named.** `datetime.isoformat()` omits microseconds when they are exactly zero — a 1-in-a-million event that would produce a 26-character prefix among 32-character ones and sort it wrongly. The mapper therefore formats with an explicit `strftime`-style width rather than calling `isoformat()` bare, and `test_keys.py` includes the zero-microsecond case. Small, real, and exactly the kind of thing that shows up once a year and is impossible to diagnose.

### 7.2 When writes happen

**Both messages, once, after the stream ends**, inside the generator's `finally`:

```python
async def _body() -> AsyncIterator[bytes]:
    yield sse_frame("meta", meta)
    try:
        for delta in model.stream(context):
            answer.append(delta.text)
            yield sse_frame("delta", {"text": delta.text})
    except ChatStreamError as exc:
        finish = FinishReason.ERROR
        yield sse_frame("error", {"code": exc.code, "message": exc.public_message})
    finally:
        # Runs on normal completion, on an exception, and on GeneratorExit
        # (client disconnect). Persisting here rather than up front is what
        # keeps a dead model call from leaving a dangling user message with
        # no answer -- PLANS/phase-7.md §7.2 / Q12.
        if answer:
            chat_repository.save_turn(user_message, assistant_message)
    yield sse_frame("done", {...})
```

Consequences, stated:

- **Nothing is written when zero tokens were produced.** The transcript never contains an unanswered question, so §6.1's "drop a trailing unanswered user message" rule is a belt-and-braces normalisation rather than the primary defence.
- **A partial answer *is* written**, with `finishReason: "ERROR"` or `"TRUNCATED"`. The persisted transcript must match what the user saw; silently dropping text that is still on their screen is worse than storing it truncated.
- **A hard Lambda crash mid-stream loses the turn.** Named cost of writing once at the end. Equivalent to a dropped connection from the user's point of view, and the alternative (write-before, delete-on-failure) buys a rarer failure mode at the price of a delete path and a dangling-item state.

### 7.3 Authorization — the same rule as chunks, and its one real limit

`CHAT#` items are keyed `PK=BOOK#<bookId>`, which structurally cannot carry ownership — identical to `CHUNK#`. So `ChatRepository` gets `ChunkRepository`'s exact contract, in its own docstring:

```python
class ChatRepository(Protocol):
    """``book_id`` must already have been authorized via
    ``BookRepository.get(user_id, book_id)``. This port performs no access
    control -- the same rule ``ChunkRepository`` carries, for the same
    structural reason (PLANS/phase-7.md §7.3)."""
```

`application/use_cases.py`'s `_load_owned_book` stays the single gate, and every chat use case starts with it. §13.2 adds a direct test: `ListBookChat` and `AskBookQuestion` raise `NotFoundError` (never `ForbiddenError`, never a 403) for a book id belonging to another user, **even though the chat items for that book exist and the query would succeed**. That test is the one that fails if someone later "optimises" the ownership read away.

**The limit, stated plainly.** Chat is per-book, and per-book is per-user *only because a book has exactly one owner today* (`USER#<sub>` / `BOOK#<bookId>`; there is no sharing, no GSI, no second owner). The day books become shareable, `BOOK#<bookId>` + `CHAT#…` would mean two readers see each other's conversations. Mitigation shipped now, at a cost of one attribute: **every chat item carries `userId`**, so a future `FilterExpression`/partition split is a code change rather than a backfill. No GSI is added — a filter over ≤50 items is free and a GSI is not.

### 7.4 What is not added

- **No TTL on the table.** §4.7's arithmetic: 30 KB/year of quota items.
- **No GSI.** Every access pattern is a `Query` on a known partition.
- **No change to `storage_stack.py`.** Stated explicitly, as phase 6 did, so nobody goes looking.

---

## 8. Frontend

### 8.1 Components and files

```
frontend/
├── lib/chat.ts                    ⊕  types, parseSseStream (PURE), streamChat
├── hooks/useChat.ts               ⊕  transcript + streaming state
├── hooks/useReadingAnchor.ts      ⊕  §6.2
├── components/ChatSidebar.tsx     ⊕  the panel: notice, transcript, composer
├── components/ChatTurn.tsx        ⊕  one message bubble (React.memo)
├── components/ChatComposer.tsx    ⊕  textarea + send, disabled states
├── components/ReaderView.tsx      Δ  three-column layout, sidebar toggle
├── components/PlayerBar.tsx       Δ  chat toggle button
├── components/ReadingPane.tsx     Δ  data-chunk-index on each paragraph
└── lib/books.ts                   Δ  listChat(), clearChat()
```

Layout: the app shell (`app/(app)/layout.tsx`) is untouched. `ReaderView` becomes

```tsx
<main className="flex h-screen flex-1 flex-col">
  <div className="flex min-h-0 flex-1">
    <div className="flex-1 overflow-y-auto …">{/* notice + ReadingPane */}</div>
    {chatOpen ? <ChatSidebar bookId={bookId} anchorRef={anchorRef} /> : null}
  </div>
  {player === "absent" ? null : <PlayerBar … />}
</main>
```

so the player bar stays full width and the chat panel scrolls independently of the text.

**Opening behaviour, refined.** `IMPLEMENTATION_PLAN.md` says the sidebar *"opens during playback"*. Taken literally that makes chat unreachable in every environment CI can run, because no book there has audio. Refinement: **a toggle in the player bar (and `c` as a keyboard shortcut), persisted in `localStorage`, which auto-opens once the first time playback starts and never fights the user afterwards.** Same shape as phase-6 OQ-6's playback-rate preference. **OQ-10.**

### 8.2 State — still no store

Phase-6 Q12 deferred this decision with a condition: *"revisit **then**, with a concrete problem, rather than pre-emptively now."* The concrete problem turns out to be one array of messages and one streaming buffer, read by one component tree that is already a child of the reader. There is no cross-cutting invalidation: nothing outside `ChatSidebar` reads the transcript, and the only thing chat reads from outside is a chunk index, through a ref.

So: **`useChat` + React state, no store, no new dependency.** The one borrowed pattern is phase 6's, and for the same reason:

```ts
// deltas land in a ref; state flushes on rAF
bufferRef.current += event.text;
if (frameRef.current === null) {
  frameRef.current = requestAnimationFrame(() => {
    frameRef.current = null;
    setStreamingText(bufferRef.current);
  });
}
```

A fast model can emit deltas well above 60/s; without this, `setState` per delta re-renders the transcript at the token rate. `ChatTurn` is `React.memo`'d on `(id, content, finishReason)` so completed turns never re-render while a new one streams — the same render-count assertion phase 6 used for `ChunkParagraph` (§13.1).

### 8.3 `lib/chat.ts` — the parser is the part that must be pure

```ts
export type ChatStreamEvent =
  | { type: "meta";  model: string | null; enabled: boolean;
      reason: ChatDisabledReason | null; anchorChunk: number;
      windowChunks: number[]; messageId: string }
  | { type: "delta"; text: string }
  | { type: "done";  finishReason: FinishReason; usage?: ChatUsage; messageId: string }
  | { type: "error"; code: string; message: string };

/** Incremental SSE parser. Pure, synchronous, and the reason the whole
 *  streaming client is unit-testable without a network or a browser.
 *
 *  Must survive, because all of these happen on a real connection:
 *   - a frame split across two network chunks (mid-JSON, mid-`data:` prefix)
 *   - several complete frames in one chunk
 *   - `\r\n` line endings
 *   - `:` keepalive comment lines
 *   - an unknown `event:` type (ignore it, do not throw -- forward compat) */
export function createSseParser(): (chunk: string) => ChatStreamEvent[];

/** POST + read. Rejects only for pre-stream failures (the JSON error body is
 *  parsed and rethrown as a typed error with its `code`); once the stream is
 *  open it resolves and reports everything through callbacks.
 *
 *  Ends WITHOUT calling onDone if the connection drops -- the caller must
 *  treat "resolved but no done" as TRUNCATED (§4.5). Named here because
 *  every streaming client forgets it. */
export async function streamChat(
  bookId: string, body: AskRequest,
  handlers: { onMeta; onDelta; onDone; onError }, signal: AbortSignal,
): Promise<void>;
```

`streamChat` is the second exception to "everything goes through `authFetch`" — it attaches the same `Bearer` token from `lib/auth.ts` but posts to `chatUrl(path)` rather than `apiUrl(path)`, because the Function URL is a different host. (The first exception, phase-6 §3, was the presigned audio URL, which carries *no* header.) `lib/api.ts` gains:

```ts
export const chatUrl = (path: string): string =>
  `${(process.env.NEXT_PUBLIC_CHAT_BASE_URL ?? "http://localhost:8001").replace(/\/$/, "")}${path}`;
```

A 401 from the chat host clears the session and bounces to `/login`, exactly as `authFetch` does — the same handling, not a second copy of the policy: `streamChat` calls the existing helper for that branch.

### 8.4 Rendering the transcript

- User turns render plain text. Assistant turns render **plain text with paragraph breaks**, not Markdown — adding a Markdown renderer is a new dependency (Q20 says no) and the system prompt asks for two or three sentences. Named limitation: a model that emits a bulleted list will render as literal `- ` lines. Acceptable; revisit if it happens.
- The streaming turn shows a caret and, once `done` arrives with `usage`, nothing further (token counts are logged server-side, not shown).
- `meta.anchorChunk` renders as a small "about section 12" label on the answer, and clicking it calls `playback.seekToChunk(12)` — reusing the phase-6 affordance, so a question you asked yesterday is a bookmark.
- Notices, driven by `chat.enabled` / `chat.reason` / `finishReason`:

| condition | notice | composer |
|---|---|---|
| `enabled: false, reason: "NOT_CONFIGURED"` (prod, no key) | "Chat isn't available on this deployment yet — no language-model key is configured." | **disabled** |
| `enabled: false, reason: "NON_PROD"` (local / PR) | "Chat is turned off in this environment. Answers are placeholders." | enabled |
| `usedToday >= dailyLimit` | "You've reached today's question limit ({n}). It resets at midnight UTC." | disabled |
| `finishReason: "ERROR"` | inline chip under the partial: "The answer stopped early. Ask again." | enabled |
| `finishReason: "TRUNCATED"` | inline chip: "Response interrupted." | enabled |
| `finishReason: "MAX_TOKENS"` | inline chip: "Answer cut off — ask a narrower question." | enabled |
| book `status` non-terminal with `chunksTotal === 0` | "Chat becomes available once the text is extracted." | disabled |

The second row is why the PR-environment Playwright spec can actually ask a question: the stub environment leaves the composer live.

---

## 9. Infrastructure

### 9.1 What changes, and what deliberately does not

| file | change |
|---|---|
| `backend/Dockerfile` | ⊕ `lambda-stream` stage (§9.2) |
| `infra/stacks/api_stack.py` | ⊕ `ChatFunction`, its Function URL, the optional secret grant, `ChatUrl` output; Δ `ApiFunction` env gains `OPENAI_SECRET_NAME`/`CHAT_DAILY_LIMIT` |
| `infra/stacks/config.py` | ⊕ `ENV_OPENAI_SECRET_NAME`, `ENV_OPENAI_MODEL`, `ENV_OPENAI_MAX_OUTPUT_TOKENS`, `ENV_CHAT_DAILY_LIMIT`, `ENV_COGNITO_USER_POOL_ID`, `ENV_CHAT_BASE_URL`, and the defaults |
| `infra/stacks/frontend_stack.py` | Δ one new constructor kwarg `chat_base_url` → one env var, mirroring `api_base_url` |
| `infra/app.py` | Δ `openai_secret_name` context read; `chat_base_url=api.chat_url` into `FrontendStack` |
| **`infra/stacks/storage_stack.py`** | **unchanged** — no TTL, no GSI, no new bucket (§7.4) |
| **`infra/stacks/pipeline_stack.py`** | **unchanged** — no new queue, no new Lambda, no ESM change |
| **`infra/stacks/auth_stack.py`** | **unchanged** — the pool and client already exist; the chat function only reads their ids |
| **The HTTP API's 9 routes** | **unchanged**, including the deliberate absence of `OPTIONS` on `/{proxy+}` (phase-6 §16). §13.3 keeps that assertion. |

### 9.2 The `lambda-stream` image stage

```dockerfile
# --- lambda-stream (response streaming; PLANS/phase-7.md §9.2) --------------
#
# FROM lambda, so this is the SAME dependency set and the SAME src/ -- the
# pushed delta is one small layer, not a second full image.
#
# The adapter is HERE and NOT in the `lambda` stage, and that is load-bearing:
# anything in /opt/extensions/ is started as an external extension for EVERY
# function built from that image. Put it in `lambda` and the adapter would
# come up alongside the Runtime Interface Client on ExtractFunction,
# SynthesizeFunction, StitchFunction and ApiFunction, with both polling the
# Runtime API. Do not "simplify" this by moving the COPY up.
FROM lambda AS lambda-stream

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_ASYNC_INIT=true \
    PYTHONPATH=/var/task

# The base image's ENTRYPOINT is the RIC. Clear it: with the adapter present,
# the container's job is to be a web server, and the adapter owns the Runtime
# API loop.
ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "src.chat_app:app", "--host", "0.0.0.0", "--port", "8080"]
```

Two named risks, both verified by the first `deploy-pr` run rather than by any local test:

1. **`uvicorn` is installed under `${LAMBDA_TASK_ROOT}` by `uv pip install --target`, not onto `PATH`.** Hence `python3 -m uvicorn` and an explicit `PYTHONPATH`. If the module is not importable the container fails its readiness check and the Function URL 502s — loud, not silent.
2. **`AWS_LWA_ASYNC_INIT`** lets the adapter start returning before the app is fully warm on a cold start; combined with the readiness path, the failure mode is a slow first request rather than a dropped one.

CDK builds it with `target="lambda-stream"` — `DockerImageAsset`'s hash includes the target, so this publishes a distinct image rather than colliding with the shared one.

### 9.3 `ApiStack` additions

```python
chat_fn = lambda_.DockerImageFunction(
    self, "ChatFunction",
    code=lambda_.DockerImageCode.from_image_asset(backend_dir, target="lambda-stream"),
    memory_size=512,        # I/O-bound: one DynamoDB Query pair + one HTTPS stream
    timeout=cdk.Duration.seconds(120),
    environment={
        Config.ENV_ENVIRONMENT: environment,
        Config.ENV_GIT_SHA: git_sha,
        Config.ENV_TABLE_NAME: table.table_name,
        Config.ENV_COGNITO_USER_POOL_ID: user_pool.user_pool_id,
        Config.ENV_COGNITO_CLIENT_ID: user_pool_client.user_pool_client_id,
        Config.ENV_COGNITO_REGION: self.region,
        Config.ENV_OPENAI_SECRET_NAME: openai_secret_name,   # "" unless -c
        Config.ENV_OPENAI_MODEL: Config.DEFAULT_OPENAI_MODEL,
        Config.ENV_OPENAI_MAX_OUTPUT_TOKENS: str(Config.OPENAI_MAX_OUTPUT_TOKENS),
        Config.ENV_CHAT_DAILY_LIMIT: str(Config.CHAT_DAILY_LIMIT),
        Config.ENV_LOG_LEVEL: "INFO",
    },
    # NO reserved_concurrent_executions -- same account-quota wall as every
    # other function here (PLANS/phase-4.md §0's §6.3 correction). §13.3
    # asserts Match.absent() so re-adding it fails a unit test instead of a
    # six-minute deploy.
)

table.grant_read_write_data(chat_fn)   # Query CHUNK#/CHAT#, UpdateItem quota, PutItem turns
# No S3 grant, no SQS grant -- asserted in §13.3, because the streaming
# function is the one with a public URL and its blast radius should be
# exactly "this user's books".

if openai_secret_name:
    secretsmanager.Secret.from_secret_name_v2(self, "OpenAiSecret", openai_secret_name) \
        .grant_read(chat_fn)

self.chat_url = chat_fn.add_function_url(
    auth_type=lambda_.FunctionUrlAuthType.NONE,
    invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
    cors=lambda_.FunctionUrlCorsOptions(...),          # §4.4
)
cdk.CfnOutput(self, "ChatUrl", value=self.chat_url.url)
```

`ApiFunction` also gains `OPENAI_SECRET_NAME` and `CHAT_DAILY_LIMIT` on its environment — it needs both to compute `chat.enabled` / `chat.dailyLimit` for `GET /books/{id}/chat` — and **no** secret grant.

### 9.4 Concurrency arithmetic (constraint 3)

The account's total Lambda concurrency is **10, per region**, and AWS rejects every possible `reserved_concurrent_executions` value.

| function | concurrent, worst case | when |
|---|---:|---|
| `SynthesizeFunction` | 2 | ESM `max_concurrency=2`, only while a book synthesizes |
| `ExtractFunction` | 1 | one book at a time |
| `StitchFunction` | 2 | ESM `max_concurrency=2` |
| `ApiFunction` | 1 | one user, polling at 2-5 s |
| `FrontendFunction` (SSR) | 1 | one navigation at a time |
| **`ChatFunction`** ⊕ | **1** | one user; the composer is disabled while a stream is open, so a second concurrent question requires a second tab |
| **total** | **8 / 10** | |

The 5 pipeline executions exist only while a book is processing, and chat during processing is a real scenario (the text is readable from `EXTRACTED`), so 8 is the honest worst case rather than a theoretical one. Headroom: 2.

Two things keep it there: the UI serialising questions, and the daily cap bounding a runaway client to 50 invocations. Neither is infra-enforced, because infra enforcement (reserved concurrency) is unavailable on this account. Stated as a known ceiling rather than a guarantee.

### 9.5 Dependencies

`backend/pyproject.toml` gains exactly one runtime dependency:

```toml
"pyjwt[crypto]>=2.9",
```

There is no stdlib RSA, so verifying a Cognito RS256 token needs a crypto library; `cryptography` (~4 MB manylinux wheel) lands in an image that already carries PyMuPDF and edge-tts. Deliberately **not** added: the `openai` SDK (Q6/§5.4). Deliberately **not** added to the frontend: anything at all.

Note the dependency lands in the shared `lambda` stage, so Extract/Synthesize/Stitch/Api all get slightly larger images. Accepted; the alternative (a separate dependency set for one function) is exactly the split `pipeline_stack.py`'s comment already explains this repo avoids.

---

## 10. Local dev

### 10.1 A `chat` compose service

The chat app is a **separate process on a separate port** locally, mirroring the deployed topology rather than papering over it:

```yaml
  # The streaming chat app. A SEPARATE service, not another router on
  # `backend`, because in AWS it is a separate Lambda behind a Function URL
  # (PLANS/phase-7.md §4.2) and a local topology that hides that would hide
  # exactly the class of bug this split exists to surface. uvicorn streams
  # natively, so the SSE path here is real -- what it does NOT exercise is
  # the Lambda Web Adapter, which only `deploy-pr` can prove (§12.3).
  chat:
    build: { context: ./backend, target: dev }
    command: ["uv", "run", "uvicorn", "src.chat_app:app", "--host", "0.0.0.0", "--port", "8001"]
    ports: ["8001:8001"]
    environment:
      - ENVIRONMENT=local          # -> StubChatModel(NON_PROD), unconditionally
      - TABLE_NAME=bookloud-local
      - AWS_ENDPOINT_URL=http://localstack:4566
      - CHAT_DAILY_LIMIT=50
      # No OPENAI_SECRET_NAME. Setting one would change nothing -- the
      # environment gate is checked first -- and its absence says so.
```

`make up` starts it; `frontend` gains the build arg `NEXT_PUBLIC_CHAT_BASE_URL=http://localhost:8001`.

`--reload` is deliberately omitted on this service: a reload mid-stream drops the connection, which looks exactly like the `TRUNCATED` bug we are trying to be able to see.

### 10.2 `Makefile`

- `up` adds `chat` to the service list.
- `smoke` passes `--chat-url http://localhost:8001`.
- `e2e-local` is unchanged (it calls `up` and `ui`).

---

## 11. Deferred from earlier phases

Nothing. Phase 6 closed its own carry-overs (audio delivery, `/resynthesize`, the upload flow, the CORS/OPTIONS fix). Phase 5's residual hole — a book whose completing chunk's stitch publish exhausts its retries and sits at 100% forever — remains phase 8's, unchanged and unaffected by this phase.

---

## 12. The smoke test

### 12.1 What it can honestly assert

`local/smoke_test.py` is **stdlib-only**. New argument `--chat-url` (default `http://localhost:8001`; `deploy-pr` passes the `ChatUrl` stack output).

### 12.2 `check_chat(api_url, chat_url, book_id, id_token, chunks)`

Runs in every environment the smoke test reaches, because the stub is present in all of them.

1. **`GET {api_url}/books/{id}/chat` → 200**, `messages == []`, `chat.enabled is False`, `chat.reason == "NON_PROD"` (compose and `pr-N`), `chat.dailyLimit == 50`.
2. **`POST {chat_url}/books/{id}/chat` with no `Authorization` → 401.** *The single assertion that proves in-Lambda JWT verification is wired at all.* An unauthenticated POST to a `NONE`-auth Function URL reaching a 200 would be the worst bug this phase could ship, and this is the only automated check that can see it.
3. **`OPTIONS {chat_url}/books/{id}/chat`** with `Origin: https://example.com` and `Access-Control-Request-Method: POST`, `Access-Control-Request-Headers: authorization` → **2xx with `access-control-allow-origin` present, and specifically NOT 401.** Phase-6 §16's bug, measured rather than reasoned about (§4.4).
4. **`POST` with a token belonging to another user's book id → 404** (never 403).
5. **`POST` with a valid token → 200**, `Content-Type: text/event-stream`, then read the body **frame by frame** and assert:
   - the first frame is `event: meta`, with `enabled is False`, `model == "stub"`, and `anchorChunk` equal to the value sent;
   - at least **three** `delta` frames arrive (so the answer really is chunked, not one blob);
   - the last frame is `event: done` with `finishReason == "DISABLED"`;
   - the concatenated delta text **contains the first 40 characters of `chunks[anchor]["text"]`**, fetched from `GET /books/{id}/chunks`. **This is the load-bearing assertion of the whole phase**: it proves, end to end through the real deploy, that the anchor was clamped correctly, the window was sliced correctly, and the resolved context reached the model — in an environment with no API key.
6. **A second `POST`** with a different question → the earlier turn appears in `GET /books/{id}/chat`, ordered oldest-first, with `role` alternating `user`/`assistant` and `anchoredChunk` set.
7. **`DELETE {api_url}/books/{id}/chat` → 200** with `{"deleted": 4}`, then `GET` → `messages == []`.
8. **Question length**: `POST` with a 3,000-character question → **422**.

Deliberately not asserted: the quota 429 (fifty requests to prove one branch is a poor trade; unit-tested instead against a fake repository).

### 12.3 What no automated check can verify — say it out loud

The last three phases each shipped a bug only a real deploy could catch. These are this phase's candidates:

| uncovered | why | where the risk actually lands |
|---|---|---|
| **That `InvokeMode: RESPONSE_STREAM` + LWA genuinely streams rather than buffering** | Compose runs uvicorn directly; the adapter exists only in the Lambda image. A buffering regression is *functionally invisible* — the same bytes arrive, just all at once. | `deploy-pr` only. §12.2 step 5's "at least three delta frames" catches a total collapse; the Playwright spec's strictly-increasing-length assertion (§13.4) catches the subtler version. |
| **That the LWA container starts at all** | The `lambda-stream` stage is never built locally by `make up` (compose uses the `dev` target). | `deploy-pr`'s first run. Failure mode is loud (502 on every chat request), not silent. Step 0 of §15 builds the stage locally by hand before any code is written. |
| **That the Function URL's CORS config answers preflight without invoking the function** | Locally FastAPI's own `CORSMiddleware` answers it, so the asymmetry is invisible — the exact shape of phase-6's bug. | `deploy-pr`, via §12.2 step 3. |
| **That JWKS is reachable from a no-VPC Lambda, and that the pool id/audience are right** | No environment before `deploy-pr` verifies a real Cognito signature (cognito-local's signatures are fake, which is why `LocalAuthMiddleware` does not verify). | `deploy-pr`, via §12.2 steps 2 and 5. |
| **That a cross-origin browser `fetch` + `ReadableStream` sees incremental chunks** | jsdom's fetch is stubbed in vitest; compose is same-machine. | `deploy-pr`'s Playwright run. |
| **That the OpenAI request shape is accepted, and the response parses** | **Nothing, ever, in any automated environment** — constraint 1 forbids the call outside prod, and `deploy-prod.yml` runs no login-gated checks. | A human, in prod, after creating the secret. **This is phase-4 §0's accepted trade-off, unchanged**: real external services get proven by using the deployed app, not by an automated check against a paid third party. §15 step 15 makes it an explicit manual step with a checklist. |

---

## 13. Test plan

### 13.1 Frontend — vitest

**Pure modules.**

- **`chat-sse.test.ts`** (`lib/chat.ts`'s `createSseParser`)
  1. one complete frame in one chunk → one event.
  2. one frame split across **three** chunks, splitting mid-`data:` prefix and mid-JSON → one event, emitted only when complete.
  3. three complete frames in one chunk → three events, in order.
  4. `\r\n` line endings.
  5. `:` keepalive comment lines are skipped and do not emit.
  6. unknown `event:` type is ignored, and a following known frame still parses (forward compat).
  7. a `data:` line that is not valid JSON is skipped, and the parser keeps working.
  8. a `meta` frame with `enabled:false` round-trips `reason` and `windowChunks`.
- **`chat-client.test.ts`** (`streamChat`, `global.fetch` stubbed with a `ReadableStream`)
  1. a pre-stream 401 rejects with `UNAUTHENTICATED` and clears the session.
  2. a pre-stream 429 rejects with `QUOTA_EXCEEDED` and surfaces `retryAfterSeconds`.
  3. a pre-stream 404 rejects with `BOOK_NOT_FOUND`.
  4. happy path calls `onMeta` once, `onDelta` N times in order, `onDone` once.
  5. an `error` frame mid-stream calls `onError` and then `onDone` with `ERROR`, **without** discarding earlier deltas.
  6. **a stream that ends with no `done` frame resolves and calls `onDone` with `TRUNCATED`** — §4.5's forgotten branch.
  7. `AbortSignal` aborts the reader and does not call `onDone`.
  8. the request body carries `question`, `anchorChunk`, `positionMs`, and an `Authorization: Bearer` header.

**Hooks.**

- **`use-reading-anchor.test.tsx`** — playback `chunkIndex >= 0` wins; with `chunkIndex === -1` the topmost intersecting `data-chunk-index` wins (stubbed `IntersectionObserver`); with neither, 0; **scrolling does not re-render** (render-count spy — the ref rule).
- **`use-chat.test.tsx`** — optimistic user turn appears immediately; deltas accumulate into one assistant turn; **≤ 3 renders over 60 simulated deltas spanning 2 rAF frames** (the flush-on-rAF guarantee); composer disabled while streaming; unmount aborts; a second question appends rather than replacing.

**Components.**

- **`chat-sidebar.test.tsx`** — one test per row of §8.4's table, asserting the exact copy and the composer's disabled state; `NOT_CONFIGURED` and `NON_PROD` produce **different** copy and **different** composer states; the "about section 12" label renders and calls `seekToChunk`; completed `ChatTurn`s do **not** re-render while a new turn streams (render-count spy, phase-6's `ChunkParagraph` pattern).
- **`chat-composer.test.tsx`** — Enter sends, Shift+Enter newlines; a 2,001-character question is blocked client-side and never reaches the network; empty/whitespace does not send.
- **`books-api.test.ts`** Δ — `listChat` returns `{messages, chat}`; `clearChat` issues `DELETE`.

### 13.2 Backend — pytest (gate stays `--cov-fail-under=90`; **zero AWS credentials, no region**)

- **`test_chat_context.py`** ⊕ (pure `resolve_context`)
  1. mid-book anchor → 5 chunks, anchor flagged.
  2. anchor 0 → 3 chunks, **clipped not shifted** (asserts indexes `[0,1,2]`, not `[0..4]`).
  3. anchor at the last index → 3 chunks, clipped.
  4. a 3-chunk book at anchor 1 → all 3.
  5. `anchor_chunk=None` → 0; negative → 0; ≥ len → last (the clamp).
  6. char ceiling: five 2,600-char chunks → farthest neighbours dropped first, anchor always present, total ≤ 12,000.
  7. an anchor chunk that alone exceeds the ceiling → truncated, never dropped.
  8. history: 20 messages → last 6, ascending.
  9. a trailing unanswered `user` message is dropped.
  10. a leading `assistant` message is dropped.
  11. a 1,500-char history message is truncated to 1,000 with a marker.
  12. `window_indexes` matches what the `meta` frame will carry.
- **`test_openai_prompt.py`** ⊕ (pure `render_messages`)
  1. `messages[0].role == "system"`; instructions precede the `<book>` block (the prefix-cache ordering, §6.4).
  2. exactly one section carries `reading-here="true"`, and it is the anchor.
  3. `messages[-1]` is the user's question, verbatim.
  4. history messages alternate and appear between system and question.
  5. **no chunk text outside the window appears anywhere in the rendered messages** (fed a 50-chunk book with a sentinel string in chunk 40).
  6. `title` is escaped for the XML-ish attribute (a book titled `Ship "Pequod"` does not corrupt the block).
- **`test_stub_chat_model.py`** ⊕
  1. `NON_PROD` streams ≥ 3 deltas, and the concatenation contains the anchor chunk's opening text and the question.
  2. `NOT_CONFIGURED` streams the fixed prod sentence and **not** the context echo.
  3. deterministic: two runs on the same context produce identical output.
  4. **network guard** — `socket.socket` monkeypatched to raise; a full stream completes.
- **`test_chat_dependencies.py`** ⊕ — **the constraint-1 regression suite**
  1. parametrized over `local`, `dev`, `pr-1`, `pr-42`, `staging`: `get_chat_model()` is a `StubChatModel` **even when `openai_secret_name` is set to a non-empty value**, and `get_secret` is never called (asserted with a spy that raises).
  2. `prod` + `openai_secret_name == ""` → `StubChatModel(NOT_CONFIGURED)`, `get_secret` not called.
  3. `prod` + non-empty name → `OpenAiChatModel`, `get_secret` called exactly once with that name.
  4. `prod` + name pointing at an absent secret (moto, no such secret) → the `ClientError` propagates and the controller maps it to **503 `LLM_UNAVAILABLE`**, before any stream begins.
- **`test_openai_chat_model.py`** ⊕ (injected fake HTTP callable — zero network)
  1. request shape: URL, `model`, `stream: true`, `stream_options.include_usage`, `max_completion_tokens`, `temperature`, and the `Authorization: Bearer` header.
  2. delta mapping: `choices[0].delta.content` → `ChatDelta`; frames with no `content` are skipped.
  3. `finish_reason` mapping: `stop`/`length`/`content_filter`/unknown.
  4. `usage` from the final frame reaches the result, including `prompt_tokens_details.cached_tokens`.
  5. `data: [DONE]` terminates cleanly.
  6. a malformed JSON frame mid-stream is skipped with a WARNING and the stream continues.
  7. a 429 **before** the first token → exactly one retry, then success.
  8. a 429 → 429 → `ChatUnavailable`, and the public message is the fixed constant (not the upstream body).
  9. a socket error **after** the first token → `ChatStreamError`, **no retry** (a replay would duplicate on-screen text).
  10. **`repr(model)` and `str(model)` do not contain the key.**
  11. **log scrub**: a full stream at DEBUG with `caplog` — no record contains the key sentinel, the request body, or the response body.
- **`test_jwt_verifier.py`** ⊕ (locally generated RSA keypair; no network)
  1. a valid id token verifies and returns claims with `sub`.
  2. expired → `InvalidToken`.
  3. wrong `aud` → `InvalidToken`.
  4. wrong `iss` → `InvalidToken`.
  5. `token_use: "access"` → `InvalidToken`.
  6. a token signed by a different key → `InvalidToken`.
  7. `alg: "none"` → `InvalidToken` (the classic).
  8. an unknown `kid` triggers **exactly one** JWKS refetch; a second unknown `kid` within 300 s triggers **zero** (the cooldown, §4.3).
  9. `InvalidToken`'s `str()` contains neither the token nor any claim value.
- **`test_function_url_middleware.py`** ⊕ — missing header → 401 JSON, route never invoked; invalid token → 401; valid token → claims land in `scope["aws.event"]` in API Gateway's shape and `get_current_user` returns the right `sub`; `/health` is exempt.
- **`test_chat_repository.py`** ⊕ (moto) — `save_turn` writes two items; SK ordering is chronological across a 20-message fixture; `list_recent(limit=6)` returns the newest 6 **ascending**; `delete_for_book` removes all and returns the count; a zero-microsecond timestamp still sorts correctly (§7.1's caveat).
- **`test_chat_quota_repository.py`** ⊕ (moto) — first call creates `count=1`; the 50th succeeds; the 51st raises `QuotaExceeded`; a different date is a fresh counter; a different user is a fresh counter; concurrent increments (sequential calls against the conditional) never exceed the limit.
- **`test_chat_use_case.py`** ⊕
  1. `NotFoundError` for a **foreign** book whose chat items exist — the §7.3 authorization test.
  2. `ConflictError` when `chunks_total == 0`.
  3. `QuotaExceeded` → the model's `stream` is **never called** (spy).
  4. ordering with a call-recording spy: **auth → quota → context → model** (phase-4 §8.2's rule).
  5. a completed stream persists exactly two items with the right roles, `anchoredChunk`, `model`, `finishReason`, token counts.
  6. an exception mid-stream persists the partial with `finishReason: "ERROR"`.
  7. **zero deltas persists nothing.**
  8. `GeneratorExit` (client disconnect) persists the partial.
  9. the clamped anchor, not the client's, is what is stored.
- **`test_chat_controllers.py`** ⊕ — 401 anon; 404 foreign; 409 `chunksTotal == 0`; 422 over-long question; 429 with `Retry-After`; 200 with `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`; frame order `meta` → `delta`+ → `done`; a mid-stream error emits `error` then `done`.
- **`test_controllers.py`** Δ — `GET /books/{id}/chat` 200 shape (including the `chat` envelope with `enabled`/`reason`/`dailyLimit`/`usedToday`) / 404 foreign / 401 anon; `DELETE /books/{id}/chat` 200 count / 404 foreign / 401 anon.
- **`test_chat_app.py`** ⊕ — the route set is **exactly** `{"/health", "/books/{book_id}/chat"}`; `FunctionUrlAuthMiddleware` is mounted for `pr-1`/`prod` and `LocalAuthMiddleware` for `local`; `CORSMiddleware` is present for `local` and **absent** otherwise (§4.2's deliberate asymmetry).
- **`test_config.py`** Δ — `openai_secret_name` defaults to `""`; `openai_model` defaults to `"gpt-4.1-mini"`; `chat_daily_limit` defaults to 50; **there is no `openai_api_key` field on `Settings`** (§5.5 rule 1, asserted).
- **`test_keys.py`** Δ — `sk_chat` monotonicity, fixed width, zero-microsecond case, and `sk_chat_quota` format.

### 13.3 Infra — `infra/tests/test_synth.py` (Δ)

- `test_api_stack_lambda_count` — `AWS::Lambda::Function` count in `ApiStack` is **exactly 2** (Api + Chat). A count assertion, not a presence one, because Q4 is a concurrency-budget promise.
- `test_chat_function_shape` — `ReservedConcurrentExecutions: Match.absent()`, memory 512, timeout 120.
- `test_chat_function_url` — `AWS::Lambda::Url` with `AuthType: "NONE"`, `InvokeMode: "RESPONSE_STREAM"`, and a `Cors` block whose `AllowHeaders` contains `authorization` and `AllowMethods` contains `POST`.
- `test_chat_function_env` — carries `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_REGION`, `OPENAI_SECRET_NAME`, `OPENAI_MODEL`, `CHAT_DAILY_LIMIT`.
- **`test_openai_secret_name_defaults_empty_in_every_environment`** — parametrized over `dev`, `pr-1`, **`prod`** — `OPENAI_SECRET_NAME` is `""` and **zero** `secretsmanager:GetSecretValue` statements exist. The constraint-2 regression test, and the direct analogue of the phase-4 OQ-A correction.
- `test_openai_secret_grant_appears_with_context_flag` — synth with `-c openai_secret_name=bookloud/openai-api-key`: the env var is set **and** exactly one `GetSecretValue` grant appears, **on `ChatFunction` only**.
- **`test_api_function_cannot_read_the_secret`** — even with the context flag, `ApiFunction`'s role has **no** `GetSecretValue` statement.
- `test_chat_function_has_no_s3_or_sqs_grants` — the function with a public URL touches the table and nothing else.
- **`test_no_stack_template_contains_a_key_shaped_string`** — every synthesized template is searched for `sk-` prefixed values; §5.5 rule 7.
- `test_http_api_route_shape_unchanged` — still 9 routes; still no authorized route matching `OPTIONS` (phase-6 §16's guard, extended not replaced).
- `test_frontend_stack_has_chat_base_url` — `NEXT_PUBLIC_CHAT_BASE_URL` on the SSR function.

### 13.4 Playwright

One new spec, and — unlike audio — **it runs everywhere**, because the stub streams in every environment.

| spec | `e2e-local` | `deploy-pr` (`chromium`) | asserts |
|---|---|---|---|
| `e2e/chat.spec.ts` ⊕ | ✅ | ✅ | the whole feature |

Steps, in order:

1. Log in, upload the fixture, poll to terminal (reusing `e2e/support/session.ts` unchanged).
2. Open the chat sidebar via the player-bar toggle. Assert the **"Chat is turned off in this environment"** notice is present and the composer is **enabled** (§8.4 row 2 — this is the row that makes the rest of the spec possible).
3. Scroll the reading pane so a **known** chunk is at the top, then assert `meta`'s `anchorChunk` in the rendered "about section N" label matches — proving §6.2's IntersectionObserver path, which is the only anchor path that exists in a PR environment.
4. Ask "What is this section about?".
5. **Incremental delivery**: sample the answer bubble's `textContent.length` twice, ~300 ms apart, while `data-streaming="true"`; assert it strictly increases. **This is the assertion that catches a buffering regression** — the one failure mode §12.3 says no local test can see.
6. **References book content**: assert the final answer contains a distinctive marker string from the fixture PDF's anchor chunk. The stub echoes it verbatim, so this is a real assertion about context resolution, through a real deploy, with no API key.
7. Ask a second question; assert **both** turns are in the transcript, in order, and the composer re-enabled between them.
8. Reload the page, reopen the sidebar, assert the transcript persisted (proving `GET /books/{id}/chat` and the SK ordering through the real stack).
9. Click the "about section N" label; assert the reading pane scrolls to that chunk.

**Fixture.** `e2e/fixtures/reader-pdf.ts` already produces ≥3 chunks with distinctive text; the marker string for step 6 is a sentence that appears in exactly one chunk. If the current fixture has no such sentence, one is added — a fixture change, not a new fixture.

**CI wiring.** None. `ci.yml`'s `e2e-local` job and `deploy-pr.yml`'s `chromium` step both already run the whole `frontend/e2e/` directory; the new spec is picked up for free. `deploy-pr.yml` changes only to export `ChatUrl` and pass it as `NEXT_PUBLIC_CHAT_BASE_URL` (build) and `--chat-url` (smoke). Same for `deploy-prod.yml`'s build step.

---

## 14. Concrete file list

**`backend/src/`**
- new: `chat_app.py`, `auth/jwt_verifier.py`, `auth/function_url.py`
- new: `contexts/library/domain/chat.py`
- new: `contexts/library/application/chat.py`
- new: `contexts/library/infrastructure/openai_chat_model.py`, `openai_prompt.py`, `stub_chat_model.py`, `dynamodb_chat_repository.py`, `chat_mapper.py`, `dynamodb_chat_quota_repository.py`
- new: `contexts/library/interface/chat_controllers.py`
- changed: `config.py`, `main.py` (docstring only — the `chat/` package it anticipated is not created, §3), `contexts/library/domain/repository.py`, `contexts/library/infrastructure/keys.py`, `contexts/library/interface/controllers.py`, `dependencies.py`, `schemas.py`

**`backend/tests/`**: `test_chat_context.py`, `test_openai_prompt.py`, `test_stub_chat_model.py`, `test_chat_dependencies.py`, `test_openai_chat_model.py`, `test_chat_use_case.py`, `test_chat_controllers.py`, `test_chat_repository.py`, `test_chat_quota_repository.py` (new, under `tests/contexts/library/`); `tests/auth/test_jwt_verifier.py`, `tests/auth/test_function_url_middleware.py`, `tests/test_chat_app.py` (new); `tests/contexts/library/fakes.py` (⊕ `FakeChatModel`, `FakeChatRepository`, `FakeChatQuotaRepository`), `test_controllers.py`, `test_keys.py`, `test_config.py`, `test_dependencies.py` (changed)

**`backend/`**: `Dockerfile` (⊕ `lambda-stream` stage), `pyproject.toml` + `uv.lock` (⊕ `pyjwt[crypto]`)

**`frontend/`**
- new: `lib/chat.ts`, `hooks/useChat.ts`, `hooks/useReadingAnchor.ts`, `components/ChatSidebar.tsx`, `ChatTurn.tsx`, `ChatComposer.tsx`
- changed: `lib/api.ts` (⊕ `chatUrl`), `lib/books.ts` (⊕ `listChat`, `clearChat`), `components/ReaderView.tsx`, `PlayerBar.tsx`, `ReadingPane.tsx`, `ChunkParagraph.tsx` (`data-chunk-index`)
- new tests: `__tests__/chat-sse.test.ts`, `chat-client.test.ts`, `use-reading-anchor.test.tsx`, `use-chat.test.tsx`, `chat-sidebar.test.tsx`, `chat-composer.test.tsx`
- new e2e: `e2e/chat.spec.ts`; changed: `e2e/fixtures/reader-pdf.ts` (marker sentence)
- **no new runtime or dev dependency**

**`infra/`**: `stacks/api_stack.py`, `stacks/config.py`, `stacks/frontend_stack.py`, `app.py`, `tests/test_synth.py` (changed). **`stacks/storage_stack.py`, `stacks/pipeline_stack.py`, `stacks/auth_stack.py` untouched** — stated explicitly.

**`local/`**: `smoke_test.py` (changed). `setup.sh` untouched.

**Root**: `docker-compose.yml` (⊕ `chat` service, frontend build arg), `Makefile`, `.github/workflows/deploy-pr.yml`, `deploy-prod.yml`, `README.md`, `IMPLEMENTATION_PLAN.md`.

---

## 15. Implementation sequence

0. **Pre-flight, before any application code.** Build the new image stage by hand — `docker build --target lambda-stream backend/` — and run it locally with `docker run -p 9000:8080` against the Lambda RIE to confirm the container starts, uvicorn binds, and `/health` answers. **If the LWA entrypoint does not come up, fix the Dockerfile first**; every downstream step assumes it does, and §12.3 says nothing else in the plan can catch it.
1. **Backend, pure first:** `domain/chat.py`'s value objects + `resolve_context` + `chat_availability`, and `test_chat_context.py`. **Gate:** the clip-don't-shift and ceiling-eviction cases are green before anything renders a prompt.
2. `infrastructure/openai_prompt.py` + `test_openai_prompt.py`, including the "no text outside the window" test.
3. `infrastructure/stub_chat_model.py` + `test_stub_chat_model.py` (with the socket guard). **Checkpoint:** there is now a complete, offline, deterministic model implementation before a single line of OpenAI code exists.
4. `auth/jwt_verifier.py` + `auth/function_url.py` + their tests (local RSA keypair). **Gate:** the `alg: none` and `token_use: access` cases are green.
5. `keys.py` additions, `chat_mapper.py`, `dynamodb_chat_repository.py`, `dynamodb_chat_quota_repository.py` + moto tests. **Gate:** SK monotonicity including the zero-microsecond case.
6. `application/chat.py` + `test_chat_use_case.py` — the ordering spy and the foreign-book test first.
7. `interface/chat_controllers.py`, `schemas.py`'s `sse_frame`, `src/chat_app.py` + `test_chat_controllers.py`, `test_chat_app.py`. **Checkpoint:** `uvicorn src.chat_app:app` locally + `curl -N` streams a stub answer with real book text in it.
8. `interface/controllers.py`'s `GET`/`DELETE /books/{id}/chat` + `test_controllers.py`. Full backend suite ≥ 90%.
9. `infrastructure/openai_chat_model.py` + `test_openai_chat_model.py` (injected fake transport), including the repr and log-scrub tests. **This is the last backend step, deliberately** — everything above it is provably correct with no key and no network.
10. `interface/dependencies.py`'s `get_chat_model` + `test_chat_dependencies.py`. **Gate:** the parametrized "non-prod always stubs even with a secret name set" case is green. This is constraint 1's regression test and nothing ships without it.
11. `backend/Dockerfile`, `infra/stacks/config.py`, `api_stack.py`, `frontend_stack.py`, `app.py`, `infra/tests/test_synth.py`. `make synth`.
12. `docker-compose.yml`'s `chat` service, `Makefile`. `local/smoke_test.py`'s `check_chat`. **`make smoke` is the gate** — steps 3-8 of §12.2 run here for the first time, against uvicorn rather than LWA.
13. **Frontend pure first:** `lib/chat.ts` + `chat-sse.test.ts` + `chat-client.test.ts`. **Gate:** the split-frame and no-`done`-frame cases are green before anything renders.
14. `hooks/useReadingAnchor.ts`, `hooks/useChat.ts` + tests. **Gate:** the ≤3-renders-over-60-deltas test.
15. `ChatSidebar`/`ChatTurn`/`ChatComposer` + tests; `ReaderView`/`PlayerBar`/`ReadingPane` wiring. **Checkpoint:** every row of §8.4's table renders correctly, including the two disabled rows.
16. `e2e/chat.spec.ts` + the fixture marker. `make e2e-local`.
17. `deploy-pr.yml` / `deploy-prod.yml`: export `ChatUrl`, thread it into the frontend build and the smoke test.
18. `README.md` (the two hosts, the auth model for the streaming endpoint, the context/token budget, the degradation table, and the **two-step key setup**) and `IMPLEMENTATION_PLAN.md`'s checklist.
19. Push. `deploy-pr` runs the whole thing against real AWS with a real browser — which is the first and only place §12.3's five uncovered items get tested.
20. **Manual, prod, after merge — the one thing no pipeline does.** Create the secret out of band, deploy with the context flag, ask three questions in the deployed app, and check: an answer arrives incrementally; it is grounded in the right section; `usage` in the CloudWatch log shows a non-zero `cachedInputTokens` on the second question at the same anchor; and no log line anywhere contains the key. Then leave `openai_secret_name` set. **Until this step is done, prod runs §5.3's degraded path, and that is a supported state, not a broken one.**

---

## 16. Open Questions

Each states a recommendation and the trade-off, so one word approves it.

**OQ-1 — Ship streaming, or ship a buffered single response and defer the Function URL entirely?**
Streaming is what `IMPLEMENTATION_PLAN.md` specifies, and it is the reason this phase adds a Lambda, a Dockerfile stage, a Function URL, an in-Lambda JWT verifier, a second ASGI app and a compose service. A buffered `POST /books/{id}/chat` on the **existing** API Lambda would need none of those — it is one route, one use case, and the Cognito authorizer already in front of it. The cost is a 5-15 second spinner per question instead of words appearing.
**Trade-off:** roughly two-thirds of this plan's infrastructure and auth surface exists to make tokens arrive early, on a personal app where the same user asks a handful of questions a day; against that, a wall of silence followed by a paragraph is a materially worse reading experience, and retrofitting streaming later means doing all of §4 anyway with a migration on top.
**Recommendation: ship streaming.** It is the phase's stated goal, the incremental work is front-loaded rather than ongoing, and §5's stub design means the whole transport is exercised in every environment rather than only in prod.

**OQ-2 — `gpt-4.1-mini` (recommended), `gpt-4o-mini` (cheaper), or `gpt-4.1` (better)?**
§6.5's arithmetic at 20 questions/day: ~$0.47/mo, ~$1.26/mo, ~$6.24/mo respectively; at the 50/day cap, ~$1.17 / ~$3.15 / ~$15.60.
**Trade-off:** the task is grounded extraction over ~2,400 supplied tokens, which is the mini tier's strong suit, so `gpt-4.1` mostly buys prose quality on a two-sentence answer. `gpt-4o-mini` saves another $0.79/month but is noticeably weaker at honouring "say so when the excerpt doesn't cover it" — which is the one behaviour that makes the feature trustworthy rather than confidently wrong.
**Recommendation: `gpt-4.1-mini`.** `OPENAI_MODEL` is an env var, so changing it is one line and a deploy — decide again after a month of real bills rather than now. **Verify current list prices at implementation time**; the figures above are estimates.

**OQ-3 — Direct Function URL (cross-origin), or route `/chat/*` through the existing CloudFront distribution (same-origin)?**
This plan calls the Function URL directly from the browser, cross-origin, with Function-URL-native CORS. The alternative adds an additional behaviour on the FrontendStack distribution pointing at the chat origin, making chat same-origin with the app and removing CORS from the picture entirely.
**Trade-off:** CloudFront routing eliminates the preflight and hides the Function URL behind the app's own domain, but adds a `Frontend → chat URL` wiring edge, a behaviour with `CACHING_DISABLED` + no compression, and — the reason it loses — **one more service in the streaming path whose buffering behaviour we cannot verify locally**, on top of LWA's, which §12.3 already lists as unverifiable. With `AuthType: NONE` it also does not actually hide anything: the origin URL stays publicly reachable either way.
**Recommendation: direct Function URL.** Fewer services between the token and the browser, and Lambda answering preflight without invoking the function is a stronger guarantee than a CloudFront behaviour that has to be configured correctly.

**OQ-4 — Window of ±2 chunks (5 sections, ~2,370 tok), or ±1, or ±4?**
±1 is ~1,420 tokens (~$0.0014/question), ±4 is ~4,270 (~$0.0028).
**Trade-off:** at 1,800 chars a chunk is roughly two minutes of narration, so ±1 covers ~6 minutes and will sometimes cut off the paragraph the question is actually about; ±4 covers ~18 minutes and nearly doubles input cost for context most questions never reach. The difference in monthly spend between all three is under $1.
**Recommendation: ±2.** It maps to "the scene you're in", the cost delta is noise at this volume, and `CONTEXT_NEIGHBOURS` is one constant if it proves wrong.

**OQ-5 — A daily cap of 50 questions per user, or no cap?**
The cap is one conditional `UpdateItem` before context resolution, and it bounds a runaway client (a retry loop, a stuck component) at ~$3/month instead of unbounded.
**Trade-off:** 50 is arbitrary and a genuinely engaged reading session could conceivably hit it, at which point the sidebar goes read-only until midnight UTC with no override. Against that: this account cannot use reserved concurrency (constraint 3), so the counter is the *only* mechanism standing between a bug and a bill.
**Recommendation: keep the cap at 50.** `CHAT_DAILY_LIMIT` is an env var, so raising it is a config change, not a deploy of code — and the failure mode of having no cap is discovered on a statement.

**OQ-6 — `CHAT#<createdAt>#<msgId>`, or the literal `CHAT#<msgId>` from `IMPLEMENTATION_PLAN.md`'s data model?**
The spec's row says `CHAT#<msgId>`. A uuid4 sorts randomly, so "the last six messages" becomes read-the-partition-and-sort-client-side on every single question, forever, growing with the transcript.
**Trade-off:** deviating from the recorded data model is a real cost — that table is the one place someone looks to understand the schema — and the timestamp prefix makes the SK slightly less pleasant to read in the console. The alternative is either a full-partition read per question or a GSI, and a GSI for a ≤50-item partition is not defensible.
**Recommendation: take the deviation**, and update `IMPLEMENTATION_PLAN.md`'s data-model row in the same PR so the spec and the table never disagree.

**OQ-7 — Should the non-prod stub stream a canned answer (this plan), or raise like `StubSynthesizer` does?**
Phase-4 §0's stub raises, and consistency argues for the same here.
**Trade-off:** a raising stub is simpler and unambiguously "off". But it would mean **no environment in existence** ever streams a byte through the Function URL — not compose, not `deploy-pr`, not `deploy-prod` — so `RESPONSE_STREAM`, LWA, the SSE re-framing, the browser reader and the incremental render would all reach prod unexecuted, on a project where the last three phases each shipped a bug only a real deploy caught. The canned answer also makes "the response references book content" a genuine assertion about context resolution in a keyless environment (§12.2 step 5, §13.4 step 6).
**Recommendation: canned stream.** It is still fully offline, so phase-4 §0's actual rule — no external service outside prod — is untouched; what changes is only what the offline stand-in does with its turn.

**OQ-8 — Persist chat turns at all, or keep the conversation in browser memory only?**
Persistence costs two DynamoDB writes per question, a repository, a mapper, two endpoints, and §7.3's authorization argument.
**Trade-off:** in-memory would be materially less code and would sidestep the `BOOK#`-keyed ownership question entirely. But history is what makes follow-up questions ("what did she mean by that?") work at all — it is an input to §6.1's context, not just a UI nicety — and a transcript that vanishes on reload for a book you read across several sittings is a feature that feels broken.
**Recommendation: persist.** The data model already reserved the row; the follow-up-question case is the main reason to want chat over search.

**OQ-9 — Window-only context, or add whole-book retrieval?**
As designed, a question about chapter 1 asked during chapter 20 gets "that isn't in the section you're on."
**Trade-off:** whole-book retrieval (keyword or embeddings) would answer it, but means an index built at extraction time, stored somewhere, kept in sync with `/resynthesize`, and a relevance model — a phase of work, not a section. And the *stated* product is a chat "scoped to the section currently being read" (`IMPLEMENTATION_PLAN.md`'s project summary), where a bounded window is the feature rather than a limitation.
**Recommendation: window-only in phase 7**, with the boundary stated in the system prompt so the model says so honestly rather than confabulating. Revisit only if "it can't answer about earlier chapters" turns out to be a real, repeated complaint from actual use.

**OQ-10 — Sidebar opens on playback (the plan's literal wording), or a persisted toggle that auto-opens once on first play?**
`IMPLEMENTATION_PLAN.md` says "opens during playback". Taken literally, chat is unreachable in every environment CI can run, because no book there has audio — including the environment where the Playwright spec runs.
**Trade-off:** a toggle is one more control in the player bar and one more `localStorage` key; tying it to playback is simpler but couples a text feature to an audio feature that is absent in most of this project's environments and for every `PARTIAL` book in prod.
**Recommendation: persisted toggle, auto-opening once on first play.** Same shape as phase-6 OQ-6's playback-rate preference, and it keeps the sidebar reachable for a book that will never have audio.

---

### Critical Files for Implementation
- backend/src/chat_app.py *(new — the second ASGI app)*
- backend/src/auth/dependencies.py *(unchanged, and that is the point of §4.3)*
- backend/src/auth/local_dev.py *(the template for FunctionUrlAuthMiddleware)*
- backend/src/contexts/library/domain/chunking.py *(DEFAULT_TARGET_CHARS = 1800 — every number in §6 derives from it)*
- backend/src/contexts/library/domain/repository.py *(the "this port performs no access control" contract §7.3 copies)*
- backend/src/contexts/library/infrastructure/google_tts_synthesizer.py *(the urllib + injected-callable shape §5.4 reuses)*
- backend/src/contexts/library/infrastructure/secrets.py
- backend/src/contexts/library/interface/dependencies.py *(get_speech_synthesizer is the gate §5.2 copies)*
- backend/Dockerfile
- infra/stacks/api_stack.py
- infra/app.py *(the google_tts_secret_name block §5.2 mirrors)*
- frontend/lib/api.ts
- frontend/hooks/usePlayback.ts *(state.chunkIndex — anchor source 1)*
- local/smoke_test.py
