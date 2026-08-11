# Phase 6 — Reader UI

Goal: the first phase where a human uses this app. A left sidebar lists books and polls their status; the centre pane renders the extracted text of the open book and lights the word currently being spoken; a player bar plays, pauses and seeks the stitched `book.mp3`. Three things phases 3-5 deliberately deferred land here because this is the phase that can finally validate them: **an audio-delivery endpoint** (phase-5 OQ-3), **`POST /books/{id}/resynthesize`** (phase-5 OQ-6), and **a frontend upload flow** (the presigned POST has existed since phase 3 with no UI to call it).

**Verified against the current repo (post `528d749`, phase 5 merged):**

- `interface/controllers.py` already exposes `POST /books`, `POST /books/{id}/upload-url`, `GET /books`, `GET /books/{id}`, `GET /books/{id}/status`, `GET /books/{id}/chunks`. All six sit under `ApiStack`'s `/{proxy+}` route with `HttpUserPoolAuthorizer` — **every route added in this phase inherits that authorizer with no `api_stack.py` route change**.
- `interface/schemas.py`'s `book_status_to_dict` already computes `terminal` over `TERMINAL_BOOK_STATUSES` and `progress.percent` with the `chunksTotal == 0` branch handled. The phase-6 poll loop must use `terminal`, never a client-side status list — that is the entire justification phase-5 §8.1 gave for the endpoint's existence.
- `chunk_to_dict` already returns `text`, `charStart`, `charEnd`, `status`, `failureReason`, `durationMs`, `synthesisSource` for every chunk. **The reading pane needs no new endpoint to get its text.**
- `api_stack.py` already does `audio_bucket.grant_read_write(fn)` and `marks_bucket.grant_read_write(fn)` on the API Lambda. So presigning a GET on `audio/<u>/<b>/book.mp3` and reading `marks/<u>/<b>/book.json` need **zero new IAM**. The only new grants this phase adds are the two `sqs:SendMessage` grants `/resynthesize` needs.
- `infrastructure/s3_pdf_storage.py`'s `_build_client()` exists precisely because `generate_presigned_post` signs against the *client's* endpoint, which inside compose is `http://localstack:4566` — unreachable from a browser. `settings.s3_public_endpoint_url` is the host-reachable override. **The audio presigner has exactly the same problem and must use exactly the same client** (§4.2). `src/infrastructure/aws.py`'s `client("s3")` would silently mint a URL no browser can fetch.
- `storage_stack.py` gives `pdf_bucket` a CORS rule and gives `audio_bucket`/`marks_bucket` **none**. §3.4 shows why that stays true.
- `infra/app.py` already wires `Storage, Auth, Pipeline -> Api -> Frontend`, and already passes `extract_queue=pipeline.extract_queue` into `ApiStack`. Adding `synthesize_queue`/`stitch_queue` is the same edge, no new dependency direction, no cycle.
- `application/stitching.py`'s `StitchBook.execute` short-circuits with `DEFERRED("NOT_COMPLETE")` when `book.chunks_total == 0 or book.chunks_done < book.chunks_total`. **This is load-bearing for `/resynthesize`** (§5.4): it is what makes a stale in-flight stitch message harmless after the counters are rewound.
- `_REISSUABLE_STATUSES` (`application/use_cases.py`) and `_CLAIMABLE_STATUSES` (`application/extraction.py`) are both `(BookStatus.UPLOADED, BookStatus.FAILED)`. Phase-5 OQ-6 is explicit that adding `PARTIAL` to either reintroduces the hazard `PARTIAL` was invented to prevent. **This plan does not touch either tuple, and §13.2 adds a test that asserts their exact contents** so a future "fix" for the dead end has to fail a unit test before it can fail a book.
- `infrastructure/silent_synthesizer.py` (phase-5 OQ-1) emits real MPEG-2 Layer III frames at 24 kHz/48 kbps CBR *and* real word marks via `estimate_word_marks`, opt-in via `SYNTHESIS_STUB_MODE=silent`, which `docker-compose.yml` sets **only** on the `synthesize-worker`. `make smoke` already passes `--expect-synthesis silent`. This is the single environment in which the highlight can be exercised end to end (§13.4).
- The frontend has **no state library, no data-fetching library, no component library** — `package.json`'s runtime dependencies are exactly `next`, `react`, `react-dom`. Every existing page is `"use client"`. `lib/auth.ts` already holds cross-component state (the id token) in a plain module variable with a single-flight refresh promise. §7.3 justifies keeping it that way rather than importing a store by reflex.
- `frontend/playwright.config.ts` exists with `testDir: "./e2e"` and a comment saying *"the first real spec lands in phase 6"*. `frontend/e2e/` contains only `.gitkeep`. `ci.yml`'s `local-smoke` job already boots the full compose stack on `ubuntu-latest`; `make ui` starts the frontend at `http://localhost:3000`.

---

## 1. End-to-end flow

```
browser (client components)                    API Lambda                       S3 / DynamoDB
  |
  | POST /books {title}  --------------------->| RequestBookUpload ------------>| BOOK# row (UPLOADED)
  |<-- {book, upload:{url, fields, key, ...}}  |  presigned POST
  |
  | POST <upload.url>  (multipart, NO auth header, file LAST) ------------------>| pdf_bucket
  |                                                    (S3 -> SQS -> extract -> synthesize -> stitch)
  |
  | GET /books/{id}/status   every 2s while !terminal -->| GetBook ------------->| GetItem
  |<-- {status, terminal, progress:{percent}, audio:{audioKey, manifestKey, durationMs}}
  |
  |  ... terminal == true ...
  |
  | GET /books/{id}/chunks   (once) ----------->| ListBookChunks -------------->| Query CHUNK#*
  | GET /books/{id}/manifest (once) ----------->| GetBookManifest -------------->| marks_bucket book.json
  | GET /books/{id}/audio    (once) ----------->| GetBookAudioUrl (presign) ---->| (no call -- local signing)
  |<-- {"url": "https://...X-Amz-Signature=...", "expiresIn": 3600, "durationMs": ...}
  |
  | <audio src={url}>  ---- GET + Range: bytes=N- (no Authorization header) ---->| audio_bucket
  |
  | GET /books/{id}/chunks/{n}/marks   lazily, current segment + 1 prefetch ---->| marks_bucket 00000N.json
  |
  | requestAnimationFrame loop: audio.currentTime -> locateSegment -> locateWord -> <mark>
  |
  | POST /books/{id}/resynthesize  (PARTIAL books only) -->| ResynthesizeBook -->| chunks FAILED->PENDING
  |<-- 202 {"retriedChunks": n, "book": {...status...}}    |  + book -> EXTRACTED
  |                                                        |  + enqueue_chunks / enqueue_book
```

Four invariants the phase rests on:

1. **The `<audio>` element is the only thing in the app that is not authenticated by a `Bearer` header, and it is authenticated by a signature in its URL instead.** Everything else goes through `authFetch`. §3 works out why that asymmetry is forced rather than chosen.
2. **The manifest is the timeline; the audio file is an optimization.** Phase-5 §1 invariant 3. The reader derives position, segment and word from `manifest` + per-chunk marks — never from `audio.duration`, which for a mixed-engine book is a browser *estimate* (phase-5 §7.1's no-Xing-header caveat).
3. **Highlighting is driven by `requestAnimationFrame`, not `timeupdate`.** §6.3 refines `IMPLEMENTATION_PLAN.md`'s phase-6 bullet, which says *"syncs highlight to `<audio>` `timeupdate`"*: `timeupdate` fires at ~4 Hz, which is coarser than the ~2.5 Hz word rate by only a factor of 1.6 and would drop short words entirely. `timeupdate` keeps a real but narrower job — driving the scrubber.
4. **A book with no audio is a first-class, fully usable state, not an error screen.** Constraint 2, recorded in phase-4 §0. In every environment CI can reach except local compose, `PARTIAL`/`NO_AUDIO` is the *only* state a finished book ever has, so the degraded path is the one the PR pipeline actually exercises (§9, §13.4).

---

## 2. Decisions up front

| # | Question | Decision | § |
|---|---|---|---|
| Q1 | **How does the browser fetch `book.mp3`?** | **`GET /books/{id}/audio` returns JSON containing an S3 presigned GET URL**, fetched with the normal `authFetch`; the client puts that URL in `<audio src>`. Not a 302 from the API (an `<audio>` request carries no `Authorization`, so API Gateway's authorizer 401s it before the redirect exists). Not CloudFront (needs signed URLs → a key group, a public key, a private key in Secrets Manager, and a new `Frontend → Storage` stack edge — for a single-user app). Range works: SigV4 query-string presigning does not sign `Range`, so the browser's own byte-range seeks are accepted verbatim. | §3 |
| Q2 | **Why not proxy the audio bytes through the API like the JSON?** | **Structurally impossible.** Lambda's response payload cap is 6 MB; a stitched 300-page book is ~240 MB. Not a preference — an arithmetic fact. | §3.2 |
| Q3 | **How does the browser fetch `book.json` and the per-chunk marks?** | **Proxied through the API** (`GET /books/{id}/manifest`, `GET /books/{id}/chunks/{n}/marks`), returning the S3 objects verbatim. ~60 KB and ~25 KB — nowhere near the 6 MB cap. Buys: no CORS rule on `marks_bucket` (it has none today and gains none), ownership enforced by `_load_owned_book` rather than by key opacity, and exactly **one** exception to "everything uses `authFetch`" instead of three. | §4.3 |
| Q4 | Presigned URL expiry | **1 hour** (`AUDIO_URL_EXPIRES_IN = 3600`), plus a **reactive refresh**: on the `<audio>` `error` event the client re-requests the URL, restores `currentTime`, and resumes — capped at 3 attempts so a genuinely deleted object doesn't loop. A longer expiry is not a substitute: a URL presigned with the Lambda role's *temporary* credentials dies when those credentials do, whatever `ExpiresIn` says. | §3.3, OQ-3 |
| Q5 | **How does the client get word timings?** | **Lazily, one chunk at a time, with a one-segment prefetch**, into an `Map<index, WordMark[] \| null>` capped at 8 entries. Never all up front: 330 chunks × ~300 words is ~6.6 MB across 330 requests before the first note plays — the same arithmetic that made phase-5 §7.2 reject a merged word array. | §6.2 |
| Q6 | Rebasing | **`t_global = segment.t + word.t`, applied once at cache-insert**, so the per-frame path has zero arithmetic. `s`/`e` are left **chunk-relative** — that is phase-4 §7.5's deliberate asymmetry (per-chunk marks index into one chunk's text; the manifest indexes into the book) and it is exactly what lets the reading pane slice the active paragraph without any global-offset bookkeeping. | §6.2 |
| Q7 | The search | **Two levels, both `bisectRight(...) - 1` over sorted arrays**: `segments[].t` (≤330 entries → ≤9 comparisons), then the rebased `words[].t` (~300 entries → ≤9). Steady state is **not** a search at all: the loop keeps the current index and does one comparison (`stillInside`), falling back to the binary search only on a discontinuity (a seek, or a tab regaining focus). | §6.3 |
| Q8 | Highlight boundary rule | **A word is lit from `words[i].t` until `words[i+1].t`** — by *start* boundaries, not by `[t, t+d)` spans. Makes the partition total (no dark gaps between words), makes `stillInside` exactly consistent with `locateWord`, and moots phase-5 §7.2's "must clamp, engines report word ends optimistically" warning. `d` is retained in the type but unused for highlighting. | §6.4 |
| Q9 | `timeupdate` vs `requestAnimationFrame` | **rAF for the highlight (~60 Hz), `timeupdate` for the scrubber (~4 Hz).** Chrome fires `timeupdate` about every 250 ms; at 150 wpm a word is ~400 ms and function words are 120-200 ms, so a 250 ms sampler visibly stutters and skips short words outright. rAF is throttled to zero in a background tab, which is correct behaviour (nothing to see) and is resynced on `visibilitychange`. **This refines `IMPLEMENTATION_PLAN.md`'s phase-6 bullet.** | §6.3 |
| Q10 | DOM shape | **No per-word `<span>`s.** One `<p>` per chunk; only the *active* chunk renders `text.slice(0,s)` + `<mark>text.slice(s,e)</mark>` + `text.slice(e)`. 100k word spans for a 330-chunk book is not a layout the browser should be asked to hold; this is 3 text nodes and one element, re-rendered only in the active paragraph. | §6.4 |
| Q11 | Re-render rate | `setState` fires **only when the word index changes** (≈2-3 Hz), guarded by a ref inside the rAF loop. Non-active chunks are `React.memo`'d on `(index, isActive, isMissing)`. Without this, a 60 Hz `setState` would re-render the whole reading pane 60×/s. | §7.4 |
| Q12 | **State management** | **React state + three hooks. No store, no React Query, no new dependency.** Justified in §7.3 against the repo's actual conventions, not by preference. The one deliberate exception: the playback engine lives in `useRef`, not state — updating it through React at 60 Hz is exactly the storm Q11 avoids. | §7.3 |
| Q13 | **SSR vs client rendering** | **Route shells (`layout.tsx`, `page.tsx`) are server components; every data-touching component is `"use client"`.** Not stylistic: the id token lives in a browser module variable (`lib/auth.ts`); only the httpOnly *refresh* cookie reaches the SSR Lambda, so a server component cannot call the API without adding a Cognito refresh round-trip to the render path of a Lambda competing for a 10-execution account budget (constraint 3). | §7.2 |
| Q14 | Routing | **Route group `app/(app)/`** holding the two-pane shell: `/` is the library, `/books/<id>` is the reader. Route groups don't appear in URLs, so `/login` and `/signup` keep their own bare layout and the shell isn't duplicated across two segments. `middleware.ts`'s existing matcher already gates `/books/*`. | §7.1 |
| Q15 | Polling cadence | **Reader: `GET /books/{id}/status` every 2 s, backing off to 5 s after 60 s, stopping on `terminal`, on 404, or after a 15-minute hard cap. Sidebar: `GET /books` every 5 s only while some book is non-terminal, and not at all when all are terminal.** Both suspend on `document.hidden` and fire one immediate poll on visibility. A forgotten tab at 2 s/poll is 43,000 requests/day against 5 executions of API headroom. | §10 |
| Q16 | `POST /books/{id}/resynthesize` | **`PARTIAL` only.** Order is load-bearing: (1) reset `FAILED` chunks to `PENDING`, (2) `update_status(EXTRACTED, expected_statuses=(PARTIAL,), chunks_done=total-reset, chunks_failed=0, clear_stitch_outputs=True, clear_failure_reason=True)`, (3) publish. Step 2's conditional is the exactly-once gate against a double-clicked button. When zero chunks were reset (the `STITCH_FAILED` case) it publishes a **stitch** message instead of a fan-out. **`_CLAIMABLE_STATUSES`/`_REISSUABLE_STATUSES` are untouched.** | §5 |
| Q17 | New Lambda? | **None.** The reader adds request load to the existing API Lambda and nothing else. The concurrency arithmetic of phase-5 §6.3 is unchanged: `2 synthesize + 1 extract + 2 stitch = 5`, leaving 5 for API + SSR. This is *why* §10's hidden-tab suspension is a correctness requirement and not a nicety. | §11 |
| Q18 | New bounded context? | **No.** Audio delivery, the manifest and resynthesis all read/mutate the Library's own `Book`/`Chunk` aggregates. Same answer as phase-3 OQ-1, phase-4 Q19, phase-5 Q15. | §4.1 |
| Q19 | **Playwright, given no audio in CI** | **Two projects, three specs.** `chromium` runs `upload-and-read.spec.ts` + `degraded.spec.ts` everywhere and asserts nothing about audio. `playback` (`channel: "chrome"`, gated on `E2E_AUDIO=1`) runs `playback.spec.ts` **only against local compose**, where `SYNTHESIS_STUB_MODE=silent` produces real frames and real marks. A new `e2e-local` job in `ci.yml` is the phase's real gate. | §13.4 |
| Q20 | Frontend upload | Direct browser → S3 presigned POST, **file field appended last**, no `Authorization` header on that request (it would break the signature). Client-side pre-checks against `upload.maxBytes` and `type === "application/pdf"`, because both failure modes come back from S3 as an opaque 403. Retry path is the existing `POST /books/{id}/upload-url`. | §8 |

---

## 3. The crux: how audio reaches the `<audio>` element

### 3.1 The constraint that forces the answer

`<audio src="...">` issues a plain browser GET. It cannot carry an `Authorization` header — there is no API for it, and `crossorigin`/`fetch` workarounds either don't apply to media loading or require buffering the whole file in JS (§3.2 kills that). Meanwhile `ApiStack` guards `/{proxy+}` with `HttpUserPoolAuthorizer`, whose default identity source is `$request.header.Authorization`. So:

> **Any design in which the browser's media request hits our API is dead on arrival**, unless the authorizer is reconfigured to read the JWT from a query parameter — which would put a Cognito id token into browser history, CloudFront access logs and CloudWatch, for every audio request.

That leaves the media request going somewhere that authenticates by URL. Two candidates.

### 3.2 The options, worked through

| option | verdict |
|---|---|
| **A. `GET /books/{id}/audio` → JSON with an S3 presigned GET URL** | **Chosen.** |
| B. API streams/proxies the bytes | **Rejected — impossible.** Lambda's response payload limit is 6 MB (and API Gateway buffers). A 300-page book is ~240 MB (phase-5 §7.4's own arithmetic). Even a 20-page book blows past it. There is no version of this that works. |
| C. CloudFront distribution over `audio_bucket` | **Rejected — cost/complexity out of proportion.** Details below. |
| D. Make `audio_bucket` public-read | **Rejected.** Every bucket in `storage_stack.py` is `BlockPublicAccess.BLOCK_ALL`. Object keys embed two UUIDs, so it is not *trivially* enumerable, but "unguessable URL" is not an access-control model and reversing the bucket posture for one convenience is not a trade this repo has made anywhere else. |
| E. API returns `302` to a presigned URL | **Rejected for the same reason as B's cousin:** the `<audio>` request that would follow the redirect never gets to make it, because the request *to the API* is the one that 401s. (It works fine for a `fetch()` caller — but a `fetch()` caller can't feed `<audio>` without buffering.) |

**Why not CloudFront (C), concretely.** Serving a private bucket through CloudFront means either OAC + public distribution (which is option D with extra steps — anyone with the URL gets the bytes, and CloudFront URLs are *more* guessable, not less) or **CloudFront signed URLs**. Signed URLs need: a CloudFront public key resource, a key group on the behaviour, the matching private key stored out of band and read by the API Lambda at request time, and a new `FrontendStack → StorageStack` dependency edge (today `FrontendStack.__init__` takes only `api_base_url`, `cognito_client_id`, `cognito_region`, `environment` — it has never seen a bucket). That is four new moving parts and one new secret to rotate, buying edge caching for a single user's personal library. The presigned-S3 answer needs **zero new infrastructure** — the API Lambda already holds `s3:GetObject` on `audio_bucket` via the existing `audio_bucket.grant_read_write(fn)`.

CloudFront becomes the right answer the day this app has more than one reader in more than one region. It is not that day.

### 3.3 Range requests, seeking, and expiry

**Range.** SigV4 query-string presigning signs the HTTP method, the host, the path and the query parameters — `SignedHeaders` for a presigned GET is just `host`. `Range` is therefore *unsigned*, and S3 honours whatever the browser sends. This is what makes `<audio>` seeking work at all: Chrome issues `Range: bytes=N-` on every seek and expects `206`. **This is the single assertion the smoke test makes that only a real S3 API can prove** (§12.2).

**Seek accuracy.** Phase-5 §7.1 recorded, deliberately, that the stitcher writes **no Xing header**, so a browser estimates byte position from the first frame's bitrate. For a uniform edge-tts book (`audio-24khz-48kbitrate-mono-mp3`, CBR) that estimate is exact. For a mixed-engine book (Google fallback on some chunks) it is approximate. Phase 6 **does not use** the manifest's `b0`/`b1` byte ranges, and that is a decision, not an omission: `audio.currentTime = ms/1000` is one line and correct for the common case. The failure mode is named and detectable — if `Math.abs(audio.duration*1000 - manifest.durationMs) / manifest.durationMs > 0.02`, the player logs a console warning and shows a "seeking may be imprecise" hint. `b0`/`b1` remain the escape hatch (fetch a segment by Range and play it standalone) if that warning ever fires in prod.

**Expiry.** `AUDIO_URL_EXPIRES_IN = 3600`. Two honest facts:

- A presigned URL made with the Lambda role's **temporary** credentials is void when those credentials expire, regardless of `ExpiresIn`. So the client must never treat `expiresIn` as a guarantee.
- Therefore the design is **reactive, not predictive**: on the `<audio>` `error` event (`MEDIA_ERR_NETWORK` / `MEDIA_ERR_SRC_NOT_SUPPORTED`), `usePlayback` re-requests `GET /books/{id}/audio`, restores `currentTime`, and resumes if it was playing. Bounded at 3 attempts, after which the state becomes `error: "URL_EXPIRED"` and the UI shows "Audio connection lost — reload to continue reading aloud", **with the text still fully readable**.

### 3.4 What this decision does *not* require

Stated so nobody goes looking for work that isn't there:

- **No CORS rule on `audio_bucket`.** A media element loading a cross-origin `src` without a `crossorigin` attribute is not a CORS request. (It would become one the day someone wires a `WebAudio` `AudioContext` for a waveform. Not this phase.)
- **No CORS rule on `marks_bucket`.** The browser never touches it — Q3 proxies that JSON through the API, whose CORS is already `allow_origins=["*"]`.
- **No change to `storage_stack.py`, `frontend_stack.py`, or `pipeline_stack.py`.**

---

## 4. New API surface

### 4.1 Placement — stays inside `contexts/library/`

Same argument as every prior phase (phase-3 OQ-1, phase-4 §3, phase-5 §3.1): these operations read and mutate the Library's own aggregates. New/changed layout (⊕ new, Δ changed):

```
backend/src/contexts/library/
├── domain/
│   └── storage.py                  Δ  + PresignedDownload, AudioDelivery (Protocol),
│                                      AUDIO_URL_EXPIRES_IN
├── application/
│   ├── delivery.py                 ⊕  GetBookAudioUrl, GetChunkAudioUrl,
│   │                                  GetBookManifest, GetChunkMarks
│   └── resynthesis.py              ⊕  ResynthesizeBook + command/result
├── infrastructure/
│   ├── s3_client.py                ⊕  public_s3_client() -- promoted verbatim from
│   │                                  s3_pdf_storage._build_client (§4.2)
│   ├── s3_pdf_storage.py           Δ  imports it instead of defining it
│   └── s3_audio_delivery.py        ⊕  S3AudioDelivery (presigned GET)
└── interface/
    ├── dependencies.py             Δ  + get_audio_delivery
    ├── schemas.py                  Δ  + audio_url_to_dict, resynthesis_to_dict
    └── controllers.py              Δ  + 4 routes
```

### 4.2 The presigner and the compose landmine

```python
# infrastructure/s3_client.py  (promoted from s3_pdf_storage._build_client)
def public_s3_client() -> Any:
    """An S3 client whose endpoint is reachable **from the browser**.

    boto3 signs presigned URLs against the client's own endpoint, so inside
    docker-compose ``src/infrastructure/aws.py``'s ``client("s3")`` would
    mint ``http://localstack:4566/...`` -- a hostname that resolves only on
    the compose network. ``settings.s3_public_endpoint_url``
    (``http://localhost:4566``) is the host-reachable override; it falls back
    to ``aws_endpoint_url``, and to None in real AWS where boto3 resolves the
    real regional endpoint. PLANS/phase-3.md §9.4, and now PLANS/phase-6.md
    §4.2: the presigned *download* has exactly the same failure mode as the
    presigned upload, and it fails silently -- the URL is well-formed, the
    signature is valid, and the browser simply cannot connect.
    """
```

Note it needs **no** laziness: unlike SQS, S3 has a global endpoint, so constructing the client with no region and no credentials does not raise (this is stated verbatim in `infrastructure/sqs_synthesis_queue.py`'s `_sqs` docstring, which is why *that* one had to be lazy). §13.2 keeps a regression test on it anyway.

```python
# domain/storage.py
AUDIO_URL_EXPIRES_IN = 3600   # PLANS/phase-6.md §3.3 -- reactive refresh, not a long expiry

@dataclass(frozen=True)
class PresignedDownload:
    url: str
    expires_in: int

class AudioDelivery(Protocol):
    """Read-side delivery port (PLANS/phase-6.md §3). Deliberately NOT a
    method on ObjectStorage: that port is the *workers'* write side, and its
    only two consumers (synthesize, stitch) must never learn how to mint a
    browser-fetchable URL."""
    def presigned_download(
        self, *, key: str, expires_in: int = AUDIO_URL_EXPIRES_IN
    ) -> PresignedDownload: ...  # pragma: no cover
```

```python
# infrastructure/s3_audio_delivery.py
class S3AudioDelivery:
    def __init__(self, *, bucket: str, client: Any | None = None) -> None:
        self._bucket = bucket
        self._client = client if client is not None else public_s3_client()

    def presigned_download(self, *, key: str, expires_in: int = AUDIO_URL_EXPIRES_IN) -> PresignedDownload:
        url = self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_in
        )
        return PresignedDownload(url=url, expires_in=expires_in)
```

### 4.3 The four routes

All four reach the book through `_load_owned_book` (via a use case), so another user's book is a **404, never a 403** — the repo's standing rule since phase 2.

```python
@router.get("/books/{book_id}/audio")
def get_book_audio(book_id, user=Depends(get_current_user),
                   book_repository=Depends(get_book_repository),
                   audio_delivery=Depends(get_audio_delivery)) -> dict:
    """Mints a presigned S3 GET for the stitched book.mp3 (§3). 409 when the
    book has no audioKey -- the everyday state of every non-prod environment
    (PLANS/phase-4.md §0), so it is a documented outcome, not an error path."""
```

```json
{"url": "https://…?X-Amz-Signature=…", "expiresIn": 3600,
 "durationMs": 1418240, "contentType": "audio/mpeg"}
```

| route | 200 | 4xx |
|---|---|---|
| `GET /books/{id}/audio` | the shape above | `409 ConflictError` when `book.audio_key is None`; 404 missing/foreign book |
| `GET /books/{id}/chunks/{n}/audio` | same shape, `durationMs` from `chunk.duration_ms` | `409` when `chunk.audio_key is None`; 404 missing chunk/book |
| `GET /books/{id}/manifest` | the `book.json` document **verbatim**, `Cache-Control: private, max-age=3600` | `404` when `manifest_key is None` **or** the object is gone (a wiped PR bucket) |
| `GET /books/{id}/chunks/{n}/marks` | the per-chunk marks document **verbatim**, `Cache-Control: private, max-age=86400` | `404` when `chunk.marks_key is None` — **the normal case for every chunk in a PR environment** |
| `POST /books/{id}/resynthesize` | `202`, §5.3's shape | `409` on any status but `PARTIAL`; 404 missing/foreign |

`GET /books/{id}/chunks/{n}/audio` ships **backend-only** this phase: it is ~10 lines, it is what makes phase-5 §7.2 invariant 3 ("a book whose concatenation failed still plays chunk by chunk") a real capability rather than a claim, and the UI that would use it is deferred — see OQ-1.

The manifest and marks routes return the stored bytes untouched, with `media_type="application/json"`. No re-serialization: the documents are already the contract (phase-5 §7.2 calls the manifest "a contract" in so many words), and re-shaping them in the API would create a second place for the schema to drift.

**Failure mode: `manifest_key` set, object missing.** Happens when a PR bucket is torn down under a still-open tab, or if a stitch wrote the DynamoDB row and the object was later deleted. `S3ObjectStorage.get_bytes` already raises `NotFoundError` → 404, and §9's degradation table turns that into "text only, no audio", not a crash.

---

## 5. `POST /books/{id}/resynthesize`

### 5.1 Why the recorded design is the right one

Phase-5 OQ-6 is unambiguous: today a prod book whose engines all failed is a **dead end** — it cannot be re-uploaded (`_REISSUABLE_STATUSES` excludes `PARTIAL`) and cannot be re-extracted (`_CLAIMABLE_STATUSES` excludes it). Adding `PARTIAL` to those tuples would "fix" it by reintroducing exactly the hazard phase-5 §3.1 invented `PARTIAL` to prevent: a stray redelivered S3 event claiming a book with perfectly good text, calling `delete_for_book`, and re-extracting it because the *audio* was unavailable.

The right lever is not re-upload. The PDF is fine, the text is fine, the chunks are fine; only synthesis failed. So the operation is: **rewind the chunks that failed, rewind the counters by the same amount, and re-fire the existing fan-out.** Nothing new is invented — `SynthesizeChunk`, the fan-in counter, `_maybe_publish_stitch` and `StitchBook` all run exactly as they do after a first extraction.

### 5.2 The use case

```python
# application/resynthesis.py

@dataclass(frozen=True)
class ResynthesizeBookResult:
    retried_chunks: int
    republished_stitch: bool   # True in the zero-failed-chunk (STITCH_FAILED) case

class ResynthesizeBook:
    def __init__(self, book_repository, chunk_repository,
                 synthesis_queue: SynthesisQueue, stitch_queue: StitchQueue,
                 clock: Clock) -> None: ...

    def execute(self, user_id: str, book_id: str) -> ResynthesizeBookResult: ...
```

Step by step, with the reason each ordering constraint exists:

1. `book = _load_owned_book(...)` → 404 for a missing or foreign book.
2. `if book.status is not BookStatus.PARTIAL: raise ConflictError("Book has no failed audio to retry")`.
   - `READY` → nothing failed. `EXTRACTED`/`STITCHING`/`EXTRACTING` → work is in flight; rewinding counters underneath it would corrupt the fan-in. `UPLOADED`/`FAILED` → already covered by `POST /books/{id}/upload-url`, which is what `_REISSUABLE_STATUSES` exists for.
3. Read `chunks = chunk_repository.list_for_book(book_id)`. Candidates are chunks whose status is **`FAILED` or `PENDING`**. `PENDING` is included for crash recovery: if a previous `/resynthesize` died between steps 4 and 5, some chunks are already `PENDING` on a still-`PARTIAL` book, and excluding them would make the retry a no-op forever.
4. For each candidate, `chunk_repository.update_status(book_id, i, ChunkStatus.PENDING, expected_statuses=(ChunkStatus.FAILED, ChunkStatus.PENDING), clear_failure_reason=True)`. A per-chunk `ConflictError` is **logged and the chunk dropped from the count** (something else claimed it — the chunk-level conditional claim is the authority, exactly as in phase-4 §8.4). `reset` is the list of indexes actually reset.
5. `book_repository.update_status(user_id, book_id, BookStatus.EXTRACTED, expected_statuses=(BookStatus.PARTIAL,), chunks_done=max(0, book.chunks_total - len(reset)), chunks_failed=0, clear_stitch_outputs=True, clear_failure_reason=True, updated_at=now)`.
   - `expected_statuses=(PARTIAL,)` is **the exactly-once gate**. A double-clicked button, or two tabs, produces one `202` and one `409`.
   - `clear_stitch_outputs=True` is not optional: leaving a stale `audioKey` pointing at a `book.mp3` assembled from the *old* chunk set would let the reader play audio that no longer matches the manifest it is about to be handed.
6. `if reset: synthesis_queue.enqueue_chunks(user_id=..., book_id=..., chunk_indexes=reset)` — **else** `stitch_queue.enqueue_book(user_id=..., book_id=...)`.

**Why the else branch exists.** A `PARTIAL`/`STITCH_FAILED` book has zero `FAILED` chunks (concatenation itself gave up; phase-5 §3.2's last row). With `reset == []`, `chunks_done` stays equal to `chunks_total`, no chunk will ever increment again, and therefore nothing would ever publish a stitch. Publishing one directly is correct: the book is now `EXTRACTED`, which is in `STITCHABLE_BOOK_STATUSES`, and the stitcher's writes are idempotent by construction (keys are pure functions of `(user_id, book_id)` — phase-5 §6.4).

### 5.3 Response

Mirrors `POST /books`'s existing `{"book": …, "upload": …}` envelope:

```json
{"retriedChunks": 12,
 "book": { …exactly book_status_to_dict(reloaded book)… }}
```

`202`, not `200`: nothing has been resynthesized yet; N messages have been published. The client drops `book` straight into its poll state and resumes polling — no extra round trip to learn it's back to `EXTRACTED`.

### 5.4 The ordering hazard, named

Between step 5 and the new work completing, the book sits at `EXTRACTED` with `chunks_done < chunks_total`. `EXTRACTED` is in `STITCHABLE_BOOK_STATUSES`, so a **stale in-flight stitch message** (plausible precisely here — the user is retrying *because* something failed, and the stitch queue's `max_receive_count=3` × 90-minute visibility means a message can still be alive) could claim the book and stitch a half-finished set.

It cannot, and the guard already exists:

```python
if book.chunks_total == 0 or book.chunks_done < book.chunks_total:
    return StitchBookResult("DEFERRED", reason="NOT_COMPLETE")
```

— `application/stitching.py`, checked **before** the claim. With `reset > 0` the counters are rewound, so a stale message is deferred and the completing increment publishes a fresh one. With `reset == 0` the counters are complete and a duplicate stitch is exactly what we asked for. Both cases get an explicit test (§13.2).

**What is deliberately *not* done:** `PARTIAL` is not added to `STITCHABLE_BOOK_STATUSES`, `_CLAIMABLE_STATUSES` or `_REISSUABLE_STATUSES`. §13.2's `test_status_tuples_unchanged` asserts all three tuples literally.

---

## 6. Highlight sync

### 6.1 The data, quoted

The manifest is a **segment** manifest (phase-5 §7.2, quoted verbatim):

```json
{
  "version": 1,
  "bookId": "6f1a…",
  "status": "PARTIAL",
  "audioKey": "audio/8a2c…/6f1a…/book.mp3",
  "durationMs": 1418240,
  "chunksTotal": 330,
  "chunksDone": 330,
  "chunksFailed": 2,
  "sampleRateHz": 24000,
  "segments": [
    {"i": 0, "t": 0,      "d": 118240, "s": 0,    "e": 1800,
     "audioKey": "audio/8a2c…/6f1a…/000000.mp3",
     "marksKey": "marks/8a2c…/6f1a…/000000.json",
     "b0": 0, "b1": 709440},
    {"i": 1, "t": 118240, "d": 121016, "s": 1800, "e": 3611,
     "audioKey": "…/000001.mp3", "marksKey": "…/000001.json",
     "b0": 709440, "b1": 1435536}
  ],
  "missing": [7, 42]
}
```

with the guarantees phase-5 §7.2 states as a contract: `segments` is **sorted ascending by `i`**, `segments[k].t == segments[k-1].t + segments[k-1].d` **exactly**, and `sum(d) == durationMs` **exactly** (§7.3's round-once-per-boundary rule). `s`/`e` here are **book-global character offsets**.

Word timings live in the per-chunk marks documents (`domain/marks.py`'s `build_marks_document`):

```json
{"version": 1, "bookId": "…", "chunkIndex": 7, "source": "edge-tts",
 "voice": "…", "timing": "measured", "audioKey": "…", "charStart": 12600,
 "charEnd": 14400, "durationMs": 118240, "wordCount": 297,
 "words": [{"t": 0, "d": 210, "s": 0, "e": 3, "w": "The"}, …]}
```

`words[].s`/`e` are **chunk-relative** — phase-5 §7.2 calls this asymmetry deliberate, and it is what makes §6.4's rendering trivial. `build_marks_document` asserts `words` is sorted by non-decreasing `t`, which is the precondition the binary search needs.

### 6.2 Fetch strategy: lazy per segment, one prefetch, bounded cache

**Rejected: fetch all marks up front.** 330 files × ~25 KB ≈ 6.6 MB and 330 requests before the first note plays, against an API Lambda with ~5 executions of headroom. It is the same arithmetic phase-5 §7.2 used to reject a merged word array, applied one level down.

**Rejected: fetch on demand with no prefetch.** The fetch would land in the rAF path at a chunk boundary, blanking the highlight for a round trip every ~2 minutes.

**Chosen:**

```ts
// lib/marks.ts
export type MarksEntry = WordMark[] | null;   // null == "known absent", negatively cached

export class MarksCache {
  /** Loaded, rebased marks for segment `i`, or undefined if not loaded. */
  get(i: number): MarksEntry | undefined;
  /** Idempotent: concurrent calls for the same `i` share one in-flight promise. */
  ensure(i: number, segmentStartMs: number): Promise<MarksEntry>;
  /** Drops entries further than `radius` from `centre` (default 4 -> ≤8 held). */
  evict(centre: number, radius?: number): void;
}
```

On entering segment `k`: `ensure(k)` (awaited into state) and `ensure(k+1)` fire-and-forget, then `evict(k)`. A 330-segment book fully traversed would otherwise hold ~100k word objects (~15 MB of JS heap); the cap holds ~2,400.

**The nastiest failure mode in this design, named:** a chunk that is `DONE` but whose marks object is missing (or whose chunk `FAILED`, the normal case in PR environments) returns 404. Without a **negative cache**, the rAF loop would re-request it on the very next frame — a 60 requests-per-second loop against the API Lambda, from a UI that looks merely "stuck". Hence `MarksEntry = WordMark[] | null` and `getMarks()` in `lib/books.ts` returning `null` on 404 rather than throwing. §13.1 tests this explicitly.

Rebasing happens once, at insert:

```ts
/** t_global = segment.t + word.t (PLANS/phase-5.md §7.2 -- "phase 6 rebases a
 *  word with one addition"). `s`/`e` are left CHUNK-relative on purpose: they
 *  index into the chunk's own rendered text (PLANS/phase-4.md §7.5's
 *  deliberate asymmetry), which is what lets ChunkParagraph slice without any
 *  global-offset bookkeeping. */
export function rebaseMarks(words: WordMark[], segmentStartMs: number): WordMark[];
```

### 6.3 The loop: rAF, not `timeupdate`

`IMPLEMENTATION_PLAN.md`'s phase-6 bullet says *"syncs highlight to `<audio>` `timeupdate` via word-boundary data (binary search into timestamp array)"*. **This plan refines that** and says why:

- Chrome/Firefox fire `timeupdate` roughly every **250 ms** (the spec permits 15-250 ms; implementations sit at the ceiling). That is ~4 Hz.
- At 150 wpm a word is ~400 ms, and function words ("the", "of", "a") are commonly **120-200 ms**. A 250 ms sampler therefore *skips them outright* and lags the rest by up to 250 ms — visible as a highlight that jerks and occasionally jumps two words.

So:

| signal | rate | drives |
|---|---|---|
| `requestAnimationFrame` (while `!audio.paused`) | ~60 Hz | the word/chunk highlight |
| `timeupdate` | ~4 Hz | the scrubber position and the time readout |
| `visibilitychange`, `seeked`, `play` | event | a forced full resync |

rAF is throttled to ~0 Hz in a background tab. That is **correct** (there is no highlight to see) and self-healing: the first frame after refocus fails `stillInside`, triggers the full binary search, and resnaps. `visibilitychange` also forces it so the resnap doesn't wait for a paused tab's first frame.

**Cost control — the reason this is affordable at 60 Hz:**

```ts
// hot path, once per frame
const t = audio.currentTime * 1000;
if (stillInside(words, wordIdx, t)) return;     // ~59 of 60 frames: one comparison
// otherwise: bisect (≤9 comparisons), and only then touch React state
```

`stillInside` is O(1); the binary searches (`locateSegment` over ≤330 entries, `locateWord` over ~300) are ≤9 comparisons each and run at word rate (~2.5 Hz) or on a seek. React `setState` fires **only when the word index actually changes** (Q11) — so the reading pane re-renders ~2-3 times per second, not 60.

```ts
// lib/manifest.ts
/** Index into `segments` of the segment covering `tMs`; -1 before the first.
 *  bisectRight(segments, tMs, by .t) - 1, clamped to the last segment past the
 *  end. Segments partition the timeline contiguously (PLANS/phase-5.md §7.2:
 *  `segments[k].t === segments[k-1].t + segments[k-1].d` exactly), and
 *  `missing` chunks are ABSENT from `segments` rather than represented as
 *  silence (phase-5 Q10), so this can never land "inside a gap" -- the
 *  returned index is a position in `segments`, NOT a chunk index. */
export function locateSegment(segments: Segment[], tMs: number): number;

// lib/marks.ts
/** bisectRight(words, tMs, by .t) - 1; -1 before the first word. Ties resolve
 *  to the LAST word sharing that `t` -- build_marks_document only asserts
 *  NON-decreasing `t`, and when two words claim the same millisecond the
 *  later one is the one that should be lit. */
export function locateWord(words: WordMark[], tMs: number): number;

/** True while `tMs` is still inside word `i`'s span under §6.4's start-boundary
 *  rule. Exactly consistent with locateWord by construction. */
export function stillInside(words: WordMark[], i: number, tMs: number): boolean;
```

### 6.4 Boundary rule and rendering

**A word is lit from `words[i].t` until `words[i+1].t`** — not `[t, t+d)`. Three consequences, all good:

- The partition is total: there is never a frame with no word lit, so the highlight never flickers off between words.
- `stillInside(words, i, t) === t >= words[i].t && (i+1 >= n || t < words[i+1].t)` is exactly the inverse of `locateWord`, so the fast path and the slow path can never disagree.
- Phase-5 §7.2's warning — *"it must clamp — the last word's `t + d` can exceed the segment duration by a few ms because engines report word ends optimistically"* — becomes moot. `d` is kept in the type (a future "progress within the word" effect would want it) and is unused for highlighting.

**Rendering.** No per-word spans:

```tsx
// components/ChunkParagraph.tsx
type Props = {
  chunk: Chunk;             // from GET /books/{id}/chunks
  active: boolean;
  missing: boolean;         // index ∈ manifest.missing
  wordStart: number;        // CHUNK-relative; -1 when unknown/inactive
  wordEnd: number;
};
// active && wordStart >= 0  ->  {text.slice(0,wordStart)}<mark …>{text.slice(wordStart,wordEnd)}</mark>{text.slice(wordEnd)}
// otherwise                 ->  {text}
export default React.memo(ChunkParagraph);   // re-renders only when its own props change
```

Three text nodes and one element per active-paragraph update, and `React.memo` keeps the other 329 paragraphs untouched. The `<mark>` carries `data-testid="active-word"` — the single hook every Playwright and RTL highlight assertion uses.

**When marks for the active segment aren't loaded yet** (`wordStart === -1`), the paragraph still gets `data-active="true"` and a subtle background tint, so the user always sees *where* they are even during the one-round-trip gap. Named because "the highlight vanished" would otherwise read as a bug.

**Auto-scroll** fires on **chunk** change, not word change: `markRef.current?.scrollIntoView({block: "center", behavior: "smooth"})`. A "follow along" toggle (default on) turns itself off when the user scrolls manually (a `scroll` listener that ignores the ~600 ms after a programmatic scroll), so scrolling back to re-read something doesn't fight the player.

---

## 7. Frontend architecture

### 7.1 Routes

```
frontend/app/
├── layout.tsx                       Δ  unchanged (html/body/globals.css)
├── login/page.tsx                      unchanged
├── signup/…                            unchanged
└── (app)/                           ⊕  route group -- does NOT appear in URLs
    ├── layout.tsx                   ⊕  server: <div class="flex h-screen"><BookSidebar/>{children}</div>
    ├── page.tsx                     ⊕  server: "/" -- <LibraryEmptyState/>
    └── books/[bookId]/page.tsx      ⊕  server: <ReaderView bookId={…}/>
```

A route group rather than a `/books` prefix on everything: `/` stays the library (no redirect to write or test), `/books/<id>` is the reader, and the two-pane shell is written once. `/login` and `/signup` sit outside the group and keep their bare centred layout. `middleware.ts`'s matcher `["/((?!_next|api/auth|.*\\..*).*)"]` already gates `/books/<uuid>` — **no middleware change**.

`app/page.tsx`'s current `HealthBadge` moves to `components/HealthBadge.tsx` and is rendered in the sidebar footer next to the existing `UserBadge`; `__tests__/page.test.tsx` moves with it to `__tests__/health-badge.test.tsx` (same assertions, new import path).

### 7.2 SSR vs client rendering — the decision, with the reason

**Every component that touches the API is `"use client"`. Route shells are server components that render them and nothing else.**

This is not a style preference. `lib/auth.ts` keeps the id token in a **browser module variable**, deliberately (phase-1 §1.1): the only credential the SSR Lambda ever sees is the httpOnly `bookloud_refresh` cookie. A server component that wanted to call `GET /books` on the user's behalf would have to:

1. read the refresh cookie,
2. call Cognito's `InitiateAuth` to mint an id token,
3. call the API,

— i.e. add two network round trips to the render path of a Lambda that is competing for an account-wide budget of 10 concurrent executions (constraint 3), on every navigation, for data that the client is about to start polling anyway. Every existing page (`app/page.tsx`, `login`, `signup`) is `"use client"` for the same reason.

What the SSR Lambda keeps doing: routing, `metadata`, the HTML shell, and the `app/api/auth/*` BFF handlers. Those are cheap and have no API dependency, so a cold start costs one render, not one render plus two round trips.

No `loading.tsx` streaming and no server-side `fetch` against the API anywhere in this phase.

### 7.3 State management — React state, three hooks, no new dependency

**What the repo actually does today:** zero runtime dependencies beyond `next`/`react`/`react-dom`; cross-component session state held in a plain module variable with a single-flight promise (`lib/auth.ts`); tests that are either pure-function vitest or RTL renders of a component with `global.fetch` stubbed (`__tests__/api.test.ts`, `__tests__/login.test.tsx`).

**What this phase actually needs to share:** the book list (sidebar), the open book's status (sidebar badge + reader header), and playback position (reading pane + player bar). Three pieces, one screen, one user.

So: **three hooks and React state.** No Zustand, no Redux, no React Query, no SWR.

- `hooks/useBookList.ts` — list + poll + optimistic insert on upload.
- `hooks/useBookStatus.ts` — one book's `GET /books/{id}/status` poll (§10).
- `hooks/usePlayback.ts` — the audio engine (§7.4).

Sharing between the sidebar and the reader goes through **one React context** created in `app/(app)/layout.tsx`'s client child (`BooksProvider`), holding `useBookList`'s return value. That is 30 lines and no dependency. Importing React Query here would add a cache-invalidation model, a devtools story and a `QueryClientProvider` to every existing test's render tree — for one list and one poll.

**Where this would stop being true:** phase 7's chat sidebar adds streamed messages and a third long-lived state slice. If that turns into cross-cutting invalidation, revisit *then*, with a concrete problem, rather than pre-emptively now.

### 7.4 `usePlayback` — the one place refs beat state

```ts
export type PlaybackState = {
  ready: boolean;
  playing: boolean;
  positionMs: number;             // from `timeupdate` (~4Hz) -- scrubber only
  durationMs: number;             // from the manifest, NOT audio.duration
  segmentIndex: number;           // index into manifest.segments; -1 before the first
  chunkIndex: number;             // segments[segmentIndex].i; -1 when none
  wordStart: number;              // CHUNK-relative char offset; -1 when unknown
  wordEnd: number;
  marksPending: boolean;          // active segment's marks still loading
  error: "NO_AUDIO" | "URL_EXPIRED" | "DECODE_FAILED" | null;
};

export function usePlayback(bookId: string, manifest: BookManifest | null): {
  state: PlaybackState;
  audioRef: React.RefObject<HTMLAudioElement>;
  play(): void;
  pause(): void;
  seekMs(ms: number): void;
  seekToChunk(chunkIndex: number): void;   // "read from here" clicks in the pane
};
```

Held in refs, never in state: the `HTMLAudioElement`, the `MarksCache`, the current word/segment indexes, the rAF handle, the URL-refresh attempt counter. State is written only when a *rendered* value changes — which is what keeps a 60 Hz loop at ~3 renders/second.

The `<audio>` element is rendered hidden (`preload="metadata"`, no `controls`), because the player bar needs a seek-to-chunk affordance and consistent styling that native controls can't provide. The scrubber is `<input type="range" min={0} max={durationMs}>` — keyboard-accessible for free, and trivially drivable from RTL with `fireEvent.change`.

Enumerated failure handling inside the hook:

| failure | handling |
|---|---|
| `manifest.audioKey === null` | `error: "NO_AUDIO"`, no `<audio src>` mounted at all, `play()` is a no-op |
| `GET /books/{id}/audio` → 409 | same as above (they agree by construction; the 409 is the authority) |
| `<audio>` `error` event | re-request the URL, restore `currentTime`, resume if it was playing; ≤3 attempts, then `error: "URL_EXPIRED"` |
| decode failure (`MEDIA_ERR_DECODE`) | `error: "DECODE_FAILED"` — surfaced verbatim, never retried; this is the signal that would catch a bad stitch |
| marks 404 | negative-cached; `wordStart = -1`, chunk-level highlight only |
| `abs(audio.duration*1000 - manifest.durationMs)/durationMs > 0.02` | console warning + a "seeking may be imprecise" hint (§3.3) |

---

## 8. Upload flow

```ts
// lib/upload.ts  (PURE -- all four failure modes are unit-testable)
export type PresignedUpload = { url: string; fields: Record<string,string>;
                                key: string; expiresIn: number; maxBytes: number };

/** S3 requires the `file` field LAST in the multipart body -- every field
 *  after it is ignored, which shows up as a signature failure with no
 *  explanation. Also pre-checks the two conditions the presigned POST pins
 *  (Content-Type: application/pdf, content-length-range 0..maxBytes), because
 *  both come back from S3 as an opaque 403 that cannot be explained to a
 *  user after the fact. */
export function buildUploadForm(upload: PresignedUpload, file: File): FormData;
export class UploadTooLarge extends Error {}
export class UploadNotAPdf extends Error {}
```

Flow in `components/UploadButton.tsx`:

1. `<input type="file" accept="application/pdf">`; on change, validate locally (`UploadNotAPdf`, `UploadTooLarge` — `MAX_UPLOAD_BYTES` is 50 MiB, surfaced by the API as `upload.maxBytes`, never hard-coded in the frontend).
2. `POST /books {title}` where `title` defaults to the filename minus `.pdf` (the backend's `Book.create` validates and normalizes; a blank title comes back as a **400**, which is why the request is permissive by design — see `CreateBookRequest`'s comment).
3. `fetch(upload.url, {method: "POST", body: buildUploadForm(upload, file)})` — **no `Authorization` header** (the presigned POST is self-authenticating and an extra header is at best ignored, at worst a signature mismatch). Expect `204`.
4. On success: optimistically insert the returned `book` (status `UPLOADED`) into the list, navigate to `/books/<id>`, and let §10's poll take over from `UPLOADED`.
5. On a failed S3 POST: one retry through `POST /books/{id}/upload-url` (legal — `UPLOADED ∈ _REISSUABLE_STATUSES`) with a fresh presign, then surface "Upload failed — try again" with the book row left in `UPLOADED` (which is re-issuable, so nothing is orphaned).

Progress: `fetch` cannot report upload progress. Rather than swap in `XMLHttpRequest` for a progress bar, the button shows an indeterminate "Uploading…" state. A 50 MiB cap on a personal library makes a determinate bar not worth an XHR code path.

---

## 9. Graceful degradation — the explicit user requirement

Constraint 2, recorded in phase-4 §0: *"on the UI just show a simple message.. but allow the user to continue reading the book or whatever they can with no external service."* Phase-5 §3.2 made this a data property; this section makes it a rendering table. `components/AudioNotice.tsx` owns every row, and §13.1 tests one case per row.

The book-level `failureReason` picks the headline; the **modal per-chunk** `failureReason` (from `GET /books/{id}/chunks`, already available) picks the wording.

| condition | reading pane | player | notice |
|---|---|---|---|
| `status ∈ {UPLOADED, EXTRACTING}` | skeleton | absent | "Reading your PDF…" |
| `status ∈ {EXTRACTED, STITCHING}` | **full text** (chunks already exist — `application/extraction.py` writes chunks *before* the `EXTRACTED` flip) | disabled | "Preparing audio — {percent}%" + progress bar |
| `FAILED` | nothing to render | absent | "We couldn't read this PDF ({failureReason})." + **Re-upload** button (`POST /books/{id}/upload-url`; legal, `FAILED ∈ _REISSUABLE_STATUSES`) |
| `READY` | full | enabled | — |
| `PARTIAL`, `failureReason: null`, `missing.length > 0` | full | **enabled** | "{n} of {m} sections have no audio — playback skips them." Missing chunks get an inline muted marker. |
| `PARTIAL`, `failureReason: "NO_AUDIO"`, chunks modally `EXTERNAL_TTS_DISABLED` | full | absent | "Text-to-speech is turned off in this environment. **You can still read the book.**" + **Try audio again** |
| `PARTIAL`, `failureReason: "NO_AUDIO"`, chunks modally `ALL_ENGINES_FAILED` | full | absent | "Every text-to-speech engine failed for this book. **You can still read it.**" + **Try audio again** |
| `PARTIAL`, `failureReason: "STITCH_FAILED"` | full | disabled (see OQ-1) | "Audio couldn't be assembled." + **Try audio again** |
| manifest 404 (object gone) | full | absent | "Audio information is unavailable. You can still read the book." |

Row 6 is the **steady state of every environment CI can reach except local compose** (phase-4 §0). It is therefore the row the PR pipeline's Playwright run actually exercises, and the one whose copy matters most (§13.4).

Two rules that hold across every row:

1. **If chunk text exists, it renders.** There is no state in which the reading pane is replaced by an error screen while text is available.
2. **"Try audio again" appears on exactly the `PARTIAL` rows**, and posts to `/resynthesize`. On `202` the UI drops the returned `book` into state and resumes polling from `EXTRACTED`; on `409` it refetches status (someone else already retried).

---

## 10. Polling

Project-level decision (`IMPLEMENTATION_PLAN.md`): status is surfaced by **polling**, not WebSockets or SSE. Nothing here revisits that.

### 10.1 Reader — `useBookStatus(bookId)`

| aspect | value | why |
|---|---|---|
| endpoint | `GET /books/{id}/status` | one `GetItem`, no extra I/O vs `GET /books/{id}` (phase-5 §8.1) |
| interval | 2 s for the first 60 s, then 5 s | extraction of a small book finishes in seconds; synthesis of a big one takes minutes |
| **stop condition** | `payload.terminal === true` | **never** a client-side status list. That is phase-5 §8.1's entire argument: `status === "READY"` hangs forever on a `PARTIAL` book, i.e. on every book in local dev and every PR environment |
| other stops | `404` (book deleted elsewhere) → navigate to `/`; `401` → `authFetch` already bounces to `/login` | |
| hard cap | 15 minutes → state `stalled`, "Still processing. Reload to keep watching." | a stranded book (phase-5 §4.3's named residual hole, backstopped in phase 8) would otherwise poll from a tab left open overnight |
| visibility | suspend on `document.hidden`; on visible, poll **immediately**, then resume the interval | constraint 3: a hidden tab at 2 s/poll is ~43,000 requests/day against ~5 executions of API headroom |
| transitions | `UPLOADED → EXTRACTING → EXTRACTED → STITCHING → READY\|PARTIAL`, or `→ FAILED` from extraction | `percent` is only meaningful from `EXTRACTED` (before that `chunksTotal == 0`, and `book_status_to_dict._percent` already returns 0 for non-terminal / 100 for terminal — the reader shows an indeterminate bar for `chunksTotal === 0`) |
| on terminal | stop polling; fetch `GET /books/{id}/chunks`, `GET /books/{id}/manifest`, `GET /books/{id}/audio` (the last two only when `audio.manifestKey` / `audio.audioKey` are non-null) | |

One subtlety worth stating: the reader fetches `GET /books/{id}/chunks` **as soon as `chunksTotal > 0`**, not on terminal — the text is readable from `EXTRACTED` onward and making the user wait for synthesis to read is precisely what constraint 2 forbids.

### 10.2 Sidebar — `useBookList()`

`GET /books` every **5 s while any book is non-terminal**, and **no interval at all** when every book is terminal (the common steady state — a personal library that isn't processing anything should generate zero background traffic). Recomputed after every response. Same `document.hidden` suspension. A manual refresh affordance covers the "I uploaded from another tab" case without polling forever.

When the reader is open, both hooks are live: the list poll (5 s, N books) and the reader poll (2 s, one book). That is deliberate — the sidebar badge and the reader header must not disagree, and merging them would mean the list poll's cadence governs the screen the user is actually watching. Two GETs per 2-5 s against one Lambda is not a load problem; a stale reader is a UX problem.

---

## 11. Infrastructure

Only one file changes, plus its wiring:

**`infra/stacks/api_stack.py`** (Δ):

```python
def __init__(self, scope, construct_id, *, table, pdf_bucket, audio_bucket, marks_bucket,
             extract_queue: sqs.Queue,
             synthesize_queue: sqs.Queue,      # NEW -- POST /books/{id}/resynthesize
             stitch_queue: sqs.Queue,          # NEW -- the zero-failed-chunk branch (§5.2 step 6)
             user_pool, user_pool_client, environment, git_sha, **kwargs): ...
```

```python
environment={
    …unchanged…,
    Config.ENV_SYNTHESIZE_QUEUE_URL: synthesize_queue.queue_url,
    Config.ENV_STITCH_QUEUE_URL: stitch_queue.queue_url,
}
synthesize_queue.grant_send_messages(fn)
stitch_queue.grant_send_messages(fn)
```

**`infra/app.py`** (Δ): two lines, `synthesize_queue=pipeline.synthesize_queue, stitch_queue=pipeline.stitch_queue`. The `Api → Pipeline` edge already exists (`extract_queue`), so there is no new dependency direction and no cycle.

Explicitly **unchanged**, stated so nobody goes looking:

- **`storage_stack.py`** — no CORS on `audio_bucket`/`marks_bucket` (§3.4), no new bucket, no lifecycle rule.
- **`frontend_stack.py`** — no new CloudFront behaviour, no origin over the audio bucket (§3.2 option C).
- **`pipeline_stack.py`** — no new queue, no new Lambda, no ESM change.
- **Reserved concurrency** — **still absent everywhere, and no `reserved_concurrent_executions` is added to any function.** Constraint 3: this account's total Lambda concurrency is 10 and AWS rejects *every* value (phase-4 §0's §6.3 correction; caught as a `CREATE_FAILED` on PR #5). §13.3 adds the `Match.absent()` assertion to `ApiFunction` too, since this is the phase that puts real load on it.
- **`GOOGLE_TTS_SECRET_NAME`** — still `""` by default in every environment including prod (`infra/app.py`'s existing default). Nothing here touches it.
- **Concurrency budget** — unchanged at `2 (synthesize) + 1 (extract) + 2 (stitch) = 5`, leaving 5 for the API and SSR Lambdas. No new function is created. The reader adds *requests* to the existing API Lambda, which is why §10's hidden-tab suspension is a correctness requirement rather than a nicety.

---

## 12. Local dev and the smoke test

### 12.1 Compose / Makefile

- **`docker-compose.yml`**: no change. The `backend` service already has `SYNTHESIZE_QUEUE_URL` and `STITCH_QUEUE_URL` set "for symmetry" (its own comment says "unused by the API today") — as of this phase they are used, and that comment is updated to say so. `S3_PUBLIC_ENDPOINT_URL=http://localhost:4566` is already set, which is what makes §4.2's presigned download reachable from the browser.
- **`Makefile`**: new `e2e-local` target — `up`, `ui`, `npx playwright install --with-deps chromium chrome`, `E2E_AUDIO=1 npm run test:e2e`, `down`. The existing `e2e` target (which just runs the smoke test against a compose stack) is folded into it.
- **`frontend/package.json`**: `"test:e2e:local": "E2E_AUDIO=1 playwright test"`.

### 12.2 `local/smoke_test.py` — stdlib only, and what it can honestly assert

Two new checks, both branching on the existing `--expect-synthesis {failed,silent}` flag. `failed` is what `deploy-pr.yml` gets; `silent` is what `make smoke` passes.

**`check_audio_delivery(api_url, book_id, id_token, expect_synthesis)`**

*`failed` mode (every PR environment, and the default):*

1. `GET /books/{id}/audio` → **409**. The single assertion that proves the endpoint exists, is authorized, and correctly refuses a book with no audio.
2. `GET /books/{id}/manifest` → **200**, `segments == []`, `missing == [0..N-1]`, `audioKey is None`, `durationMs == 0`. Proves the manifest is fetchable through the API and that the zero-segment document phase-5 §3.2 promises is real.
3. `GET /books/{id}/chunks/0/marks` → **404** (the chunk failed; no marks object exists).

*`silent` mode (local compose only — the one place bytes exist):*

4. `GET /books/{id}/audio` → **200**; `url` is a string containing `X-Amz-Signature`.
5. Fetch that URL with a plain `urllib` GET and **no Authorization header** → `200`, `Content-Type: audio/mpeg`, `Content-Length > 0`. *This is the assertion that proves the presign works unauthenticated — the whole point of §3.*
6. Re-fetch with `Range: bytes=0-1023` → **`206`** with exactly 1024 bytes and a `Content-Range` header. **This is the load-bearing seeking assertion, and only a real S3 API can produce it** — no unit test, no mock and no fake can prove that a URL signed one way accepts an unsigned `Range` header.
7. `GET /books/{id}/manifest` → segments contiguous (`t[k] == t[k-1] + d[k-1]` for all k) and `sum(d) == durationMs` exactly. Re-asserts phase-5 §7.3's rounding contract **through the API**, since that contract is what phase 6's binary search rests on.
8. `GET /books/{id}/chunks/0/marks` → 200, non-empty `words`, `t` non-decreasing, `charStart == manifest.segments[0].s`.

**`check_resynthesize(api_url, book_id, id_token, expect_synthesis)`**

- *`failed` mode:* the book is `PARTIAL`/`NO_AUDIO`. `POST /books/{id}/resynthesize` → **202**, `retriedChunks == chunksTotal`, `book.status == "EXTRACTED"`, `book.terminal is False`. Then poll back to terminal and assert `PARTIAL`/`NO_AUDIO` again — **the loop closes**, which proves the fan-out was really re-published and really consumed. This is the *only* end-to-end proof of `/resynthesize` any automated check can produce, and it happens to be a complete one.
- *`silent` mode:* the book is `READY`, so `POST /books/{id}/resynthesize` → **409**. Proves the status guard from the other side.

What remains uncovered by any automated check, and where the coverage lives instead:

| uncovered | covered by |
|---|---|
| real edge-tts audio playing in a real browser | nothing — phase-4 §0's accepted trade-off, unchanged |
| the presigned URL against **real** S3 (vs LocalStack) | nothing automated; the shapes are identical and `deploy-pr` proves the 409 path. Manual check on the PR environment once prod has audio. |
| CloudFront + SSR Lambda + API Gateway with a real browser | `deploy-pr`'s Playwright run (OQ-2) |

---

## 13. Test plan

### 13.1 Frontend — vitest (`frontend/__tests__/`)

**Pure modules — these carry the design.**

- **`manifest.test.ts`** (`lib/manifest.ts`)
  1. `locateSegment` returns `-1` for `t` before `segments[0].t`.
  2. exact boundary `t === segments[k].t` selects `k`, not `k-1`.
  3. mid-segment `t` selects the containing segment.
  4. `t` past the end clamps to the last segment (a browser can report `currentTime` a few ms past `duration`).
  5. single-segment manifest.
  6. empty `segments` → `-1`.
  7. a manifest with `missing: [1,3]` — segment indexes are positions in `segments`, **not** chunk indexes, and the timeline is still contiguous.
  8. a 330-segment sweep: for every segment, `locateSegment(segs, seg.t)` and `locateSegment(segs, seg.t + seg.d - 1)` both return that segment.
  9. `segmentsAreContiguous(manifest)` (the dev-mode assertion) returns false for a hand-corrupted manifest with a gap.
- **`marks.test.ts`** (`lib/marks.ts`)
  1. `rebaseMarks` adds `segmentStartMs` to every `t` and leaves `s`/`e` **untouched** (the phase-4 §7.5 asymmetry — this is the test that catches someone "helpfully" globalizing them).
  2. `locateWord`: before-first `-1`; exact `t` boundary; mid-word; past-end clamps to last; empty → `-1`.
  3. tie-breaking: two words sharing a `t` → the **later** index wins.
  4. round-trip: `locateWord(words, words[i].t) === i` for every `i` in a 300-word fixture.
  5. `stillInside(words, i, t)` is true for `t ∈ [words[i].t, words[i+1].t)` and false at `words[i+1].t` exactly.
  6. `stillInside` for the last word is true for any `t >= words[n-1].t`.
  7. `stillInside` and `locateWord` agree across a sweep of 1,000 sampled `t` values (the fast-path/slow-path consistency property, stated as a decision in Q8).
  8. `MarksCache.ensure` dedupes concurrent calls for the same index into one fetch.
  9. `MarksCache` stores `null` for a 404 and **never re-requests it**.
  10. `MarksCache.evict(centre)` keeps ≤8 entries and always keeps `centre`.
- **`upload.test.ts`** — `buildUploadForm` emits every presigned field **before** `file`, and `file` last; `UploadTooLarge` above `maxBytes`; `UploadNotAPdf` for a non-`application/pdf` type; a zero-byte file is allowed (S3's condition is `0..maxBytes`).
- **`books-api.test.ts`** (`lib/books.ts`) — each of the 8 client functions issues the right method + path through `authFetch`; `getAudioUrl` throws `NoAudioError` on 409; `getMarks` returns `null` on 404 (**not** throw — the negative-cache contract); `resynthesize` surfaces 409 as `AlreadyRetryingError`.

**Hooks (RTL + fake timers).**

- **`use-book-status.test.tsx`** — polls every 2 s while `terminal:false`; **stops** on `terminal:true`; stops on 404; backs off 2→5 s after 60 s; suspends on `document.hidden` and fires an immediate poll on visibility; enters `stalled` after 15 min; cleans its interval up on unmount.
- **`use-playback.test.tsx`** (stubbed `HTMLMediaElement`, controllable rAF)
  1. advancing `currentTime` advances the word index.
  2. `setState` fires **only** on index change — assert the render count over 60 simulated frames spanning 2 words is ≤3.
  3. crossing a segment boundary triggers exactly one marks fetch for the new segment and one prefetch for the next.
  4. a 404 marks response leaves `wordStart === -1`, `marksPending === false`, and **zero** further requests over 120 frames.
  5. an `error` event triggers exactly one URL refresh and restores `currentTime`; a fourth consecutive failure yields `error: "URL_EXPIRED"`.
  6. `manifest.audioKey === null` → no `src` is ever set and `play()` is a no-op.
  7. `visibilitychange` to visible forces a full resync (binary search runs even when `stillInside` would have passed).

**Components.**

- **`reading-pane.test.tsx`** — renders every chunk's text; exactly one `[data-testid="active-word"]` exists; its text equals `chunk.text.slice(wordStart, wordEnd)`; non-active `ChunkParagraph`s do **not** re-render when the word changes (render-counting spy — the `React.memo` guarantee); a `missing` chunk renders its muted marker and stays readable; `wordStart === -1` renders the chunk-level tint and no `<mark>`.
- **`audio-notice.test.tsx`** — **one test per row of §9's table**, asserting the exact user-visible copy, and a shared assertion that every row with text present also renders the reading pane. Plus: `EXTERNAL_TTS_DISABLED` vs `ALL_ENGINES_FAILED` pick different copy; "Try audio again" appears on exactly the `PARTIAL` rows.
- **`book-sidebar.test.tsx`** — titles + status pills; a non-terminal book shows `progress.percent`; a terminal book shows none; clicking a row navigates to `/books/<id>`; the upload control is present.
- **`player-bar.test.tsx`** — play/pause toggles and calls the hook; the range input's `change` maps to `seekMs`; disabled when `error !== null`; the time readout uses `manifest.durationMs`, not `audio.duration`.
- **`upload-button.test.tsx`** — happy path calls `POST /books` then POSTs the form to `upload.url` **without** an `Authorization` header; an oversized file never reaches the network; a failed S3 POST re-issues via `POST /books/{id}/upload-url` exactly once.
- **`health-badge.test.tsx`** — moved from `page.test.tsx`, assertions unchanged.

### 13.2 Backend — pytest (coverage gate stays `--cov-fail-under=90`; **zero AWS credentials, no region**)

- **`test_delivery_use_cases.py`** ⊕ — `GetBookAudioUrl` returns the presign for `book.audio_key`; raises `ConflictError` when it is `None`; `NotFoundError` for a foreign book (never `403`). Same three for `GetChunkAudioUrl`. `GetBookManifest` returns the stored bytes verbatim; `NotFoundError` when `manifest_key is None`; `NotFoundError` when the object is absent. `GetChunkMarks` likewise.
- **`test_s3_audio_delivery.py`** ⊕ (moto) — the URL contains the bucket, the key, and `X-Amz-Signature`; `expires_in` reaches `X-Amz-Expires`; **the client is built from `s3_public_endpoint_url` when set, from `aws_endpoint_url` otherwise, and from neither in real AWS** (§4.2's compose landmine — the one bug here that would be invisible in every test that didn't check it); constructing `S3AudioDelivery(bucket=…)` with `AWS_DEFAULT_REGION`/`AWS_REGION` deleted does not raise (the phase-4/5 `NoRegionError` regression shape).
- **`test_resynthesis_use_case.py`** ⊕
  1. resets only `FAILED` chunks, to `PENDING`, with `clear_failure_reason=True`.
  2. publishes `enqueue_chunks` with exactly those indexes, once.
  3. the book update carries `EXTRACTED`, `expected_statuses=(PARTIAL,)`, `chunks_done == total - len(reset)`, `chunks_failed=0`, `clear_stitch_outputs=True`, `clear_failure_reason=True`.
  4. **ordering asserted with a call-recording spy: chunks → book → queue** (phase-4 §8.2 step 7's rule).
  5. zero-`FAILED` (`STITCH_FAILED`) case: publishes a **stitch** message, no fan-out, `chunks_done` unchanged at `chunks_total`.
  6. `PENDING` chunks are included as candidates (the crash-recovery path).
  7. a per-chunk `ConflictError` is logged, the chunk excluded from the count, and the run continues.
  8. `ConflictError` for every non-`PARTIAL` status (`UPLOADED`, `EXTRACTING`, `EXTRACTED`, `STITCHING`, `READY`, `FAILED`) — parametrized.
  9. double execution → one success, one `ConflictError` from the conditional book update.
  10. **`test_status_tuples_unchanged`** — literal assertions that `_REISSUABLE_STATUSES == (UPLOADED, FAILED)`, `_CLAIMABLE_STATUSES == (UPLOADED, FAILED)`, and `STITCHABLE_BOOK_STATUSES == (EXTRACTED, STITCHING)`. Named after phase-5 OQ-6: a future "fix" for the `PARTIAL` dead end that takes the dangerous shortcut fails a unit test before it can wipe a book's text.
  11. **`test_stale_stitch_message_is_deferred_after_resynthesize`** — an integration-flavoured test: run `ResynthesizeBook`, then `StitchBook.execute` on the same book, and assert `DEFERRED("NOT_COMPLETE")` (§5.4).
- **`test_controllers.py`** Δ — `GET /books/{id}/audio` 200 shape / 409 when unset / 404 foreign / 401 anon; `GET /books/{id}/chunks/{n}/audio` same four; `GET /books/{id}/manifest` 200 passthrough + `Content-Type: application/json` + `Cache-Control` / 404 unset / 404 object-missing; `GET /books/{id}/chunks/{n}/marks` 200 / 404 unset / 404 foreign; `POST /books/{id}/resynthesize` 202 shape / 409 non-`PARTIAL` / 404 foreign / 401 anon.
- **`test_schemas.py`** Δ — `audio_url_to_dict` keys; `resynthesis_to_dict` nests `book` as `book_status_to_dict`'s exact output.
- **`test_dependencies.py`** Δ — `get_audio_delivery()` returns an `S3AudioDelivery` built from `settings.audio_bucket` and touches no network at construction.
- **`test_storage_domain.py`** (or wherever `storage.py`'s protocols are covered) Δ — `AUDIO_URL_EXPIRES_IN == 3600`, `PresignedDownload` shape.

### 13.3 Infra — `infra/tests/test_synth.py` (Δ)

- `test_api_stack_has_queue_urls` — `ApiFunction`'s environment carries `SYNTHESIZE_QUEUE_URL` and `STITCH_QUEUE_URL`.
- `test_api_stack_grants_send_to_both_queues` — assert the **count** of `sqs:SendMessage` statements on the API role is 3 (extract, synthesize, stitch), not merely their presence, so losing one grant fails.
- `test_api_stack_still_reads_audio_and_marks` — `s3:GetObject*` on both bucket ARNs (regression guard: this is what the presigned URL's validity depends on, and it is granted incidentally by `grant_read_write`).
- `test_api_stack_function_shape` — **`ReservedConcurrentExecutions: Match.absent()`** on `ApiFunction`, with the same "re-adding this fails a unit test instead of a six-minute deploy" comment the synthesize and stitch functions carry (constraint 3).
- `test_api_stack_synthesizes` / the parametrized `_synth_api_stack` helper gain the two new kwargs.
- **No `AWS::Lambda::Function` count change anywhere** — asserted, because Q17 ("no new Lambda") is a concurrency-budget promise and should be enforced rather than remembered.

### 13.4 Playwright — the strategy, given no audio in CI

The constraint, restated so the split is unambiguous:

> `get_speech_synthesizer()` gates on `settings.environment != "prod"` **first and unconditionally**. In `deploy-pr` every chunk reaches `FAILED`/`EXTERNAL_TTS_DISABLED`, the book lands `PARTIAL`/`NO_AUDIO`, and `audioKey` is `None`. `deploy-prod.yml` never runs login-gated checks at all. **Local compose is the only environment in existence where a Playwright test can hear anything**, and only because `SYNTHESIS_STUB_MODE=silent` puts `SilentSynthesizer` — real MPEG-2 frames, real interpolated word marks — on the `synthesize-worker`.

**Two projects** (`frontend/playwright.config.ts`):

```ts
projects: [
  { name: "chromium",
    use: { ...devices["Desktop Chrome"] },
    testIgnore: /playback\.spec\.ts/ },
  // channel: "chrome" -- Playwright's bundled Chromium omits proprietary
  // media codecs. Google Chrome ships them. This project is the ONLY place
  // audio is ever exercised, so it must not be the place a codec gap
  // silently turns a real assertion into a skip.
  ...(process.env.E2E_AUDIO === "1"
    ? [{ name: "playback",
         use: { ...devices["Desktop Chrome"], channel: "chrome" },
         testMatch: /playback\.spec\.ts/ }]
    : []),
]
```

**Three specs, and exactly where each runs:**

| spec | `e2e-local` (compose, `E2E_AUDIO=1`) | `deploy-pr` (`E2E_AUDIO` unset) | asserts |
|---|---|---|---|
| `e2e/upload-and-read.spec.ts` | ✅ | ✅ (OQ-2) | login → upload the fixture PDF → poll until `terminal` → the reading pane contains the fixture's marker text → the sidebar shows the book with a terminal status pill. **No audio assertion at all.** |
| `e2e/degraded.spec.ts` | ⛔ (skipped: the local book is `READY`) | ✅ (OQ-2) | on a `PARTIAL`/`NO_AUDIO` book: the full text is readable; **no `<audio>` element with a `src` is mounted**; the notice reads "Text-to-speech is turned off in this environment"; a "Try audio again" button exists, clicking it produces a `202`, the status returns to `EXTRACTED`, and the book cycles back to `PARTIAL`/`NO_AUDIO`. |
| `e2e/playback.spec.ts` | ✅ | ⛔ (project not created) | play → the highlighted word index advances within 3 s; pause; seek into segment 1 → the active paragraph is chunk 1 and the highlighted word matches the value computed from `manifest` + that chunk's marks; seek to 0 → back to word 0. |

**What the PR pipeline can actually assert about a UI whose audio is always absent** — stated plainly, because this is the question:

1. That the whole non-audio path works end to end against real AWS: Cognito login through the SSR Lambda's BFF handlers, CloudFront → Lambda Function URL, the presigned S3 upload from a real browser to real S3, the extract→synthesize→stitch pipeline, the poll loop, and the reading pane rendering text extracted by a real PyMuPDF Lambda.
2. That **the degraded path is correct**, which in a PR environment is not an edge case — it is the *only* terminal state. `degraded.spec.ts` is therefore the highest-value spec the PR pipeline runs, and it is testing the user requirement from constraint 2 directly.
3. That `/resynthesize` is wired: the button, the `202`, the status rewind, and the full round trip back to a terminal state.
4. Nothing whatsoever about decoding, seeking, or the highlight. Those are proven by `playback.spec.ts` against compose, plus the pure vitest suite in §13.1 which covers every branch of the search and rebasing logic against fixtures.

**CI wiring:**

- **`ci.yml`** gains a **new `e2e-local` job** (parallel to `local-smoke`, not merged into it, so a Playwright failure is distinguishable from a smoke failure): `make up` → `make ui` → `npx playwright install --with-deps chromium chrome` → `E2E_AUDIO=1 npx playwright test` → upload the trace artifact on failure → `make down`. Since `deploy-pr.yml`'s `ci` job calls this workflow, **every PR runs it**. This is the phase's real gate.
- **`deploy-pr.yml`** gains a step after the smoke test running the `chromium` project against `steps.frontend-url.outputs.url` with the seeded smoke credentials — see **OQ-2**.

**Fixture.** `frontend/e2e/fixtures/reader-pdf.ts` exports a base64 string decoded to a `Buffer` at test time and fed to `setInputFiles({name, mimeType, buffer})` — no binary in git, mirroring `local/smoke_test.py`'s existing embedded-PDF convention (including the regeneration snippet in a comment). It must produce **≥3 chunks**, i.e. ≥5,400 characters of body text (`DEFAULT_TARGET_CHARS = 1800`), because crossing a segment boundary is the assertion the two-level search exists for. The smoke test's 3-page fixture is too small and stays as it is.

**Named risk, with a pre-flight.** `SilentSynthesizer._silent_frame()` emits valid MPEG frame *headers* with a zero-filled payload, documented as "not a decodable granule". Chrome should render silence by resynchronizing on the next header, and CBR-with-no-Xing seeking is byte-exact — but nothing has ever loaded these bytes into a real decoder. **Step 0 of §14 is a ten-minute manual probe** (`make up && make ui`, upload, press play in Chrome, check `audio.duration` and that `currentTime` advances) *before any UI code is written*. If Chrome refuses, the fix is a ~15-line change to `_silent_frame` emitting a real all-zero-granule Layer III frame (side info + Huffman zeros), and it is a backend change made first, not a Playwright workaround made later.

---

## 14. Concrete file list

**`backend/src/contexts/library/`**
- new: `application/delivery.py`, `application/resynthesis.py`, `infrastructure/s3_client.py`, `infrastructure/s3_audio_delivery.py`
- changed: `domain/storage.py`, `infrastructure/s3_pdf_storage.py`, `interface/dependencies.py`, `interface/schemas.py`, `interface/controllers.py`

**`backend/tests/contexts/library/`**: `test_delivery_use_cases.py`, `test_s3_audio_delivery.py`, `test_resynthesis_use_case.py` (new); `test_controllers.py`, `test_schemas.py`, `test_dependencies.py`, `fakes.py` (a `FakeAudioDelivery`) (changed)

**`backend/`**: nothing else. **`pyproject.toml`/`uv.lock` untouched — this phase adds no backend dependency.**

**`frontend/`**
- new pages: `app/(app)/layout.tsx`, `app/(app)/page.tsx`, `app/(app)/books/[bookId]/page.tsx`
- new components: `components/BookSidebar.tsx`, `components/BookListItem.tsx`, `components/UploadButton.tsx`, `components/ReaderView.tsx`, `components/ReadingPane.tsx`, `components/ChunkParagraph.tsx`, `components/PlayerBar.tsx`, `components/AudioNotice.tsx`, `components/HealthBadge.tsx`, `components/BooksProvider.tsx`
- new lib: `lib/books.ts`, `lib/manifest.ts`, `lib/marks.ts`, `lib/upload.ts`
- new hooks: `hooks/useBookList.ts`, `hooks/useBookStatus.ts`, `hooks/usePlayback.ts`
- new e2e: `e2e/upload-and-read.spec.ts`, `e2e/degraded.spec.ts`, `e2e/playback.spec.ts`, `e2e/fixtures/reader-pdf.ts`, `e2e/support/session.ts` (a shared login helper)
- changed: `app/page.tsx` (**deleted** — replaced by `app/(app)/page.tsx`), `playwright.config.ts`, `package.json`
- new tests: `__tests__/manifest.test.ts`, `marks.test.ts`, `upload.test.ts`, `books-api.test.ts`, `use-book-status.test.tsx`, `use-playback.test.tsx`, `reading-pane.test.tsx`, `audio-notice.test.tsx`, `book-sidebar.test.tsx`, `player-bar.test.tsx`, `upload-button.test.tsx`
- moved test: `__tests__/page.test.tsx` → `__tests__/health-badge.test.tsx`
- **no new runtime dependency** (`next`/`react`/`react-dom` unchanged); `@playwright/test` is already a devDependency.

**`infra/`**: `stacks/api_stack.py`, `app.py`, `tests/test_synth.py` (changed). **`stacks/storage_stack.py`, `stacks/frontend_stack.py`, `stacks/pipeline_stack.py`, `stacks/config.py` untouched** — stated explicitly.

**`local/`**: `smoke_test.py` (changed). `setup.sh` untouched.

**Root**: `Makefile`, `.github/workflows/ci.yml`, `.github/workflows/deploy-pr.yml` (OQ-2), `docker-compose.yml` (one comment), `README.md`, `IMPLEMENTATION_PLAN.md` (changed).

---

## 15. Implementation sequence

0. **Pre-flight, before any code (§13.4's named risk):** `make up && make ui`, upload a PDF by hand, open the browser console and load one of LocalStack's `audio/…/book.mp3` objects into an `<audio>` element. Confirm `duration` is finite and `currentTime` advances. **If it doesn't, fix `SilentSynthesizer._silent_frame` first** — everything downstream in the local e2e story depends on it.
1. **Backend, pure/adapters first:** `infrastructure/s3_client.py` (promote `_build_client`), `domain/storage.py`'s `AudioDelivery`/`PresignedDownload`, `infrastructure/s3_audio_delivery.py` + `test_s3_audio_delivery.py`. **Gate:** the public-endpoint test and the no-region construction test are green.
2. `application/delivery.py` + `test_delivery_use_cases.py`; then the four GET routes in `controllers.py` + `test_controllers.py`. Full backend suite ≥90%.
3. `application/resynthesis.py` + `test_resynthesis_use_case.py`, including `test_status_tuples_unchanged` and the stale-stitch deferral test; then `POST /books/{id}/resynthesize`.
4. `infra/stacks/api_stack.py` + `infra/app.py` + `infra/tests/test_synth.py`. `make synth` locally.
5. `local/smoke_test.py`'s two new checks. **`make smoke` is the gate** — this is where the Range assertion (§12.2 step 6) first runs.
6. **Frontend pure modules first, zero React:** `lib/manifest.ts`, `lib/marks.ts`, `lib/upload.ts` + their three vitest files. **Gate:** the `stillInside`/`locateWord` consistency sweep is green before anything renders a `<mark>`.
7. `lib/books.ts` + `books-api.test.ts` (including the 404→`null` marks contract).
8. `hooks/useBookStatus.ts` + tests; `components/BookSidebar.tsx`, `BookListItem.tsx`, `BooksProvider.tsx`, `HealthBadge.tsx`; the `app/(app)/` route group. **Checkpoint:** the sidebar lists books and polls, with no reader yet.
9. `components/UploadButton.tsx` + tests. **Checkpoint:** upload works end to end locally; the book appears and reaches a terminal status without touching the AWS console.
10. `components/ReadingPane.tsx`, `ChunkParagraph.tsx`, `AudioNotice.tsx` + tests, plus `ReaderView.tsx` wired to text only. **Checkpoint:** every row of §9's table renders correctly — including the `PARTIAL`/`NO_AUDIO` one, which is what the PR pipeline will see.
11. `hooks/usePlayback.ts` + `PlayerBar.tsx` + tests. **Gate:** the render-count test (≤3 renders over 60 frames) and the marks-404 test (zero repeat requests over 120 frames) are green — those two are the difference between a smooth reader and a browser tab at 100% CPU hammering the API.
12. `playwright.config.ts`'s two projects, `e2e/support/session.ts`, the fixture, then the three specs. `make e2e-local`.
13. `ci.yml`'s `e2e-local` job; `deploy-pr.yml`'s `chromium` step (OQ-2).
14. `README.md` (the new routes, the audio-delivery decision, the manifest→highlight contract, the degradation table) and `IMPLEMENTATION_PLAN.md`'s checklist.
15. Push; `deploy-pr` runs the whole thing against real AWS with a real browser.

---

## 16. Open Questions

> **All eight are DECIDED (2026-08-11); every recommendation was accepted as written.**
>
> ### Added to this phase's scope after the plan was written: fix the recurring PR-teardown failure
>
> Two of the last three `destroy-pr` runs failed identically — `HttpApiCognitoAuthorizer` returning
> `HandlerErrorCode: InternalFailure` on delete — stranding four `pr-N` stacks each time and needing
> manual cleanup with elevated credentials.
>
> What the pr-6 post-mortem established: CloudFormation ordered the deletion correctly (all 25 routes
> reached `DELETE_COMPLETE` before the authorizer was touched), and afterwards the API had **zero
> routes and zero integrations** yet the authorizer still existed — so it is not an ordering bug and
> not a still-in-use reference. `InternalFailure` carries no service-side reason, so the true cause is
> not observable from outside the account. It is transient: the identical delete succeeded on retry for
> both pr-4 and pr-6.
>
> Two changes, approved by the user:
>
> 1. **`infra/stacks/api_stack.py`: collapse the enumerated per-method routes to a single `ANY` route
>    per path.** Today 5 paths x 5 methods = 25 routes; `ANY` makes it 5. This reduces how hard the
>    teardown provokes the failure. Verify the authorizer/no-authorizer split still holds per route —
>    the four public paths must keep `HttpNoneAuthorizer` and `/{proxy+}` must keep the Cognito
>    authorizer. Update `infra/tests/test_synth.py`'s route assertions accordingly.
> 2. **`.github/workflows/destroy-pr.yml`: a bounded retry on `DELETE_FAILED`.** Re-issue
>    `delete-stack` up to 3 times with a **meaningful** back-off (~60s, 180s, 300s), and **only** for a
>    stack in `DELETE_FAILED`. The back-off length is deliberate: pr-6's stack deleted cleanly on a plain
>    workflow rerun, making the transient diagnosis 2-for-2 (pr-4 and pr-6, no code change either time) --
>    but **both observed recoveries came hours later**, so a token few-second retry would exhaust its
>    attempts and fail anyway, producing a fix that looks effective and is not. Retrying within minutes
>    may still not be enough; the retry is best-effort, and the loud failure plus an easy `gh run rerun`
>    is the actual safety net.
>    Any other terminal state, and exhausting the retries, must still fail the job loudly — the phase-1
>    fix that made teardown fail rather than skip is what surfaced these leaks at all, and it must not
>    be weakened. A retry that silently swallowed a real error would recreate the original leak bug.


Each states a recommendation and the trade-off, so one word approves it.

**OQ-1 (DECIDED: endpoint-only, defer the UI) — Ship a per-chunk playback UI for the `STITCH_FAILED` case, or endpoint-only?**
Phase-5 §7.2's invariant 3 says a book whose concatenation failed is "still fully playable and fully highlightable from per-chunk artefacts", and §4.3 of this plan ships `GET /books/{id}/chunks/{n}/audio` to make that true at the API level. Making it true in the *UI* means a second playback mode: N `<audio>` elements (or one with a swapped `src`) stitched into a virtual timeline, with boundary handling, prefetch and its own seek semantics. `STITCH_FAILED` requires concatenation to fail on three consecutive SQS attempts and has never been observed.
**Trade-off:** endpoint-only means one documented state (`PARTIAL`/`STITCH_FAILED`) shows "Audio couldn't be assembled" + a retry button rather than degraded playback; shipping the UI adds a whole second playback engine to test for a state nothing has ever produced.
**Recommendation: endpoint-only.** Ship the route (tested), show the retry affordance, and revisit if prod ever produces a `STITCH_FAILED`.

**OQ-2 (DECIDED: ship now) — Run the `chromium` Playwright project in `deploy-pr.yml` now, or leave all pipeline wiring to phase 8?**
`IMPLEMENTATION_PLAN.md` phase 8 says its job is *"wire the Playwright e2e suite (from phases 6-7) into both the per-PR ephemeral pipeline and the main-branch CD pipeline"*. Strictly, that's phase 8's line item. But the `e2e-local` job cannot exercise CloudFront, the SSR Lambda, API Gateway's Cognito authorizer, or real S3 — and `degraded.spec.ts` is only *meaningful* against an environment whose books are genuinely `PARTIAL`/`NO_AUDIO`, which compose (with `SYNTHESIS_STUB_MODE=silent`) is not.
**Trade-off:** ~15 lines and ~2 minutes on a pipeline that already deploys five stacks, versus leaving the deployed UI untested by any browser for a whole phase. Phase 8 then hardens and extends rather than starting from zero.
**Recommendation: ship it now.** Add the step; phase 8 keeps its job of adding the *prod* gate and the rollback story.

**OQ-3 (DECIDED: 1 hour + reactive refresh) — Presigned URL expiry: 1 hour + reactive refresh, or a longer window?**
1 hour with an on-`error` refresh (§3.3) versus, say, 6 or 12 hours. A longer window means fewer refreshes but a longer-lived bearer capability sitting in browser history and CloudFront/CloudWatch logs — and it is not even reliably longer, since a URL signed with the Lambda role's temporary credentials dies when those expire regardless of `ExpiresIn`.
**Trade-off:** the refresh path is ~20 lines and must exist either way (a mid-book credential rotation would break a 12-hour URL too); a longer expiry only reduces how often that path runs.
**Recommendation: 1 hour.** The refresh is the real mechanism; the expiry is just how often it fires.

**OQ-4 (DECIDED: accept the ceiling, warn loudly) — `GET /books/{id}/chunks` returns full text for every chunk in one response. Paginate now?**
A 330-chunk book is ~600 KB of JSON; the ceiling is Lambda's 6 MB response limit at roughly **3,300 chunks ≈ 2,700 pages**. Paginating means a cursor on the endpoint, a windowed reading pane, and a virtualized scroll container.
**Trade-off:** paginating now is real work (and virtualization interacts badly with the "slice the active paragraph" rendering in §6.4) for a limit no personal library will hit; not paginating means the reader hard-fails, opaquely, on a book that large.
**Recommendation: accept the ceiling, and make it loud** — the reader logs a console warning above 2,000 chunks, and this line goes in `README.md`. Revisit if a real book gets close.

**OQ-5 (DECIDED: no, `PARTIAL` only) — Should `/resynthesize` also accept `READY` (force a full re-synthesis)?**
Useful for changing the voice (`EDGE_TTS_VOICE`) on an already-synthesized book. Would mean resetting **every** chunk, not just failed ones, and would make a mis-click destroy hours of good audio.
**Trade-off:** a genuinely useful capability once voices are configurable, versus a destructive operation with no confirmation model and no UI concept of "this will take 20 minutes".
**Recommendation: `PARTIAL` only in this phase.** If voice switching becomes a feature, it deserves its own endpoint (`POST /books/{id}/resynthesize?force=true` with an explicit confirmation), not an overload of the repair path.

**OQ-6 (DECIDED: ship it) — Ship playback-rate control (0.75×-2×)?**
`audio.playbackRate` is one property. The highlight math is entirely unaffected: everything is derived from `audio.currentTime`, which is already media time, not wall-clock time.
**Trade-off:** ~10 lines of UI and one more control in the player bar, versus a smaller surface. For a reading app, rate control is close to table stakes.
**Recommendation: ship it.** Add `playbackRate` to `PlayerBar` with a persisted preference in `localStorage`; add one vitest case asserting the highlight is unaffected at 2×.

**OQ-7 (DECIDED: yes, real Chrome, hard-fail on a codec gap) — `channel: "chrome"` for the `playback` project, or bundled Chromium?**
Playwright's bundled Chromium omits proprietary media codecs, and MP3 sits in the grey zone across versions. `npx playwright install chrome` on `ubuntu-latest` adds ~30 s to the `e2e-local` job and guarantees full codec support.
**Trade-off:** 30 s of CI, and Google Chrome is a slightly different browser from the one `chromium` tests use — versus the possibility that the *one* test in the entire repo that exercises audio silently fails on a codec gap rather than a real regression.
**Recommendation: `channel: "chrome"`**, and make the project **hard-fail** (not skip) if `canPlayType("audio/mpeg")` is empty — a silent skip here would be worse than no test.

**OQ-8 (DECIDED: subtle visual difference + tooltip) — Should `timing: "estimated"` marks be highlighted differently from `"measured"` ones?**
`build_marks_document` records `timing` (`MarksTiming.MEASURED` for edge-tts's real `WordBoundary` events, `ESTIMATED` for Google's per-sentence interpolation and for `SilentSynthesizer`). Estimated marks are linear interpolation across a sentence, so within a long sentence the highlight will visibly drift from the actual speech — a real artefact, not a bug, that would look like a bug.
**Trade-off:** rendering estimated marks with a softer/wider highlight (or falling back to sentence-level highlighting when `timing === "estimated"`) is honest but adds a second highlight mode to build and test; ignoring `timing` ships one mode that is occasionally, inexplicably off.
**Recommendation: one mode, plus a visual tell.** Highlight at word level regardless, but render `estimated` segments with a lower-opacity `<mark>` and a tooltip ("approximate timing"). One CSS class and one prop, no second search path. Revisit if prod's Google fallback ever fires often enough to matter — which is also what phase 8's fallback alarm (phase-4 OQ-E) is for.

---

### Critical Files for Implementation
- backend/src/contexts/library/interface/controllers.py
- backend/src/contexts/library/interface/schemas.py
- backend/src/contexts/library/domain/storage.py
- backend/src/contexts/library/application/use_cases.py
- backend/src/contexts/library/infrastructure/s3_pdf_storage.py
- frontend/lib/api.ts
- frontend/lib/auth.ts
- frontend/playwright.config.ts
- infra/stacks/api_stack.py
- local/smoke_test.py
