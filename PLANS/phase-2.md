# Phase 2 — Storage & data model

Goal: a working, tested `Library` bounded context — `Book`/`Chunk` entities, repository ports, and
DynamoDB adapters over the **already-deployed** single table — plus the minimum HTTP surface needed
to prove it works against real infra.

**Verified: Phase 2 creates no infrastructure.** `infra/stacks/storage_stack.py` already builds real
resources, not stubs:
- `dynamodb.Table` named `bookloud-{env}` (`table_name()` in `stacks/config.py`), partition key
  **`PK` (S)**, sort key **`SK` (S)**, `PAY_PER_REQUEST`, PITR on prod only, `RETAIN` on prod /
  `DESTROY` elsewhere.
- Three buckets (`PdfBucket` with CORS for phase 3's presigned upload, `AudioBucket`, `MarksBucket`),
  CDK-auto-named, surfaced as `CfnOutput`.
- `infra/stacks/api_stack.py` **already** sets `TABLE_NAME`/`PDF_BUCKET`/`AUDIO_BUCKET`/`MARKS_BUCKET`
  on the API Lambda and **already** grants `table.grant_read_write_data(fn)` plus bucket read/write.
  `grant_read_write_data` includes `dynamodb:Query`, `BatchWriteItem`, `UpdateItem` and
  `ConditionCheckItem` — everything this phase needs.
- `local/setup.sh` already creates `bookloud-local` with the identical `PK`/`SK` schema.

So `infra/` and `local/setup.sh` need **zero changes**. Phase 2 is backend application code + tests
(+ one optional infra assertion, §9).

Reference implementation (patterns copied deliberately): `/home/yngvarr/Projects/jgautocar` —
`backend/jgautocar/contexts/<ctx>/{domain,application,infrastructure,interface}`, repository ports as
`Protocol` in `domain/repository.py`, adapters satisfying them **structurally** (no inheritance),
`infrastructure/<entity>_mapper.py` for item↔entity conversion, `moto`'s `mock_aws()` in the test
fixtures.

---

## 1. DDD context structure

The context lands in Phase 0's reserved `backend/src/contexts/` directory:

```
backend/src/contexts/library/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── value_objects.py            # BookStatus, ChunkStatus
│   ├── book.py                     # Book — aggregate root
│   ├── chunk.py                    # Chunk — its own aggregate (see §1.2)
│   └── repository.py               # BookRepository, ChunkRepository (Protocols)
├── application/
│   ├── __init__.py
│   ├── commands.py                 # CreateBookCommand
│   └── use_cases.py                # CreateBook, GetBook, ListBooks, DeleteBook, ListBookChunks
├── infrastructure/
│   ├── __init__.py
│   ├── keys.py                     # PK/SK construction + parsing helpers
│   ├── book_mapper.py              # dict <-> Book
│   ├── chunk_mapper.py             # dict <-> Chunk
│   ├── dynamodb_book_repository.py
│   └── dynamodb_chunk_repository.py
└── interface/
    ├── __init__.py
    ├── dependencies.py             # FastAPI Depends providers (the composition root)
    ├── schemas.py                  # Book/Chunk -> response JSON
    └── controllers.py              # GET /books, GET /books/{book_id}
```

This mirrors jgautocar's `contexts/cars/` exactly. Deviation from jgautocar: it uses Flask blueprints
resolved through a `Container` in `flask.current_app.extensions`; bookloud uses FastAPI, so the
equivalent composition root is `interface/dependencies.py` with `Depends(...)` providers (§5.3).

Two app-level infrastructure modules land alongside, **not** inside the context (jgautocar puts these
at `jgautocar/infrastructure/`, and phases 5/7 will reuse them from other contexts):

- `backend/src/infrastructure/dynamodb.py` — `library_table()` returning
  `resource("dynamodb").Table(settings.table_name)` via the existing `src/infrastructure/aws.py`
  factory (so `AWS_ENDPOINT_URL`/LocalStack keeps working for free).
- `backend/src/infrastructure/clock.py` — `SystemClock` implementing `shared_kernel`'s `Clock`.
- `backend/src/infrastructure/ids.py` — `Uuid4IdGenerator` implementing `IdGenerator`.

`shared_kernel/application/ports.py`'s docstring already says "Empty implementations land alongside
the contexts that need them, starting in phase 2" — this is that. Do **not** redefine the protocols;
import them and satisfy them structurally.

### 1.1 Entities

**`Book`** (`domain/book.py`) — aggregate root, plain `@dataclass`:

| field | type | notes |
|---|---|---|
| `id` | `str` | UUID4, from `IdGenerator` |
| `user_id` | `str` | **Cognito `sub`**, per phase-1 §1.2 — never the username |
| `title` | `str` | non-blank, stripped |
| `status` | `BookStatus` | `UPLOADED` at creation |
| `chunks_total` | `int` | 0 until phase 3's extraction |
| `chunks_done` | `int` | 0; incremented atomically in phase 4 |
| `page_count` | `int` | 0 until phase 3 |
| `created_at` | `str` | ISO-8601 **UTC** |
| `updated_at` | `str` | ISO-8601 UTC (see Q6) |

```python
@classmethod
def create(cls, *, id: str, user_id: str, title_raw: object, now: datetime) -> Book
```
Raises `ValidationError("Title is required")` on a blank/non-str title; raises
`ValidationError("User id is required")` on a blank `user_id` (defence in depth — an empty partition
key would silently create a cross-tenant bucket). `page_count`/`chunks_total`/`chunks_done` default
to 0; `status` is `BookStatus.UPLOADED`.

Deliberately **no** `set_status()` / `mark_extracted()` / `mark_ready()` methods in Phase 2 — those
transitions belong to phases 3 and 5 and get their own domain methods + tests then. Phase 2 only
needs a book to exist and be readable.

Deliberately **no** `source_key`/S3 attribute. The mappers use `item.get(name, default)` throughout
(jgautocar's convention), so phase 3 adding `sourceKey` is a purely additive one-line change to
`book_mapper.py` with no migration.

**`Chunk`** (`domain/chunk.py`) — plain `@dataclass`:

| field | type | notes |
|---|---|---|
| `book_id` | `str` | partition of the chunk item |
| `index` | `int` | ordinal from extraction; identity is `(book_id, index)` |
| `user_id` | `str` | the owning book's `sub` — **not** an auth mechanism, see §6 |
| `text` | `str` | |
| `char_start` | `int` | offset into the book's full extracted text |
| `char_end` | `int` | |
| `audio_key` | `str \| None` | S3 key in the audio bucket; phase 4 fills it |
| `marks_key` | `str \| None` | S3 key in the marks bucket; phase 4 fills it |
| `status` | `ChunkStatus` | `PENDING` at creation |

```python
@classmethod
def create(cls, *, book_id: str, user_id: str, index: int, text: str,
           char_start: int, char_end: int) -> Chunk
```
Validates `index >= 0` and `char_end >= char_start`, else `ValidationError`.

No `created_at` on chunks — the spec's chunk row has none, and the SK already orders them.

### 1.2 Aggregate boundaries — Book and Chunk are separate aggregates

jgautocar embeds `tasks`/`photos` **inside** the `Car` item and therefore has no `TaskRepository`.
Bookloud must do the **opposite**, and `book.py`'s module docstring should say why (mirroring how
`car.py`'s docstring justifies its choice):

1. The single-table schema in `IMPLEMENTATION_PLAN.md` puts chunks in separate items
   (`BOOK#<bookId>` / `CHUNK#<n>`), not nested in the book row.
2. Chunk `text` for a whole book blows past DynamoDB's 400 KB per-item limit.
3. **The decisive reason:** `IMPLEMENTATION_PLAN.md`'s key architecture decisions specify chunk
   synthesis runs in parallel, one Lambda per chunk, with fan-in via an *atomic* `chunksDone`
   counter. Read-modify-write of one embedded aggregate from N concurrent Lambdas loses updates.
   Separate items + targeted `UpdateItem` is the whole point of the schema.

Consequence, stated explicitly in the docstring: **`Book.chunks_done`/`chunks_total` are eventually
consistent with the chunk items by design.** They are counters, not a derived invariant, and no code
should ever recompute them by listing chunks.

### 1.3 `BookStatus` / `ChunkStatus` — the minimal-set question

`domain/value_objects.py`:

```python
class BookStatus(StrEnum):
    UPLOADED  = "UPLOADED"    # set by Book.create() — phase 2/3 (presign)
    EXTRACTED = "EXTRACTED"   # first SET in phase 3 (extract Lambda)
    READY     = "READY"       # first SET in phase 5 (stitcher)
    FAILED    = "FAILED"      # first SET in phase 3/4 error paths

class ChunkStatus(StrEnum):
    PENDING = "PENDING"       # set by Chunk.create() — phase 3
    DONE    = "DONE"          # first SET in phase 4
    FAILED  = "FAILED"        # first SET in phase 4
```

**DECIDED: declare the whole lifecycle now, but only ever *write* `UPLOADED`/`PENDING` in Phase 2
code.** Rationale — the enum is a *parsing* concern before it is a workflow concern: the mapper must
round-trip whatever string is already in the table, and a deliberately-truncated enum becomes a
forward-compat landmine the moment a newer Lambda writes `EXTRACTED` and an older API container
tries to read it (`ValueError` on every `GET /books`). The *behaviour* that belongs to phase 3+ (the
transition methods) is what Phase 2 omits, and that's the line that actually matters. Each member
carries an inline comment naming the phase that first sets it.

Use `enum.StrEnum` (Python 3.12) so `str(status)` and DynamoDB serialization are trivial, and add a
`classmethod parse(value: object) -> BookStatus` raising `ValidationError("Invalid book status")` for
unknown values (jgautocar's `CarStatus.__post_init__` equivalent).

---

## 2. Repository ports

`domain/repository.py` — `Protocol`s, not ABCs (jgautocar's stated convention; adapters satisfy them
structurally with no inheritance). Every method body is `...  # pragma: no cover`, matching
`shared_kernel/application/ports.py`.

```python
class BookRepository(Protocol):
    def save(self, book: Book) -> None: ...
    def get(self, user_id: str, book_id: str) -> Book | None: ...
    def list_for_user(self, user_id: str) -> list[Book]: ...
    def delete(self, user_id: str, book_id: str) -> None: ...
    def update_status(self, user_id: str, book_id: str, status: BookStatus) -> None: ...
    def increment_chunks_done(self, user_id: str, book_id: str) -> int: ...
```

```python
class ChunkRepository(Protocol):
    def save(self, chunk: Chunk) -> None: ...
    def save_all(self, chunks: Sequence[Chunk]) -> None: ...
    def get(self, book_id: str, index: int) -> Chunk | None: ...
    def list_for_book(self, book_id: str) -> list[Chunk]: ...
    def update_status(self, book_id: str, index: int, status: ChunkStatus, *,
                      audio_key: str | None = None,
                      marks_key: str | None = None) -> None: ...
    def delete_for_book(self, book_id: str) -> int: ...
```

Design notes for the implementer:

- **`save()` vs `update_status()`/`increment_chunks_done()` — a real trap, document it in the module
  docstring.** `save()` is a whole-item `put_item`; running it concurrently with the phase-4 fan-in
  would clobber `chunksDone`. Rule: `save()` is for *creation* and owner-initiated edits only;
  every status/counter mutation goes through the targeted-update methods. Both targeted methods
  carry `ConditionExpression="attribute_exists(PK)"` and raise `NotFoundError` on
  `ConditionalCheckFailedException` — without it, DynamoDB's `SET`/`ADD` **upserts**, so an update
  for a deleted book would silently resurrect a half-formed item. This is a genuine correctness bug,
  not a nicety.
- **DECIDED: `increment_chunks_done` is in the port now, not deferred to phase 4**, because it is
  the only operation that cannot be expressed as `save()`. Putting it here prevents phase 4 from
  bolting a second write path onto the table outside the repository. It returns the new value
  (`UpdateExpression="ADD chunksDone :one"`, `ReturnValues="UPDATED_NEW"`) — phase 5's fan-in checks
  `new == chunksTotal`.
- **`save_all`** exists because phase 3 writes N chunks at once; implement with
  `table.batch_writer()`, which handles the 25-item `BatchWriteItem` cap and unprocessed-item retries
  for you. Phase 2 tests use it as the fixture-seeding path.
- **`delete_for_book`** exists because DynamoDB has no cascade delete. A `DeleteBook` that leaves
  `BOOK#<id>/CHUNK#*` items behind is an orphan-data bug that would only surface in phase 6.
  Implementation: query the keys (`ProjectionExpression="PK,SK"`), then `batch_writer().delete_item`.
  Returns the count deleted so the use case can log it.
- `update_status(chunk)` writes `audio_key`/`marks_key` **only when not `None`** (build the
  `UpdateExpression` conditionally), so phase 4 can set status+keys in one call and phase 3 can flip
  status alone without nulling keys.

---

## 3. Single-table access patterns

### 3.1 Key helpers — `infrastructure/keys.py`

```python
USER_PREFIX  = "USER#"
BOOK_PREFIX  = "BOOK#"
CHUNK_PREFIX = "CHUNK#"
CHUNK_INDEX_WIDTH = 6

def pk_user(user_id: str) -> str:      return f"{USER_PREFIX}{user_id}"
def sk_book(book_id: str) -> str:      return f"{BOOK_PREFIX}{book_id}"
def pk_book(book_id: str) -> str:      return f"{BOOK_PREFIX}{book_id}"
def sk_chunk(index: int) -> str:       return f"{CHUNK_PREFIX}{index:0{CHUNK_INDEX_WIDTH}d}"
def book_id_from_sk(sk: str) -> str:   ...   # strips BOOK#
def chunk_index_from_sk(sk: str) -> int: ... # strips CHUNK#, int()
```

**DECIDED: zero-pad the chunk index to width 6** (`CHUNK#000007`) — load-bearing, since the SK is a
string and unpadded `CHUNK#10` would sort *before* `CHUNK#2`, breaking playback order from chunk 10
onward. Width 6 supports 999,999 chunks (a 1000-page book runs to ~2,000) and is **immutable once
any data exists** — say so in a module comment. There is a dedicated regression test for this (§7).

Attribute-name constants (`PK`, `SK`) also live here so no string literal `"PK"` appears in the
repositories. Comment: these must stay in lockstep with `infra/stacks/storage_stack.py` and
`local/setup.sh` — the same "separately deployed projects, cannot share an import" rule that
`src/config.py` and `infra/stacks/config.py` already carry.

### 3.2 The queries

| operation | request |
|---|---|
| `get(user_id, book_id)` | `get_item(Key={"PK": pk_user(u), "SK": sk_book(b)})` |
| `list_for_user(user_id)` | `query(KeyConditionExpression=Key("PK").eq(pk_user(u)) & Key("SK").begins_with(BOOK_PREFIX))` |
| `delete(user_id, book_id)` | `delete_item(Key={...})` |
| `update_status` / `increment_chunks_done` | `update_item(Key={...}, ConditionExpression=Attr("PK").exists(), ...)` |
| `get(book_id, index)` | `get_item(Key={"PK": pk_book(b), "SK": sk_chunk(i)})` |
| `list_for_book(book_id)` | `query(KeyConditionExpression=Key("PK").eq(pk_book(b)) & Key("SK").begins_with(CHUNK_PREFIX))` |

Use `boto3.dynamodb.conditions.Key` / `Attr` and the **resource** API (`Table`), not the low-level
client — the resource auto-marshals Python types so no `{"S": ...}` appears anywhere.

Three things the implementer must get right:

1. **The `begins_with(...)` on `SK` is not decorative.** Phase 7 adds `BOOK#<id>/CHAT#<msgId>` under
   the same partition as chunks; a bare `Key("PK").eq(...)` in `list_for_book` would start returning
   chat messages the moment phase 7 lands. There's a test that seeds a foreign-SK item and asserts
   it's filtered out.
2. **Paginate.** `Query` caps at 1 MB per page; `list_for_book` on a real book *will* paginate
   because chunk text is bulky. Both list methods must loop on `LastEvaluatedKey` and feed it back
   as `ExclusiveStartKey`, accumulating items. Do not just return `response["Items"]`.
3. **Ordering.** `list_for_book` relies on the SK sort (ascending, DynamoDB default) — correct given
   zero-padding, no Python sort needed. `list_for_user` comes back ordered by book UUID, which is
   meaningless, so **sort in Python by `created_at` descending** (newest first, what the phase-6
   sidebar wants). A GSI or a `BOOK#<createdAt>#<id>` SK would both be over-engineering here: the
   latter breaks `get(user_id, book_id)` (you'd need the timestamp to build the key), and Phase 0
   deliberately deferred GSIs. A personal library is tens of books.

### 3.3 Item shape and the mapper layer

Attribute names follow **`IMPLEMENTATION_PLAN.md`'s data model verbatim (camelCase)**; Python domain
fields stay snake_case. The mapper is exactly what bridges the two — this is why the mapper module
exists rather than dumping `dataclasses.asdict()` into `put_item`.

Book item:
```
PK          "USER#<sub>"
SK          "BOOK#<bookId>"
entityType  "BOOK"            # added: phase 5's DynamoDB-stream consumer must tell item types apart
bookId      "<uuid4>"
userId      "<sub>"           # denormalized: saves parsing PK in stream records
title       str
status      "UPLOADED"
chunksTotal N
chunksDone  N
pageCount   N
createdAt   "2026-08-04T12:00:00.000000+00:00"
updatedAt   same
```

Chunk item:
```
PK          "BOOK#<bookId>"
SK          "CHUNK#000007"
entityType  "CHUNK"
bookId      "<bookId>"
userId      "<sub>"           # so phase 4/5 can build the book's Key={PK: USER#<sub>, ...}
chunkIndex  7
text        str
charStart   N
charEnd     N
audioKey    str | None
marksKey    str | None
status      "PENDING"
```

Mapper rules (`book_mapper.py`, `chunk_mapper.py` — mirroring jgautocar's `car_mapper.py`):

- **`item_to_*` uses `item.get(name, default)` for everything except the identity fields.** This is
  what makes later phases' new attributes additive with no migration. jgautocar does exactly this.
- **Coerce numbers with `int(...)`** — boto3's resource API returns every number as `Decimal`.
  jgautocar's `item_to_photo` carries this exact comment; keep it.
- `chunkIndex` is stored explicitly *and* derivable from the SK; `item_to_chunk` prefers the
  attribute and falls back to `chunk_index_from_sk(item["SK"])`.
- `audioKey`/`marksKey` absent ⇒ `None`. Write `None` (boto3 maps it to `NULL`); do not write `""`.
- **Reserved words.** `status`, `text`, `index`, `name`, `size` and `count` are all DynamoDB reserved
  words. Irrelevant for `put_item`/`get_item`, but **fatal in any `UpdateExpression` /
  `ProjectionExpression` / `ConditionExpression`** — so `update_status` must use
  `ExpressionAttributeNames={"#status": "status"}` and `SET #status = :status`. The ordinal is named
  `chunkIndex`, not `index`, precisely to sidestep this. Put a comment on the reserved-word aliasing;
  it is the single most likely thing to be silently gotten wrong.

---

## 4. Testing approach — moto, with a real-infra proof via the smoke test

`IMPLEMENTATION_PLAN.md` Phase 2 says "repository unit tests against LocalStack DynamoDB", but the
CI job that runs backend pytest (`ci.yml` → `test-backend`) has no LocalStack and, per Phase 0's
invariant, runs **credential-free with no services**. The `local-smoke` job is the one with
LocalStack, and it runs `make smoke` (the stdlib smoke test), not pytest.

**DECIDED: `moto`'s `mock_aws()` for the repository unit tests, plus real-DynamoDB coverage via
`local/smoke_test.py`'s new `GET /books` check** (which runs against LocalStack in `local-smoke`
*and* against real AWS in `deploy-pr`/`deploy-prod`). This is consistent with what Phase 0 and 1
actually established:
- `moto[dynamodb,s3,sqs]>=5.0` is already a dev dependency (`backend/pyproject.toml`), added in
  Phase 0 for exactly this.
- jgautocar's `tests/conftest.py` uses `mock_aws()` + `create_table(...)` — the pattern to copy.
- moto 5 fully supports `Query` with `begins_with`, `ADD` update expressions with `ReturnValues`,
  `ConditionExpression`, and `batch_writer()`. Nothing this phase needs is unsupported.

Interpretation to write down: "against LocalStack DynamoDB" means *against a real DynamoDB API
implementation rather than mocked boto3 calls* — moto satisfies the spirit at the unit level; the
letter is satisfied by `local-smoke` exercising the same code path against LocalStack over HTTP.

### The fixture — `backend/tests/contexts/library/conftest.py`

```python
@pytest.fixture
def dynamodb_table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(config.settings, "aws_endpoint_url", "")
    monkeypatch.setattr(config.settings, "table_name", "bookloud-test")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="bookloud-test",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource.Table("bookloud-test")
```

Four non-obvious points:
1. **`monkeypatch.setattr(settings, ...)`, not just `setenv`** — `src/config.py` builds `settings`
   as a module-level singleton at import time, so `setenv("TABLE_NAME", ...)` after import does
   nothing. The existing `tests/test_infrastructure_aws.py` already patches `aws_module.settings`
   this way; follow it.
2. **Clear `aws_endpoint_url` and `AWS_ENDPOINT_URL`** — since botocore 1.31, `AWS_ENDPOINT_URL` is
   a native boto3 env var. If a developer has it exported (or compose leaks it), boto3 would bypass
   moto and hit LocalStack. This must be explicitly neutralized.
3. **The key schema must be byte-identical to `infra/stacks/storage_stack.py`.** Add a comment
   pointing at that file, same lockstep convention as `config.py`.
4. **Build repositories inside the fixture/test, never at module import** — `library_table()` calls
   `boto3.resource(...)` and must run while `mock_aws()` is active.

Companion fixtures: `book_repo(dynamodb_table)`, `chunk_repo(dynamodb_table)` (constructed with the
table injected, jgautocar's `DynamoDbCarRepository(dynamodb=None)` pattern), `fixed_clock`
(returns a constant `datetime`), `seq_ids` (a deterministic `IdGenerator` stub), and
`seed_book(...)`/`seed_chunks(...)` helpers that go through the repositories (per the phase brief:
Phase 2 fixtures create chunks via the repository, **not** through any extraction pipeline).

---

## 5. API routes — DECIDED

**DECIDED: add read-only `GET /books` and `GET /books/{book_id}` in Phase 2. Do NOT add
`POST /books`.**

Why add the read routes:
- Phase 0's entire premise is that every phase's PR gets a real ephemeral deploy so nothing is
  validated against mocks until phase 8. With no route, Phase 2 ships code that is **never executed
  against real DynamoDB** — the IAM grant, the `TABLE_NAME` env var, the deployed table's key schema
  and the `Query` permission all go unproven until phase 3. Those are precisely the failures that
  are cheap to catch now and expensive to debug inside an SQS-triggered Lambda later. A `GET /books`
  returning `200 []` against the real table is a genuine end-to-end proof of all four.
- **Phase 6 (Reader UI) needs a book-list endpoint and no phase currently schedules one.**
  `IMPLEMENTATION_PLAN.md` phases 3-5 add upload, extraction, synthesis and `GET /books/{id}/status`
  — never a list route. Landing it here fills a real gap rather than inventing scope.
- It costs one small `interface/` layer that the context needs anyway.

Why **not** `POST /books`:
- Phase 3 owns book creation and creates books through the **presigned-POST** endpoint, whose
  contract is `{book, uploadUrl, fields}` — not a plain `POST /books`. A Phase-2 version would be
  rewritten immediately.
- Worse, a bare `POST /books` creates a `Book` row with no PDF behind it — a half-state nothing
  downstream handles, reachable by any authenticated user, that would need cleanup logic to exist
  for one phase only.

Instead, Phase 2 lands the **`CreateBook` use case** (application layer, fully unit-tested) with no
HTTP route. Phase 3's presign controller calls it. Creation logic is therefore built and tested now,
without committing to a URL contract that phase 3 owns.

Routes:

| route | auth | behaviour |
|---|---|---|
| `GET /books` | `Depends(get_current_user)` | `200` array of books for `user.sub`, newest first; `[]` when empty |
| `GET /books/{book_id}` | `Depends(get_current_user)` | `200` book; **`404`** if it doesn't exist *or* belongs to another user |

Both are covered by the existing `/{proxy+}` JWT authorizer — no `api_stack.py` change, no new
public path.

### 5.3 Wiring

`interface/dependencies.py`:
```python
def get_book_repository() -> BookRepository:  return DynamoDbBookRepository()
def get_chunk_repository() -> ChunkRepository: return DynamoDbChunkRepository()
def get_clock() -> Clock: return SystemClock()
def get_id_generator() -> IdGenerator: return Uuid4IdGenerator()
```
Providers construct **per request**, never at import — the FastAPI equivalent of jgautocar's
"resolved fresh from the DI container on every request rather than baked in at import time, since
tests reassign that container's contents per test". It's also what lets the moto fixture work without
`app.dependency_overrides`.

`interface/schemas.py` — `book_to_dict(book)` / `chunk_to_dict(chunk)` producing **camelCase** JSON
(`chunksTotal`, `createdAt`) to match both the DynamoDB attribute names and the TypeScript frontend.

`src/main.py` — one added line, `app.include_router(library_router)`, alongside the existing health
and auth routers; update the module docstring ("`library/` lands in phase 2" → done).

---

## 6. User isolation

`self_sign_up_enabled=False` means accounts are admin-provisioned, but that is not an isolation
mechanism — two provisioned readers must not see each other's books.

**Rule 1 — no bare `get(book_id)` exists anywhere in the codebase.** `BookRepository.get`,
`.delete`, `.update_status` and `.increment_chunks_done` all take `(user_id, book_id)` and build the
key as `{"PK": pk_user(user_id), "SK": sk_book(book_id)}`. Isolation is therefore enforced **by the
key**, not by a post-read `if book.user_id != user_id` check. This is strictly safer: a wrong
`user_id` addresses a different partition and simply finds nothing, so there is no code path — not
even a buggy one — that can return another user's book, and no ownership check that a future
refactor can accidentally drop. A `get(book_id)` that "trusts the caller already scoped it" is
rejected outright: it makes every future call site a place where isolation can be forgotten.

**Rule 2 — a missing book and someone else's book are indistinguishable: both are `404`.** The use
cases raise `NotFoundError` (status 404), never `ForbiddenError`/403, so the API doesn't leak the
existence of another user's book id. Assert the `404` explicitly in tests.

**Rule 3 — chunk access is authorized at the application layer, not the repository.** Chunk items are
keyed `PK=BOOK#<bookId>` with no user in the key, so `ChunkRepository` *structurally cannot* enforce
ownership. Handle it precisely:

- `ChunkRepository`'s module docstring states in the first paragraph: **"`book_id` must already have
  been authorized via `BookRepository.get(user_id, book_id)`. This port performs no access control."**
- Every use case that touches chunks goes through a single helper (jgautocar's `_load_car`
  equivalent):
  ```python
  def _load_owned_book(book_repository, user_id, book_id) -> Book:
      book = book_repository.get(user_id, book_id)
      if book is None:
          raise NotFoundError("Book not found")
      return book
  ```
  `ListBookChunks` / `DeleteBook` call it **before** touching the chunk repository, and there is a
  dedicated test asserting the chunk repository is **never called** when the book isn't the user's
  (using a spy fake, not a mocked table — the point is to prove the ordering).
- The `userId` attribute stored on chunk items is **not** an auth mechanism. It exists so phase 4/5's
  fan-in can construct the book's key (`{"PK": "USER#<sub>", "SK": "BOOK#<id>"}`) from a chunk item
  or a DynamoDB stream record without an extra lookup. Say this in a comment so no one later mistakes
  it for a security control and starts filtering on it.

Alternative considered and rejected: making `ChunkRepository` methods take a `Book` object instead of
a `book_id: str`, so it's structurally impossible to call them unauthorized. Rejected because it only
*moves* the trust boundary (a caller can construct a `Book`) while making phase 4's per-chunk
synthesize Lambda — which has a `book_id` from an SQS message and no reason to load the book —
awkward. The docstring + `_load_owned_book` + the ordering test is the honest, testable version.

---

## 7. Test plan

All under `backend/tests/contexts/library/` (packages with `__init__.py`, matching the existing
`tests/auth/` layout). The `--cov-fail-under=90` gate stays; everything below is trivially coverable.

**`test_domain.py`** — `Book.create` sets `UPLOADED`, zero counters, `created_at`/`updated_at` from
the injected clock, `id` from the injected generator; blank/non-str title → `ValidationError`; blank
`user_id` → `ValidationError`; `Chunk.create` defaults `PENDING` with `audio_key`/`marks_key` `None`;
negative index and `char_end < char_start` → `ValidationError`; `BookStatus.parse`/`ChunkStatus.parse`
accept every member and reject `"NOPE"`/`None`/`123`.

**`test_keys.py`** — exact string forms for all four helpers; `sk_chunk(7) == "CHUNK#000007"`;
**`sk_chunk(2) < sk_chunk(10)` lexicographically** (the ordering regression test); round-trip
`chunk_index_from_sk(sk_chunk(n)) == n` for `n in (0, 9, 10, 999999)`; `book_id_from_sk`.

**`test_mappers.py`** — `item_to_book(book_to_item(b)) == b` round-trip and the same for chunks;
`Decimal("3")` in `chunksTotal`/`charStart` comes back as `int`; an item missing every optional
attribute maps to sane defaults; extra unknown attributes are ignored; `audioKey`/`marksKey` absent
→ `None`; `entityType` is set on both; a chunk item lacking `chunkIndex` falls back to the SK.

**`test_book_repository.py`**
1. `save` → `get` returns an equal `Book`.
2. **Isolation:** user A saves; `get(user_b_sub, book_id)` → `None`.
3. `get` of a nonexistent id → `None`.
4. `list_for_user` on an empty library → `[]` (the explicit empty-library edge case).
5. `list_for_user` with two users seeded returns only the caller's books.
6. `list_for_user` returns newest-first by `created_at`.
7. `list_for_user` ignores a foreign-SK item seeded directly under `PK=USER#<sub>` (proves
   `begins_with("BOOK#")`).
8. `save` twice overwrites — title/status change round-trips.
9. `delete` removes it (`get` → `None`); deleting a nonexistent book is a silent no-op.
10. **Isolation:** `delete(user_b_sub, book_id)` does **not** delete user A's book.
11. `update_status(user, book, EXTRACTED)` round-trips and leaves `title`/`chunksTotal` untouched.
12. `update_status` on a nonexistent book → `NotFoundError`, and **no item is created** (the
    upsert-prevention test — assert the table item count is unchanged).
13. `increment_chunks_done` returns `1` then `2`; the stored value matches.
14. `increment_chunks_done` on a nonexistent book → `NotFoundError`, no item created.
15. **Pagination:** inject a stub table object whose `query` returns a `LastEvaluatedKey` on the
    first call and not the second; assert both pages' items are returned and `ExclusiveStartKey` was
    passed. (moto won't naturally produce a >1 MB page; test the loop directly.)

**`test_chunk_repository.py`**
1. `save` → `get(book_id, index)` round-trips including unicode and a multi-KB `text`.
2. `save_all` writes N; `list_for_book` returns them **in index order with indices spanning 2 and
   10** (the padding regression at the repository level).
3. `list_for_book` on a book with no chunks → `[]`.
4. `list_for_book` for book A never returns book B's chunks.
5. `list_for_book` ignores a foreign-SK item seeded under `PK=BOOK#<id>` (the phase-7 `CHAT#`
   guard).
6. `update_status(..., DONE, audio_key=..., marks_key=...)` round-trips and leaves `text`,
   `charStart`, `charEnd` untouched (proves the targeted `SET`, and exercises the `#status` reserved-
   word alias).
7. `update_status(..., DONE)` with no keys leaves existing `audioKey`/`marksKey` intact.
8. **`update_status` on a nonexistent chunk → `NotFoundError` and no item created** — the named edge
   case, and the upsert-prevention proof.
9. `delete_for_book` removes all chunks and returns the count; a second call returns `0`.
10. `delete_for_book` for book A leaves book B's chunks intact.

**`test_use_cases.py`** (fake in-memory repositories where the point is orchestration, moto where the
point is storage)
- `CreateBook` produces a `Book` with the injected id/clock and persists it via `save`.
- `ListBooks` returns newest-first for the caller only.
- `GetBook` for another user's book raises `NotFoundError` with `status_code == 404` (not 403).
- **`ListBookChunks` for another user's book raises `NotFoundError` *and the chunk repository is
  never called*** — spy fake, assert call count 0. This is the precise access-control test.
- `DeleteBook` deletes chunks **then** the book, in that order (assert the call order on the spies).
- `DeleteBook` for another user's book raises `NotFoundError` and deletes nothing.

**`test_controllers.py`** — uses `authed_client` + the moto fixture, no `dependency_overrides` needed
since providers construct per request
- `authed_client.get("/books")` on an empty table → `200 []`.
- Seeded books → `200` with the exact camelCase response shape, newest first.
- Anonymous `client.get("/books")` → `401` (the protected-route boundary, matching `tests/auth/`).
- `GET /books/{id}` for the caller's book → `200`; for another user's book → **`404`**; for a
  nonexistent id → `404`.

**`backend/tests/test_infrastructure_adapters.py`** — `SystemClock.now()` is timezone-aware UTC;
`Uuid4IdGenerator.new_id()` parses as a UUID and returns distinct values; `library_table()` uses
`settings.table_name`.

**`backend/tests/test_main.py`** (change) — assert the library router's routes are registered on `app`.

**Explicitly deferred to phase 3+** (state this in the plan so the implementer doesn't wander):
actual S3 upload / presigned POST, PDF text extraction and real chunk creation (PyMuPDF), the SQS
fan-out, `GET /books/{id}/status`, book status transitions past `UPLOADED`, and any Playwright spec.
Phase 2's chunks are created directly through `ChunkRepository.save_all` as fixtures.

---

## 8. Concrete file list

**`backend/src/`**
- `contexts/library/__init__.py` (new) — package marker.
- `contexts/library/domain/__init__.py` (new) — package marker.
- `contexts/library/domain/value_objects.py` (new) — `BookStatus`, `ChunkStatus` StrEnums with `parse()`.
- `contexts/library/domain/book.py` (new) — `Book` aggregate root + `create()`; docstring justifying the Book/Chunk aggregate split.
- `contexts/library/domain/chunk.py` (new) — `Chunk` entity + `create()`.
- `contexts/library/domain/repository.py` (new) — `BookRepository`, `ChunkRepository` Protocols + the save-vs-targeted-update rule.
- `contexts/library/application/__init__.py` (new) — package marker.
- `contexts/library/application/commands.py` (new) — `CreateBookCommand`.
- `contexts/library/application/use_cases.py` (new) — `CreateBook`, `GetBook`, `ListBooks`, `DeleteBook`, `ListBookChunks`, `_load_owned_book`.
- `contexts/library/infrastructure/__init__.py` (new) — package marker.
- `contexts/library/infrastructure/keys.py` (new) — PK/SK construction + parsing, zero-padded chunk index.
- `contexts/library/infrastructure/book_mapper.py` (new) — dict ↔ `Book`.
- `contexts/library/infrastructure/chunk_mapper.py` (new) — dict ↔ `Chunk`.
- `contexts/library/infrastructure/dynamodb_book_repository.py` (new) — `BookRepository` adapter (query + targeted updates + pagination).
- `contexts/library/infrastructure/dynamodb_chunk_repository.py` (new) — `ChunkRepository` adapter (batch writer + query + targeted updates).
- `contexts/library/interface/__init__.py` (new) — package marker.
- `contexts/library/interface/dependencies.py` (new) — per-request `Depends` providers (composition root).
- `contexts/library/interface/schemas.py` (new) — `Book`/`Chunk` → camelCase response dicts.
- `contexts/library/interface/controllers.py` (new) — `GET /books`, `GET /books/{book_id}`.
- `infrastructure/dynamodb.py` (new) — `library_table()` over `src/infrastructure/aws.py`'s `resource()`.
- `infrastructure/clock.py` (new) — `SystemClock` (UTC), satisfying `shared_kernel`'s `Clock`.
- `infrastructure/ids.py` (new) — `Uuid4IdGenerator`, satisfying `shared_kernel`'s `IdGenerator`.
- `shared_kernel/domain/errors.py` (change) — add `ValidationError(DomainError)` with `status_code = 400`.
- `main.py` (change) — include the library router; update the docstring's phase note.

**`backend/tests/`**
- `contexts/__init__.py`, `contexts/library/__init__.py` (new) — package markers.
- `contexts/library/conftest.py` (new) — moto `dynamodb_table`, repo fixtures, fixed clock/id stubs, `seed_book`/`seed_chunks`.
- `contexts/library/test_domain.py` (new) — entity creation + validation + status enums.
- `contexts/library/test_keys.py` (new) — key forms, zero-padding sort order, round-trips.
- `contexts/library/test_mappers.py` (new) — round-trip, `Decimal`→`int`, defaults, unknown attrs.
- `contexts/library/test_book_repository.py` (new) — CRUD, isolation, counters, condition checks, pagination.
- `contexts/library/test_chunk_repository.py` (new) — CRUD, ordering, scoping, `update_status`, cascade delete.
- `contexts/library/test_use_cases.py` (new) — orchestration + the access-control ordering test.
- `contexts/library/test_controllers.py` (new) — `GET /books` 200/401/404 shapes.
- `test_infrastructure_adapters.py` (new) — `SystemClock`, `Uuid4IdGenerator`, `library_table()`.
- `test_main.py` (change) — library router registered.

**`local/`**
- `smoke_test.py` (change) — `check_books_endpoint`: anonymous `GET /books` → 401; authenticated `GET /books` → 200 and a JSON array. Runs everywhere the existing auth checks run (local compose/LocalStack, PR env, prod).

**Root**
- `README.md` (change) — the `Library` context layout, the single-table key patterns, the "chunks are a separate aggregate" note.
- `IMPLEMENTATION_PLAN.md` (change) — tick Phase 2.
- `PLANS/phase-2.md` — this document.

**`infra/`** — **no changes required.** One added assertion in `infra/tests/test_synth.py` that the
API Lambda's role policy grants `dynamodb:Query` on the table, as a regression guard on the grant
Phase 2 now actually depends on.

---

## 9. Implementation sequence

1. `shared_kernel/domain/errors.py` (`ValidationError`) → `src/infrastructure/{clock,ids,dynamodb}.py` → their tests. Green.
2. `contexts/library/domain/*` → `test_domain.py`. Green, no AWS involved.
3. `contexts/library/infrastructure/keys.py` + mappers → `test_keys.py`, `test_mappers.py`. Green, still no AWS.
4. `conftest.py` moto fixture → `dynamodb_book_repository.py` → `test_book_repository.py`. Green. **Do not proceed until the condition-expression/upsert tests (12, 14) pass** — they're the ones that catch a wrong `UpdateExpression`.
5. `dynamodb_chunk_repository.py` → `test_chunk_repository.py`. Green.
6. `application/{commands,use_cases}.py` → `test_use_cases.py`. Green.
7. `interface/*` → `main.py` → `test_controllers.py`. `uv run pytest` green at ≥90 % coverage.
8. `local/smoke_test.py` + `make up && make smoke` locally against LocalStack — this is the step that
   proves the repository works against a real DynamoDB API, not just moto.
9. README + `IMPLEMENTATION_PLAN.md` checklist.

---

## 10. Decisions (all confirmed)

**Q1 — `BookStatus`/`ChunkStatus` membership: DECIDED — declare the full lifecycle now**
(`UPLOADED/EXTRACTED/READY/FAILED`, `PENDING/DONE/FAILED`), Phase 2 code only ever *writes*
`UPLOADED`/`PENDING`. Avoids a forward-compat parsing landmine when later phases write new statuses.

**Q2 — `increment_chunks_done`: DECIDED — in `BookRepository` now**, not deferred to phase 4. It's
the one mutation `save()` cannot express; keeping it in the port from day one prevents a second write
path being bolted onto the table later.

**Q3 — Chunk access-control shape: DECIDED — `ChunkRepository` methods take `book_id: str`**,
ownership enforced at the application layer via `_load_owned_book`, backed by an explicit
"chunk repo never called for another user's book" test.

**Q4 — moto vs LocalStack: DECIDED — moto for unit tests**, real DynamoDB coverage via
`local/smoke_test.py`'s new `GET /books` check (runs in `local-smoke`, PR envs, and prod).

**Q5 — API routes: DECIDED — `GET /books` + `GET /books/{id}` now, no `POST /books`.** `CreateBook`
use case lands fully tested; phase 3's presigned-POST controller calls it.

**Q6 — Schema additions: DECIDED — yes to all three**: `updatedAt` on books, `entityType` on both
item types, `userId` denormalized onto chunk items (not an auth mechanism — see §6 Rule 3).

**Q7 — API JSON casing: DECIDED — camelCase**, matching both the DynamoDB attribute names and the
TypeScript frontend.

**Q8 — Chunk SK zero-pad width: DECIDED — 6 digits** (`CHUNK#000007`). Immutable once any data
exists.

**Q9 — Id generation: DECIDED — UUID4 via the existing `IdGenerator` port** for books; chunks have
no generated id, identity is `(book_id, index)`.

**Q10 — Timestamps: DECIDED — ISO-8601 UTC strings**, not epoch millis.

**Q11 — New `ValidationError`: DECIDED — add `ValidationError(DomainError)` with `status_code = 400`**
to `shared_kernel/domain/errors.py`, already covered by `main.py`'s existing `DomainError` handler.
