# Phase 3 — Upload & extraction pipeline

Goal: a book is created together with a presigned browser→S3 upload; the PDF landing in `pdf_bucket` fires an S3 event → SQS → a new **extract Lambda** that runs PyMuPDF text extraction, filters running headers/footers/footnotes, chunks the text, writes `CHUNK#n` items with status `PENDING`, and flips the book to `EXTRACTED` (or `FAILED` with a reason).

**Verified against the current repo:**

- `infra/stacks/pipeline_stack.py` already builds `extract_queue` + `synthesize_queue`, each with a DLQ (`max_receive_count=3`, 5 min visibility). **No consumers, no S3 notification, no Lambda.** Its docstring already says "the extract Lambda (phase 3) … attach later without a queue-shape change" — this phase is that.
- `infra/stacks/api_stack.py` already sets `PDF_BUCKET`/`EXTRACT_QUEUE_URL` on the API Lambda and already does `pdf_bucket.grant_read_write(fn)` + `extract_queue.grant_send_messages(fn)`. **The presign endpoint needs no new IAM grant.**
- `infra/stacks/storage_stack.py`'s `PdfBucket` already carries a CORS rule allowing `PUT/POST/GET` from `*` "so phase 3's presigned browser upload works without a stack change". **True — no CORS change needed.**
- `Book.status` is `BookStatus.UPLOADED` at creation (`domain/book.py`); the enum already declares `EXTRACTED`/`FAILED` (phase-2 §10 Q1 deliberately pre-declared them). `Book` has **no** `source_key`; `book_mapper.py` uses `item.get(...)` everywhere, and phase-2 §1.1 explicitly names `sourceKey` as the intended additive change.
- `application/use_cases.py`'s `CreateBook` docstring says "phase 3's presigned-POST controller calls this" — §4.3 below explains why it is *superseded* rather than called.
- `local/setup.sh` creates the buckets **and** both queues but wires no notification between them.
- `local/smoke_test.py` is stdlib-only by design and already logs in via `_cognito(...)`; `deploy-prod.yml` passes **no** `--login-*` args, so any login-gated check is automatically prod-skipped.

Reference patterns copied deliberately: the existing `keys.py`/mapper/adapter split, `Protocol` ports satisfied structurally, per-request `Depends` composition root, `moto`'s `mock_aws()` in fixtures, and `local/smoke_test.py` as the only real-infra proof.

---

## 1. End-to-end flow

```
browser                     API Lambda              S3 pdf_bucket      SQS extract_queue     extract Lambda        DynamoDB
  |  POST /books {title}  ------>|
  |                              | Book.create() -> status=UPLOADED, sourceKey= books/<sub>/<bookId>/source.pdf
  |                              | put_item -------------------------------------------------------------------->|
  |                              | generate_presigned_post(key=sourceKey, 15 min, <=50 MiB, application/pdf)
  |  <-- 201 {book, upload{url,fields,key}}
  |  POST multipart (fields..., file last) ---------->|
  |  <-- 204                                          | s3:ObjectCreated:* (prefix books/, suffix .pdf)
  |                                                   |-------------------------------->|
  |                                                                                    |--- batch_size=1 ------->|
  |                                                                                                              | claim: UPLOADED|FAILED -> EXTRACTING (conditional)
  |                                                                                                              | get_object -> PyMuPDF -> layout filter -> chunker
  |                                                                                                              | delete_for_book + save_all(CHUNK#n, PENDING) ---->|
  |                                                                                                              | status=EXTRACTED, chunksTotal=N, pageCount=P ---->|
  |  GET /books/{id}  (poll)  --->| status: UPLOADED -> EXTRACTING -> EXTRACTED | FAILED(+failureReason)
  |  GET /books/{id}/chunks   --->| the chunk list
```

Chunks are written **before** the book flips to `EXTRACTED`, so any consumer that sees `EXTRACTED` is guaranteed to find all `chunksTotal` chunks. This ordering is load-bearing for phase 4's fan-out and has a dedicated test.

---

## 2. Decisions up front (details in the referenced sections)

| # | Question | Decision | § |
|---|---|---|---|
| Q1 | One call or two for create+presign? | **One.** `POST /books` creates the book **and** returns the presigned POST. Plus a separate `POST /books/{id}/upload-url` to *re-issue* for retry/re-upload. | §4 |
| Q2 | S3 → SQS wiring | **Direct S3 event notification → the existing `extract_queue`**, configured **inside `PipelineStack`** against a bucket imported by name. Not EventBridge, not a new queue. | §6.1 |
| Q3 | Extract Lambda packaging | **Second `DockerImageFunction` over the same `backend/` image asset**, differing only by `cmd`. Same ECR image, one build. Lives in `PipelineStack`. | §6.2 |
| Q4 | New bounded context? | **No.** Extraction stays in `contexts/library/` — it produces the Library's own aggregates. | §3 |
| Q5 | Status lifecycle | Add **`EXTRACTING`** to `BookStatus` (atomic claim + a real in-progress state phase 5/6 need). Reuse `EXTRACTED`/`FAILED`. Add `failureReason` attribute. | §5.2, §8 |
| Q6 | Chunking target | **Paragraph-greedy, target 1 800 chars, hard max 2 600, min 400**, sentence-split then whitespace-split as fallbacks. Chunks **exactly partition** the extracted text. | §7.4 |
| Q7 | Headers/footers | **Repetition + geometry**: normalized lines in the top/bottom 8% band appearing on ≥ 50% of pages are dropped; band-only page numbers always dropped. | §7.3 |
| Q8 | Footnotes | Dropped from the narration stream (small-font blocks in the bottom 25%), plus inline superscript-marker removal. Counted and logged, not stored. | §7.3 |
| Q9 | charStart/charEnd ↔ pages | Offsets are into the full extracted text; the extractor returns per-page char ranges, and each chunk stores **`pageStart`/`pageEnd`** (1-based). | §7.5 |
| Q10 | Extraction failure handling | Permanent failures (corrupt/encrypted/no-text-layer/empty/too-large) → book `FAILED` + `failureReason`, message **deleted** (handler returns normally). Only transient errors raise → SQS retry → existing DLQ. | §8 |
| Q11 | Test fixtures | **Generated at test time with PyMuPDF itself** (already a runtime dep) — no binary PDFs committed. Corrupt PDF is a bytes literal. | §10.1 |
| Q12 | Local dev | S3→SQS notification in `local/setup.sh` + a **`extract-worker` compose service** running the same handler in a poll loop (LocalStack community can't run our image-based Lambda). | §9 |
| Q13 | Smoke test | New login-gated `check_upload_and_extraction` (upload → poll → assert chunks). Auto-skipped in prod (deploy-prod.yml passes no `--login-*`). | §9.3 |
| Q14 | Phase-2's `CreateBook` | **Superseded and deleted**, replaced by `RequestBookUpload` (the key must be known before `save`). | §4.3 |
| Q15 | Frontend | **Out of scope** — phase 6 owns the reader UI. The presign contract is documented for it. | §12 |

---

## 3. DDD placement — stays inside `contexts/library/`

Extraction *produces the Library's own aggregates* (`Book` status/counters, `Chunk` items). A separate `ingestion` context that writes another context's aggregates would be a bigger smell than a slightly larger context. Rejected alternative noted in Open Questions (OQ-1).

New/changed layout (⊕ = new, Δ = changed):

```
backend/src/contexts/library/
├── domain/
│   ├── book.py                     Δ  + source_key, failure_reason; Book.create(source_key=...)
│   ├── chunk.py                    Δ  + page_start, page_end
│   ├── value_objects.py            Δ  + BookStatus.EXTRACTING, ExtractionFailure StrEnum
│   ├── repository.py               Δ  BookRepository.update_status gains kwargs (§5.3)
│   ├── storage.py                  ⊕  PresignedUpload, PdfStorage (Protocol)
│   ├── extraction.py               ⊕  ExtractedPage/ExtractedDocument, ExtractionError,
│   │                                  PdfTextExtractor (Protocol), page_range_for()
│   ├── layout.py                   ⊕  PURE header/footer/footnote policy over TextLine data
│   └── chunking.py                 ⊕  PURE text -> list[ChunkBoundary]
├── application/
│   ├── commands.py                 Δ  RequestBookUploadCommand, ExtractBookCommand
│   ├── use_cases.py                Δ  - CreateBook, + RequestBookUpload, + ReissueBookUpload
│   └── extraction.py               ⊕  ExtractBook use case + ExtractBookResult
├── infrastructure/
│   ├── book_mapper.py              Δ  sourceKey, failureReason
│   ├── chunk_mapper.py             Δ  pageStart, pageEnd
│   ├── dynamodb_book_repository.py Δ  update_status rewrite (§5.3)
│   ├── s3_keys.py                  ⊕  source_pdf_key(), parse_source_pdf_key(), SOURCE_PREFIX
│   ├── s3_pdf_storage.py           ⊕  S3PdfStorage (presigned POST + get_bytes)
│   └── pymupdf_extractor.py        ⊕  PyMuPdfTextExtractor — the only module importing pymupdf
└── interface/
    ├── controllers.py              Δ  + POST /books, POST /books/{id}/upload-url,
    │                                    GET /books/{id}/chunks
    ├── schemas.py                  Δ  + failureReason on book, pageStart/pageEnd on chunk,
    │                                    upload_to_dict()
    ├── dependencies.py             Δ  + get_pdf_storage(), get_pdf_extractor()
    ├── extract_handler.py          ⊕  SQS Lambda handler (the pipeline's "controller")
    └── local_extract_worker.py     ⊕  local-dev SQS poll loop over the same handler
```

`domain/layout.py` and `domain/chunking.py` are **pure** — no `pymupdf` import, no boto3 — precisely so the "chunking edge cases (headers/footnotes)" tests the plan requires can be exhaustive, fast, and readable, with `pymupdf_extractor.py` covered separately against generated PDFs.

---

## 4. HTTP surface

### 4.1 `POST /books` — create + presign, one call

Rationale for one call rather than a separate "request upload URL" step: phase-2 §5 already fixed the contract as `{book, uploadUrl, fields}` and rejected a bare `POST /books` **specifically because** "a bare `POST /books` creates a `Book` row with no PDF behind it". A two-step flow reintroduces exactly that window. One call also means the `sourceKey` is known before the first `put_item`, so the book row is never half-formed.

Request body (permissive pydantic model; the *domain* validates so the error is `400 "Title is required"` from `Book.create`, not FastAPI's 422):

```python
class CreateBookRequest(BaseModel):
    title: str = ""
```

Response `201`:

```json
{
  "book": { "id": "...", "title": "...", "status": "UPLOADED", "chunksTotal": 0,
            "chunksDone": 0, "pageCount": 0, "failureReason": null,
            "createdAt": "...", "updatedAt": "..." },
  "upload": {
    "url": "https://<bucket>.s3.amazonaws.com/",
    "fields": { "key": "books/<sub>/<bookId>/source.pdf", "Content-Type": "application/pdf",
                "policy": "...", "x-amz-algorithm": "...", "x-amz-credential": "...",
                "x-amz-date": "...", "x-amz-signature": "..." },
    "key": "books/<sub>/<bookId>/source.pdf",
    "expiresIn": 900,
    "maxBytes": 52428800
  }
}
```

Client contract (document in the docstring and README, phase 6 depends on it): build a `FormData`, append **every** entry of `fields` first, append `file` **last** (S3 ignores anything after the file field), `POST` to `url`, expect `204`.

`sourceKey` is deliberately **not** in `book_to_dict` — it's an internal storage detail; `upload.key` already tells the client what it needs for this one request.

### 4.2 `POST /books/{book_id}/upload-url` — re-issue

Returns just the `upload` object for an existing book. Allowed only when `book.status in {UPLOADED, FAILED}` → otherwise `409 ConflictError` ("Book is already being processed"). 404 for another user's book (the `_load_owned_book` path). ~15 lines, and it's what prevents a failed upload from orphaning a book row and forcing the user to create a duplicate.

### 4.3 `GET /books/{book_id}/chunks`

`ListBookChunks` has existed since phase 2 with no route. Adding it now is what lets the smoke test *prove* extraction end to end against real infra (see §9.3) rather than only asserting a status string. Returns `[chunk_to_dict(c) for c in chunks]`, `404` for a non-owned/missing book.

### 4.4 `CreateBook` is superseded

`RequestBookUpload` must generate the book id **before** `Book.create` so it can compute `source_pdf_key(user_id, book_id)` and store `sourceKey` in the same `put_item`. `CreateBook.execute` generates the id internally and saves, so it cannot be composed. Delete `CreateBook` + `CreateBookCommand` and their tests; do **not** leave dead code behind.

```python
@dataclass(frozen=True)
class RequestBookUploadCommand:
    user_id: str
    title: str

@dataclass(frozen=True)
class BookUpload:
    book: Book
    upload: PresignedUpload

class RequestBookUpload:
    def __init__(self, book_repository: BookRepository, pdf_storage: PdfStorage,
                 clock: Clock, id_generator: IdGenerator) -> None: ...
    def execute(self, command: RequestBookUploadCommand) -> BookUpload:
        book_id = self._id_generator.new_id()
        key = source_pdf_key(command.user_id, book_id)
        book = Book.create(id=book_id, user_id=command.user_id, title_raw=command.title,
                           now=self._clock.now(), source_key=key)
        self._book_repository.save(book)
        return BookUpload(book=book, upload=self._pdf_storage.presigned_upload(key=key))
```

All routes remain under the existing `/{proxy+}` JWT authorizer with `POST` already in `route_methods` — **no `api_stack.py` route change.**

---

## 5. Data model changes

### 5.1 S3 key layout — `infrastructure/s3_keys.py`

```python
SOURCE_PREFIX = "books/"          # must stay in lockstep with infra/stacks/config.py's
SOURCE_FILENAME = "source.pdf"    # Config.SOURCE_PDF_PREFIX and local/setup.sh
_KEY_RE = re.compile(r"^books/(?P<user_id>[^/]+)/(?P<book_id>[^/]+)/source\.pdf$")

def source_pdf_key(user_id: str, book_id: str) -> str
def parse_source_pdf_key(key: str) -> tuple[str, str] | None   # (user_id, book_id) or None
```

Why the key carries both ids: an S3 event gives only bucket + key, and this avoids a `HeadObject` round trip for metadata. Why it's safe: the presigned POST pins the **exact** key (no `${filename}`, no `starts-with` condition), so a browser physically cannot write into another user's prefix. The extract Lambda still re-validates by loading the book and comparing `book.source_key` to the event key (§8.2).

### 5.2 Domain changes

`Book` gains:

| field | type | notes |
|---|---|---|
| `source_key` | `str \| None` | S3 key in `pdf_bucket`; set at creation |
| `failure_reason` | `str \| None` | `ExtractionFailure` value; `None` unless `status == FAILED` |

`Book.create(..., source_key: str | None = None)` — `failure_reason` always starts `None`.

`Chunk` gains `page_start: int`, `page_end: int` (1-based, inclusive), defaulting to `0` in `Chunk.create` so phase-2 fixtures/tests keep compiling. `Chunk.create` validates `page_end >= page_start`.

`BookStatus` gains **`EXTRACTING`** with the inline comment convention already used there:

```python
EXTRACTING = "EXTRACTING"   # first SET in phase 3 (extract Lambda claim)
```

Deploy-order risk, stated explicitly so it is a known accepted risk and not a surprise: CDK orders `Storage → Pipeline → Api`, so the **writer** (extract Lambda, in PipelineStack) deploys *before* the **reader** (API Lambda). A warm old API container reading a book that a new extract Lambda just marked `EXTRACTING` would get `ValidationError` from `BookStatus.parse` → `400` on that one `GET /books`. Window is seconds-to-minutes on a single-user app with no traffic during deploy; accepted. (Alternative in OQ-5.)

New `ExtractionFailure(StrEnum)` in `value_objects.py`:

```python
CORRUPT_PDF | ENCRYPTED_PDF | EMPTY_PDF | NO_TEXT_LAYER | TOO_LARGE | UNKNOWN
```

### 5.3 `BookRepository.update_status` — one generalized targeted update

Phase 2's port comment is explicit that `save()` is a whole-item `put_item` and **every status/counter mutation must go through a targeted update**. Extraction needs to set status + `chunksTotal` + `pageCount` + `updatedAt` atomically, and to *clear* `failureReason` on retry. Rather than adding three new methods (a second write path onto the table — exactly what phase-2 §10 Q2 forbade), generalize the existing one, mirroring `ChunkRepository.update_status`'s existing optional-kwargs style:

```python
def update_status(
    self,
    user_id: str,
    book_id: str,
    status: BookStatus,
    *,
    expected_statuses: Sequence[BookStatus] | None = None,
    chunks_total: int | None = None,
    page_count: int | None = None,
    failure_reason: str | None = None,
    clear_failure_reason: bool = False,
    updated_at: str | None = None,
) -> None: ...
```

Implementation notes for the adapter (all of these are trap-shaped):

- Builds `SET #status = :status[, chunksTotal = :ct][, pageCount = :pc][, failureReason = :fr][, updatedAt = :ua]`, optionally suffixed with ` REMOVE failureReason` (a single `UpdateExpression` may combine `SET` and `REMOVE`).
- `failure_reason is not None and clear_failure_reason` → raise `ValueError` (programming error, not a domain error).
- `ConditionExpression` is `attribute_exists(PK)`, **AND** `#status IN (:exp0, :exp1)` when `expected_statuses` is given. `status` stays aliased as `#status` (reserved word) — the existing comment stays.
- **On `ConditionalCheckFailedException`, disambiguate**: re-`get_item`; item absent → `NotFoundError` (preserving phase-2 behaviour and its tests), item present → `ConflictError` (409). This is what makes the atomic claim usable — without it the caller can't tell "gone" from "already claimed".
- Every existing phase-2 call site (`update_status(u, b, EXTRACTED)`) keeps working unchanged.

### 5.4 Item shapes (additive only — mappers already use `.get`)

Book item gains `sourceKey` (str), `failureReason` (str, **absent** rather than null when unset — that's why `REMOVE` is used). Chunk item gains `pageStart`, `pageEnd` (N).

---

## 6. Infrastructure (CDK)

### 6.1 S3 → SQS notification, and the circular-dependency trap

**The trap:** `pdf_bucket` lives in `StorageStack`, `extract_queue` in `PipelineStack`. Calling `storage.pdf_bucket.add_event_notification(..., SqsDestination(pipeline.extract_queue))` puts the `NotificationConfiguration` in the **bucket's** stack (needs the queue ARN → Storage→Pipeline) and the queue resource policy with an `aws:SourceArn` bucket condition in the **queue's** stack (needs the bucket ARN → Pipeline→Storage). That is a hard CloudFormation cycle and `cdk deploy` will refuse.

**The fix** (standard CDK workaround): re-import the bucket *by name* inside `PipelineStack`, so both the notification custom resource and the queue policy are created in `PipelineStack`, leaving exactly one dependency direction, `Pipeline → Storage`.

`infra/stacks/pipeline_stack.py` constructor gains `pdf_bucket_name: str`, `table: dynamodb.Table`, `git_sha: str`:

```python
pdf_bucket = s3.Bucket.from_bucket_name(self, "PdfBucketRef", pdf_bucket_name)
pdf_bucket.add_event_notification(
    s3.EventType.OBJECT_CREATED,
    s3n.SqsDestination(self.extract_queue),
    s3.NotificationKeyFilter(prefix=Config.SOURCE_PDF_PREFIX, suffix=".pdf"),
)
```

Notes for the implementer:
- For an **imported** bucket CDK emits `Custom::S3BucketNotifications` with `Managed: false`, which *merges with* rather than replaces existing notification config, and auto-creates a `BucketNotificationsHandler` singleton Lambda (inline Python, **no Docker**) plus the `s3:PutBucketNotification` grant. Same account → no bucket-policy work needed.
- The prefix/suffix filter is why `SOURCE_PREFIX` must be duplicated in `infra/stacks/config.py` (add `SOURCE_PDF_PREFIX = "books/"` with the standard "separately deployed projects, cannot share an import" lockstep comment, next to the `ENV_*` names).
- Teardown order in `destroy-pr.yml` is already `… Pipeline → … → Storage`, which is correct for the new dependency. No workflow change.
- `deploy-pr.yml` deploys `Storage Auth Pipeline Api --exclusively`; CDK resolves the new `Pipeline → Storage` edge itself. No workflow change.

EventBridge (`event_bridge_enabled=True` + an `events.Rule`) was considered as a cycle-free alternative; rejected because it adds a service, needs `events` added to LocalStack's `SERVICES`, and S3→EventBridge is less battle-tested in LocalStack community than S3→SQS, which is the thing the `local-smoke` CI job has to exercise. Recorded in OQ-2.

### 6.2 The extract Lambda

```python
extract_fn = lambda_.DockerImageFunction(
    self, "ExtractFunction",
    code=lambda_.DockerImageCode.from_image_asset(
        backend_dir,
        cmd=["src.contexts.library.interface.extract_handler.handler"],
    ),
    memory_size=1536,
    timeout=cdk.Duration.seconds(120),
    environment={ ENV_ENVIRONMENT, ENV_GIT_SHA, ENV_TABLE_NAME, ENV_PDF_BUCKET, ENV_LOG_LEVEL },
)
extract_fn.add_event_source(lambda_event_sources.SqsEventSource(self.extract_queue, batch_size=1))
```

**Same image, second function.** `DockerImageAsset`'s hash is computed from the source directory + build args + target + platform — **not** from `cmd`, which becomes the function's `ImageConfig.Command`. So `ApiStack` and `PipelineStack` referencing the same `backend/` directory produce one ECR image, published once, with two Lambda functions pointing at it and different commands. This is the same reasoning that already justified `DockerImageFunction` for the API Lambda (`api_stack.py`'s comment: "phases 3-4 pull in PyMuPDF and edge-tts, which blow past the Lambda zip layer size limits"). A separate zip-based function would mean a second dependency set, a second install path, and a PyMuPDF wheel (~20 MB, native MuPDF binary) fighting the zip limits — exactly the migration Phase 0 pre-empted.

Sizing: 1536 MB ≈ 1 vCPU (PyMuPDF is CPU-bound; more memory is faster at roughly constant cost), 120s timeout. `batch_size=1` so one pathological PDF can never poison a batch (and makes partial-batch-failure reporting unnecessary).

`_queue_with_dlq` gains a `visibility_timeout` parameter; **`extract_queue` moves to 12 minutes** (6× the function timeout, AWS's guidance), `synthesize_queue` keeps 5 minutes. A visibility timeout shorter than the function timeout would cause duplicate concurrent invocations on the *same* message — the §8.3 claim would catch it, but the queue should be correct on its own.

### 6.3 IAM grants (all via CDK `grant*` — no hand-written policy documents)

| grant | why |
|---|---|
| `pdf_bucket.grant_read(extract_fn)` | `GetObject` on the imported bucket (role-based, same account) |
| `table.grant_read_write_data(extract_fn)` | `GetItem`, `PutItem`, `UpdateItem`, `Query`, `BatchWriteItem`, `DeleteItem` |
| `SqsEventSource` (implicit) | `ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` on `extract_queue` |
| `s3n.SqsDestination` (implicit) | queue resource policy allowing `s3.amazonaws.com` to `SendMessage`, conditioned on the bucket `SourceArn` |
| `s3:PutBucketNotification` (implicit) | on the `BucketNotificationsHandler` role |
| **API Lambda** | **nothing new** — `pdf_bucket.grant_read_write(fn)` already covers `s3:PutObject`, which is what a presigned POST needs |

### 6.4 `infra/app.py`

```python
pipeline = PipelineStack(app, stack_name("Pipeline", environment),
                         pdf_bucket_name=storage.pdf_bucket.bucket_name,
                         table=storage.table,
                         environment=environment, git_sha=git_sha, env=env)
```
declared **after** `storage`, comment updated to record the new `Storage → Pipeline → Api` chain.

### 6.5 Infra test impact (`infra/tests/test_synth.py`)

`PipelineStack` now contains a Docker image asset, so its synth requires a Docker daemon:
- `test_pipeline_stack_synthesizes` / `test_pipeline_stack_has_redrive_policies` gain `@pytest.mark.docker`, and the generic `_synth(...)` helper no longer fits — add `_synth_pipeline_stack(environment)` that builds a `StorageStack` first.
- `test_stack_naming_convention`'s parametrization must drop `PipelineStack` from the generic list.
- New assertions: exactly 4 SQS queues **and 2 Lambda functions** (extract + notifications handler); extract queue `VisibilityTimeout == 720`; one `Custom::S3BucketNotifications`; an `AWS::SQS::QueuePolicy` whose statement principal is `s3.amazonaws.com`; one `AWS::Lambda::EventSourceMapping` with `BatchSize: 1`; the extract function's role policy contains `s3:GetObject` and `dynamodb:UpdateItem`.

CI's `test-infra` job runs on `ubuntu-latest` (has Docker) and already runs docker-marked ApiStack tests, so nothing in CI changes.

---

## 7. Extraction & chunking

### 7.1 Dependency

`backend/pyproject.toml` `[project].dependencies` += `"pymupdf>=1.24,<2"`; regenerate `uv.lock`. Import as `import pymupdf`. Verify the lock resolves a **manylinux wheel** for `x86_64` — an sdist would try to build MuPDF from source inside `public.ecr.aws/lambda/python:3.12` and fail. `pymupdf_extractor.py` is the **only** module allowed to import it.

### 7.2 Ports and value objects — `domain/extraction.py`

```python
@dataclass(frozen=True)
class ExtractedPage:
    number: int        # 1-based
    char_start: int    # into ExtractedDocument.text
    char_end: int      # half-open

@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    pages: tuple[ExtractedPage, ...]
    page_count: int
    dropped_running_lines: int
    dropped_footnote_blocks: int

class ExtractionError(DomainError):
    status_code = 422
    def __init__(self, reason: ExtractionFailure, message: str) -> None:
        super().__init__(message); self.reason = reason

class PdfTextExtractor(Protocol):
    def extract(self, pdf_bytes: bytes) -> ExtractedDocument: ...  # pragma: no cover

def page_range_for(pages: Sequence[ExtractedPage], char_start: int, char_end: int) -> tuple[int, int]
```

`page_range_for` uses `bisect_right` over the pages' `char_start`s. Pages that produced no text get a zero-length range and are therefore never selected.

### 7.3 Layout policy — `domain/layout.py` (pure, no pymupdf)

```python
@dataclass(frozen=True)
class TextSpan:
    text: str
    size: float
    origin_y: float

@dataclass(frozen=True)
class TextLine:
    spans: tuple[TextSpan, ...]
    x0: float; y0: float; x1: float; y1: float
    @property
    def text(self) -> str
    @property
    def size(self) -> float

@dataclass(frozen=True)
class TextBlock:
    lines: tuple[TextLine, ...]
    x0: float; y0: float; x1: float; y1: float

@dataclass(frozen=True)
class PageLayout:
    number: int
    width: float; height: float
    blocks: tuple[TextBlock, ...]
```

**(a) Running headers/footers** — `find_running_lines(pages) -> frozenset[str]`

1. Band definition: header band = `y1 <= 0.08 * height`; footer band = `y0 >= 0.92 * height`. Only lines *entirely* inside a band are candidates.
2. Normalize each candidate: `normalize_running(text)` → casefold, NFKC, collapse whitespace, replace every run of digits with `#`, strip leading/trailing punctuation.
3. A normalized form appearing on **≥ 50% of pages** (and only when `page_count >= 3`) is a running line → dropped everywhere.
4. **Unconditional rule**: a band line that's only an Arabic/roman numeral or `— 12 —`-style decoration is a bare page number → dropped, regardless of repetition. Covers 1-2 page docs.

Non-goal: headings sitting in the body region are kept.

**(b) Footnotes** — `is_footnote_block(block, *, body_size, page_height) -> bool`

1. `body_size = modal_span_size(pages)`: char-weighted mode of span sizes.
2. A block is a footnote when: it starts in the bottom 25% (`block.y0 >= 0.75 * height`), its char-weighted median span size is `<= 0.85 * body_size`, and it is not the only block on the page.
3. Footnote blocks are **dropped from the narration stream** (counted via `dropped_footnote_blocks`, not stored).

**(c) Inline superscript footnote markers** — `strip_superscript_markers(line) -> str`

A span is dropped when its size is `<= 0.75 * body_size` **and** its baseline sits above the line's dominant baseline **and** its text is only digits / `*†‡§¶` / bracketed digits.

**Known limitation:** multi-column layouts (`sort=True` interleaves columns). Out of scope for phase 3 — see OQ-6.

### 7.4 Chunking — `domain/chunking.py` (pure, string in / boundaries out)

```python
DEFAULT_TARGET_CHARS = 1800
DEFAULT_MAX_CHARS    = 2600
DEFAULT_MIN_CHARS    = 400

@dataclass(frozen=True)
class ChunkBoundary:
    char_start: int
    char_end: int

def chunk_text(text: str, *, target: int = ..., maximum: int = ..., minimum: int = ...) -> list[ChunkBoundary]
def split_paragraphs(text: str) -> list[tuple[int, int]]
def split_sentences(text: str, offset: int = 0) -> list[tuple[int, int]]
```

Sizing rationale: ~1800 chars ≈ 300 words ≈ 2 minutes of speech — right granularity for phase 4 parallel synthesis, phase 6 seek/highlight resolution, phase 7 chat context windows.

Boundary algorithm, in strict preference order:
1. **Paragraph-greedy.** Close the chunk when adding the next paragraph would exceed `maximum`, or once past `target` at a paragraph boundary.
2. **Sentence split** when one paragraph alone exceeds `maximum`. Regex with an abbreviation guard list (`Mr Mrs Ms Dr Prof St Jr Sr vs etc e.g i.e cf Fig No Vol Ch pp` + single-capital initials).
3. **Whitespace split** when one sentence alone exceeds `maximum`: break at the last whitespace at or before `maximum`. Never mid-word.
4. **Runt merge.** A final chunk shorter than `minimum` merges into the previous one if that stays ≤ `maximum`.
5. **Page boundaries are irrelevant to chunking** — pages are recorded per chunk (§7.5), not respected as breaks.

**Invariants (each has a test):** chunks exactly partition the text (`chunks[0].char_start==0`, contiguous, `chunks[-1].char_end==len(text)`); stored `text` is the raw slice (no stripping); every emitted chunk is non-blank; `chunk_text("")`/whitespace-only → `[]` (caller turns into `NO_TEXT_LAYER`).

### 7.5 Assembly and page mapping — `infrastructure/pymupdf_extractor.py`

```python
class PyMuPdfTextExtractor:
    def __init__(self, *, max_bytes: int = MAX_UPLOAD_BYTES) -> None: ...
    def extract(self, pdf_bytes: bytes) -> ExtractedDocument: ...
```

1. `len(pdf_bytes) > max_bytes` → `ExtractionError(TOO_LARGE)`.
2. `pymupdf.open(stream=pdf_bytes, filetype="pdf")` — parse errors → `ExtractionError(CORRUPT_PDF)`.
3. `doc.needs_pass` → try `doc.authenticate("")`; still needs a password → `ExtractionError(ENCRYPTED_PDF)` (owner-password-only PDFs authenticate with `""` and extract normally).
4. `doc.page_count == 0` → `ExtractionError(EMPTY_PDF)`.
5. Pass 1: build `PageLayout` per page via `page.get_text("dict", sort=True, ...)`.
6. Compute `body_size` and `find_running_lines(pages)` across **all** pages (two-pass, whole doc in memory).
7. Pass 2 per page: drop running lines, drop footnote blocks, strip superscript markers, join lines (de-hyphenate `-`/soft-hyphen line breaks), blocks separated by `"\n\n"`.
8. Normalize: NFKC, `\r\n→\n`, ` →' '`, strip trailing spaces, collapse 3+ newlines to 2 — **before** offsets are computed.
9. Concatenate pages with `"\n\n"`, recording each page's `[char_start, char_end)`.
10. `len(text.strip()) < MIN_TEXT_CHARS (=100)` → `ExtractionError(NO_TEXT_LAYER)` (scanned-image case).

`ExtractBook` maps each `ChunkBoundary` to a `Chunk` via `page_range_for`.

---

## 8. The extract Lambda: error handling, idempotency, ordering

### 8.1 Handler — `interface/extract_handler.py`

```python
def handler(event: dict, context: object) -> None
def handle_records(records: Sequence[dict], use_case: ExtractBook) -> list[ExtractBookResult]
def s3_objects_from_sqs_body(body: str) -> list[tuple[str, str, int]]   # (bucket, key, size)
```

Two S3-event gotchas handled explicitly:
- **`s3:TestEvent`** (posted once when the notification is configured) — ignored, message returns normally.
- **URL-encoded keys** — always `urllib.parse.unquote_plus(...)`.

Composition root built per invocation (never at import) so `mock_aws()` and LocalStack both work.

### 8.2 `ExtractBook` use case — `application/extraction.py`

```python
@dataclass(frozen=True)
class ExtractBookCommand:
    user_id: str
    book_id: str
    source_key: str
    size_bytes: int | None = None

@dataclass(frozen=True)
class ExtractBookResult:
    outcome: Literal["EXTRACTED", "FAILED", "SKIPPED"]
    chunks_written: int = 0
    page_count: int = 0
    reason: str | None = None
```

Sequence:

1. `book = book_repository.get(user_id, book_id)` → `None` → `SKIPPED("BOOK_NOT_FOUND")`.
2. `book.source_key != command.source_key` → `SKIPPED("KEY_MISMATCH")`.
3. **Claim**: `update_status(..., EXTRACTING, expected_statuses=(UPLOADED, FAILED), updated_at=now)`.
   - `ConflictError` → `SKIPPED("ALREADY_CLAIMED")` — the duplicate-delivery guard: without it a redelivery after phase 4 has begun would `save_all` fresh `PENDING` chunks and wipe `audioKey`s already written.
   - `EXTRACTED`/`READY` are deliberately **not** in `expected_statuses`; `FAILED` **is** (retry support).
   - `NotFoundError` → `SKIPPED("BOOK_NOT_FOUND")`.
4. Everything from here wrapped:
   ```python
   try:
       pdf = pdf_storage.get_bytes(key=command.source_key)
       document = extractor.extract(pdf)
       boundaries = chunk_text(document.text)
       if not boundaries: raise ExtractionError(NO_TEXT_LAYER, ...)
       chunk_repository.delete_for_book(book_id)
       chunk_repository.save_all(chunks)
       book_repository.update_status(..., EXTRACTED, chunks_total=len(chunks),
                                     page_count=document.page_count,
                                     clear_failure_reason=True, updated_at=now)
   except ExtractionError as exc:
       book_repository.update_status(..., FAILED, failure_reason=exc.reason.value, updated_at=now)
       return ExtractBookResult("FAILED", reason=exc.reason.value)
   except Exception:
       book_repository.update_status(..., UPLOADED, updated_at=now)   # release the claim
       raise
   ```

**The `except Exception` release is not optional** — without it a transient error after the claim leaves the book wedged in `EXTRACTING` forever, since every SQS retry hits the same claim condition.

### 8.3 Permanent vs transient — the crux

| condition | reason code | book status | SQS message |
|---|---|---|---|
| corrupt / unparseable PDF | `CORRUPT_PDF` | `FAILED` | deleted |
| password-protected | `ENCRYPTED_PDF` | `FAILED` | deleted |
| 0 pages | `EMPTY_PDF` | `FAILED` | deleted |
| scanned image, no text layer | `NO_TEXT_LAYER` | `FAILED` | deleted |
| object larger than the cap | `TOO_LARGE` | `FAILED` | deleted |
| unexpected `pymupdf` error | `UNKNOWN` | `FAILED` | deleted |
| book missing / key mismatch / already claimed | — | unchanged | deleted |
| S3 5xx, DynamoDB throttle, timeout | — | reverted to `UPLOADED` | retried, then DLQ after 3 |

Retrying a corrupt PDF three times and parking it in a DLQ gives zero user feedback; `FAILED` + `failureReason` gives phase-6 something actionable. Swallowing a DynamoDB throttle would silently lose a book.

**DLQ residue:** a message that dies in the DLQ leaves its book in `UPLOADED` indefinitely. Phase 3 accepts this (documented manual redrive); see OQ-4 for phase 8 hardening.

---

## 9. Local dev & LocalStack

### 9.1 `local/setup.sh`

After bucket/queue creation, add idempotent notification config (`put-bucket-notification-configuration` targeting the extract queue ARN, prefix `books/`, suffix `.pdf`). S3→SQS notification is long-standing LocalStack **community** functionality — no `SERVICES` change, no Pro token.

### 9.2 `extract-worker` compose service

LocalStack community can't run our ECR-image Lambda, so run the **same handler code** in a poll loop instead of faking the Lambda:

```yaml
extract-worker:
  build: { context: ./backend, target: dev }
  restart: unless-stopped
  environment: <identical to backend service>
  command: ["uv", "run", "python", "-m", "src.contexts.library.interface.local_extract_worker"]
  volumes: [./backend:/app]
  depends_on: { localstack: { condition: service_healthy } }
```

`local_extract_worker.py` splits `poll_once(sqs, queue_url) -> int` (testable) from `main()` (the infinite loop, excluded from coverage) — calls the same `handle_records` the Lambda's `handler` calls.

`Makefile`'s `up` target adds `extract-worker`.

### 9.3 `local/smoke_test.py` — the real proof

New `check_upload_and_extraction(...)`, gated on `--login-username`/`--login-password` like the existing authenticated checks. `deploy-prod.yml` passes no `--login-*` args, so this never runs against prod.

Steps: `POST /books` → capture `upload`; build multipart body by hand (stdlib only, fields first, file last) → `POST` to `upload.url` → `204`; poll `GET /books/{id}` up to `--extraction-timeout` (default 120s) until `EXTRACTED`/`FAILED`, assert `EXTRACTED` + `chunksTotal >= 1` + `pageCount >= 1`; `GET /books/{id}/chunks` → assert ≥ 1 chunk, concatenated text contains the fixture's marker string.

PDF bytes: a **~1KB base64 constant** `_SMOKE_PDF_B64` (two-page, body text `"Bookloud smoke test"`, repeated running header + footer page number, so the smoke test also proves the header/footer filter runs in the deployed Lambda). Docstring records the PyMuPDF snippet used to regenerate it. Alternative in OQ-7.

Small refactor: extract the duplicated `InitiateAuth` block into `_login(...) -> id_token`.

### 9.4 Presigned URLs against LocalStack

`generate_presigned_post` builds URLs from the client's endpoint, so inside compose it'd return `http://localstack:4566/...` — unreachable from the host. Add `s3_public_endpoint_url` to `src/config.py` (empty in AWS, `http://localhost:4566` in compose), mirroring the existing `COGNITO_ENDPOINT` escape hatch. `S3PdfStorage`'s signing client uses `endpoint_url = settings.s3_public_endpoint_url or settings.aws_endpoint_url or None`. No infra change — never set by CDK.

---

## 10. Test plan

All new tests under `backend/tests/contexts/library/`, reusing existing fixtures. Coverage gate stays 90%.

### 10.1 Fixtures — generated, not committed

New `backend/tests/contexts/library/pdf_fixtures.py`. Generated at test time with PyMuPDF itself (already a runtime dep, byte-level geometry control, no binary to keep in sync).

| builder | shape | exercises |
|---|---|---|
| `simple_text_pdf(text)` | 1 page, 11pt body | happy path, offsets |
| `multipage_pdf(pages=5)` | 5 pages, distinct body text | page mapping, cross-page chunks |
| `headered_pdf(pages=6)` | running header + footer page number every page | **header/footer edge case** |
| `footnote_pdf()` | superscript `12` + note block | **footnote edge case** |
| `hyphenated_pdf()` | word broken across a line with `-` | de-hyphenation |
| `long_paragraph_pdf(chars=8000)` | one 8000-char paragraph | sentence-split path |
| `encrypted_pdf()` | AES-256, user password | `ENCRYPTED_PDF` |
| `owner_password_pdf()` | owner password only | must extract successfully (regression guard) |
| `image_only_pdf()` | inserted Pixmap, no text | `NO_TEXT_LAYER` |
| `empty_pdf()` | 0 pages | `EMPTY_PDF` |
| `CORRUPT_PDF_BYTES` | `b"%PDF-1.7\n<<not really a pdf>>"` | `CORRUPT_PDF` |

### 10.2 Test files

- **`test_chunking.py`** (pure) — exact-partition invariant, empty/whitespace → `[]`, paragraph boundaries preferred, sentence-split never mid-word, abbreviation guard, whitespace-split fallback, runt merge (both directions), unicode offsets.
- **`test_layout.py`** (pure) — `normalize_running` equivalence, repetition threshold (6/6 vs 2/6 pages), body-region repeats NOT dropped, bare page number dropped without repetition, roman numerals, `modal_span_size` mode-not-mean, `is_footnote_block` all branches, `strip_superscript_markers` all branches.
- **`test_pymupdf_extractor.py`** (generated PDFs) — all fixture builders exercised, NFKC ligature fix, oversized bytes → `TOO_LARGE`.
- **`test_s3_keys.py`** — round-trip, rejects malformed/traversal keys.
- **`test_s3_pdf_storage.py`** (moto) — presigned fields, exact key, content-length-range condition, `get_bytes` round-trip and missing-key error.
- **`test_book_repository.py`** (extend) — `update_status` kwarg combinations, `ConflictError` vs `NotFoundError` disambiguation, `clear_failure_reason` removes the attribute, both failure_reason+clear → `ValueError`.
- **`test_extraction_use_case.py`** — happy path, save-before-status-flip ordering (spy), all `SKIPPED` branches, `FAILED` branches, un-wedging on transient error, stale-chunk cleanup on re-extraction.
- **`test_extract_handler.py`** — full SQS envelope through real generated PDF, `s3:TestEvent` ignored, no-Records body ignored, URL-decoding, batch of 2.
- **`test_controllers.py`** (extend) — `POST /books`, `upload-url` re-issue (200/404/409), `GET /chunks` (200/404/[]).
- Existing tests updated: `test_use_cases.py`, `test_schemas.py`, `test_mappers.py`, `test_domain.py`, `test_main.py`.
- `infra/tests/test_synth.py` — per §6.5.

---

## 11. Concrete file list

**`backend/src/contexts/library/`**
- `domain/storage.py` (new), `domain/extraction.py` (new), `domain/layout.py` (new), `domain/chunking.py` (new)
- `domain/value_objects.py`, `domain/book.py`, `domain/chunk.py`, `domain/repository.py` (changed)
- `application/commands.py`, `application/use_cases.py` (changed), `application/extraction.py` (new)
- `infrastructure/s3_keys.py`, `infrastructure/s3_pdf_storage.py`, `infrastructure/pymupdf_extractor.py` (new)
- `infrastructure/book_mapper.py`, `infrastructure/chunk_mapper.py`, `infrastructure/dynamodb_book_repository.py` (changed)
- `interface/controllers.py`, `interface/schemas.py`, `interface/dependencies.py` (changed)
- `interface/extract_handler.py`, `interface/local_extract_worker.py` (new)

**`backend/`**: `src/config.py` (changed), `pyproject.toml` + `uv.lock` (changed)

**`backend/tests/contexts/library/`**: `pdf_fixtures.py`, `test_chunking.py`, `test_layout.py`, `test_pymupdf_extractor.py`, `test_s3_keys.py`, `test_s3_pdf_storage.py`, `test_extraction_use_case.py`, `test_extract_handler.py` (new); `conftest.py`, `test_book_repository.py`, `test_use_cases.py`, `test_controllers.py`, `test_mappers.py`, `test_schemas.py`, `test_domain.py`, `../../test_main.py` (changed)

**`infra/`**: `stacks/pipeline_stack.py`, `stacks/config.py`, `app.py`, `tests/test_synth.py` (changed)

**`local/`**: `setup.sh`, `smoke_test.py` (changed)

**Root**: `docker-compose.yml`, `Makefile`, `README.md`, `IMPLEMENTATION_PLAN.md` (changed)

---

## 12. Implementation sequence

1. Pure domain first: `chunking.py` + tests, then `layout.py` + tests. Zero AWS, zero PDFs.
2. `value_objects.py`, `book.py`, `chunk.py`, `extraction.py` (ports), `s3_keys.py` + tests. Still no AWS.
3. Mappers + `dynamodb_book_repository.update_status` rewrite + tests. **Gate**: `ConflictError`-vs-`NotFoundError` and `clear_failure_reason` tests must pass before continuing.
4. Add `pymupdf`, `pdf_fixtures.py`, `pymupdf_extractor.py` + tests. Confirm manylinux wheel resolves.
5. `s3_pdf_storage.py` + tests → `RequestBookUpload`/`ReissueBookUpload` → controllers/schemas/dependencies → tests. `POST /books` works locally.
6. `ExtractBook` + `extract_handler.py` + tests. Full suite green at ≥90%.
7. `pipeline_stack.py` + `config.py` + `app.py` + infra tests. **Watch for the circular-dependency error** — if `cdk synth` complains, the bucket wasn't re-imported (§6.1).
8. `local/setup.sh` notification + compose worker + Makefile. Manually upload once to prove S3→SQS fires and the presigned URL is host-reachable.
9. `local/smoke_test.py` + `make smoke` — the phase's real gate.
10. README + `IMPLEMENTATION_PLAN.md` checklist. Push; `deploy-pr` runs the smoke test against real AWS with the real Lambda.

---

## 13. Open Questions — ALL DECIDED

**OQ-1 — Bounded context. DECIDED: stay inside `contexts/library/`.** Extraction produces Library's own aggregates; no separate `ingestion` context.

**OQ-2 — S3→SQS wiring. DECIDED: direct notification via re-imported bucket** (dodges the cross-stack cycle, matches what `local-smoke` CI exercises). Not EventBridge.

**OQ-3 — Footnote disposal. DECIDED: drop entirely**, counted/logged, not stored. Revisit in phase 6 if wanted (means re-extracting every book at that point).

**OQ-4 — DLQ residue. DECIDED: accept + document manual redrive for phase 3.** DLQ-consumer + CloudWatch alarm deferred to phase 8 CD hardening.

**OQ-5 — `BookStatus.EXTRACTING` deploy-order race. DECIDED: accept the brief window** where an old API container could 400 reading a book mid-claim — negligible for a single-user app with no traffic during deploy.

**OQ-6 — Multi-column PDFs. DECIDED: out of scope for phase 3.** Column-sort will interleave text on true 2-column pages; revisit later if needed.

**OQ-7 — Smoke-test PDF. DECIDED: base64 constant inline in `smoke_test.py`** (keeps it stdlib-only, clean-checkout).

**OQ-8 — Upload cap. DECIDED: 50MiB / 15-min presign expiry**, as planned. `memory_size` sized accordingly.

**OQ-9 — Chunk sizing. DECIDED: ~1800 target / 2600 max / 400 min chars** (~2 min speech per chunk).

**OQ-10 — Orphan cleanup. DECIDED: no `DELETE /books/{id}` route this phase.** Use case stays unrouted since phase 2; defer to phase 6 where the UI would expose it.

---

### Critical Files for Implementation
- infra/stacks/pipeline_stack.py
- backend/src/contexts/library/infrastructure/dynamodb_book_repository.py
- backend/src/contexts/library/interface/controllers.py
- backend/src/contexts/library/domain/value_objects.py
- local/smoke_test.py
