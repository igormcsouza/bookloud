"""``POST /books`` (create + presigned upload, PLANS/phase-3.md §4.1),
``POST /books/{id}/upload-url`` (re-issue, §4.2), ``GET /books``,
``GET /books/{id}``, ``GET /books/{id}/chunks`` (§4.3),
``GET /books/{id}/status`` (PLANS/phase-5.md §8), and phase 6's reader
surface: ``GET /books/{id}/audio``, ``GET /books/{id}/chunks/{n}/audio``,
``GET /books/{id}/manifest``, ``GET /books/{id}/chunks/{n}/marks`` and
``POST /books/{id}/resynthesize`` (PLANS/phase-6.md §4.3/§5).

Every route sits under the existing ``/{proxy+}`` JWT authorizer -- no new
public path. (``api_stack.py`` *does* change this phase, but only to grant
``sqs:SendMessage`` on two more queues for ``/resynthesize``, and to collapse
the enumerated per-method routes to ``ANY``; the authorizer split is
untouched.)

The one asymmetry worth naming: the presigned URL ``GET /books/{id}/audio``
returns is the **only** thing in the whole app that authenticates by
signature rather than by ``Authorization`` header. That is forced, not
chosen -- ``<audio src=...>`` cannot carry a header, and proxying ~240 MB
through a 6 MB Lambda response is arithmetic, not preference (§3.1/§3.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from src.auth.dependencies import CurrentUser, get_current_user
from src.contexts.library.application.commands import RequestBookUploadCommand
from src.contexts.library.application.delivery import (
    GetBookAudioUrl,
    GetBookManifest,
    GetChunkAudioUrl,
    GetChunkMarks,
)
from src.contexts.library.application.resynthesis import ResynthesizeBook
from src.contexts.library.application.use_cases import (
    GetBook,
    ListBookChunks,
    ListBooks,
    ReissueBookUpload,
    RequestBookUpload,
)
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.stitching import StitchQueue
from src.contexts.library.domain.storage import AudioDelivery, ObjectStorage, PdfStorage
from src.contexts.library.domain.synthesis import SynthesisQueue
from src.contexts.library.domain.chat import ChatModel
from src.contexts.library.domain.repository import ChatQuotaRepository
from src.contexts.library.interface.dependencies import (
    get_audio_delivery,
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_id_generator,
    get_marks_storage,
    get_pdf_storage,
    get_stitch_queue,
    get_synthesis_queue,
    get_list_book_chat,
    get_clear_book_chat,
    get_chat_model,
    get_chat_quota_repository,
)
from src.config import settings
from src.contexts.library.interface.schemas import (
    audio_url_to_dict,
    book_status_to_dict,
    book_to_dict,
    chunk_to_dict,
    resynthesis_to_dict,
    upload_to_dict,
)
from src.shared_kernel.application.ports import Clock, IdGenerator

# Both documents are immutable for a given book once written (every S3 key is
# a pure function of the ids, and a re-stitch overwrites in place), so a
# private browser cache is safe. The manifest gets the shorter window because
# a /resynthesize can legitimately replace it; per-chunk marks cannot change
# without the chunk index changing.
_MANIFEST_CACHE_CONTROL = "private, max-age=3600"
_MARKS_CACHE_CONTROL = "private, max-age=86400"
_JSON_MEDIA_TYPE = "application/json"

router = APIRouter(tags=["library"])


class CreateBookRequest(BaseModel):
    # Permissive: FastAPI/pydantic never rejects this at the wire level --
    # the *domain* validates so a blank title comes back as the 400
    # "Title is required" from Book.create, not FastAPI's generic 422.
    title: str = ""


@router.post("/books", status_code=201)
def create_book(
    payload: CreateBookRequest,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    pdf_storage: PdfStorage = Depends(get_pdf_storage),
    clock: Clock = Depends(get_clock),
    id_generator: IdGenerator = Depends(get_id_generator),
) -> dict:
    use_case = RequestBookUpload(book_repository, pdf_storage, clock, id_generator)
    result = use_case.execute(RequestBookUploadCommand(user_id=user.sub, title=payload.title))
    return {"book": book_to_dict(result.book), "upload": upload_to_dict(result.upload)}


@router.post("/books/{book_id}/upload-url")
def reissue_book_upload(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    pdf_storage: PdfStorage = Depends(get_pdf_storage),
) -> dict:
    use_case = ReissueBookUpload(book_repository, pdf_storage)
    upload = use_case.execute(user.sub, book_id)
    return upload_to_dict(upload)


@router.get("/books")
def list_books(
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
) -> list[dict]:
    books = ListBooks(book_repository).execute(user.sub)
    return [book_to_dict(book) for book in books]


@router.get("/books/{book_id}")
def get_book(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
) -> dict:
    book = GetBook(book_repository).execute(user.sub, book_id)
    return book_to_dict(book)


@router.get("/books/{book_id}/status")
def get_book_status(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
) -> dict:
    """The phase-6 poll endpoint (PLANS/phase-5.md §8). Performs **no extra
    I/O** -- one ``GetItem`` via the same ``GetBook`` use case as
    ``GET /books/{id}``, so another user's book is a 404, never a 403."""
    book = GetBook(book_repository).execute(user.sub, book_id)
    return book_status_to_dict(book)


@router.get("/books/{book_id}/chunks")
def list_book_chunks(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    chunk_repository: ChunkRepository = Depends(get_chunk_repository),
) -> list[dict]:
    chunks = ListBookChunks(book_repository, chunk_repository).execute(user.sub, book_id)
    return [chunk_to_dict(chunk) for chunk in chunks]


# --- phase 6: audio delivery (PLANS/phase-6.md §4.3) ------------------------


@router.get("/books/{book_id}/audio")
def get_book_audio(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    audio_delivery: AudioDelivery = Depends(get_audio_delivery),
) -> dict:
    """Mints a presigned S3 GET for the stitched ``book.mp3`` (§3).

    A **409** when the book has no ``audioKey`` -- the everyday state of
    every non-prod environment (PLANS/phase-4.md §0), so it is a documented
    outcome the reader renders as "you can still read the book", not an
    error path. A foreign or missing book is a 404, never a 403.
    """
    use_case = GetBookAudioUrl(book_repository, audio_delivery)
    return audio_url_to_dict(use_case.execute(user.sub, book_id))


@router.get("/books/{book_id}/chunks/{index}/audio")
def get_chunk_audio(
    book_id: str,
    index: int,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    chunk_repository: ChunkRepository = Depends(get_chunk_repository),
    audio_delivery: AudioDelivery = Depends(get_audio_delivery),
) -> dict:
    """Backend-only this phase (PLANS/phase-6.md OQ-1). It is what makes
    phase-5 §7.2's invariant 3 -- a book whose concatenation failed is still
    playable chunk by chunk -- a real capability rather than a claim; the
    second playback engine that would consume it in the UI is deferred until
    prod actually produces a ``STITCH_FAILED`` book."""
    use_case = GetChunkAudioUrl(book_repository, chunk_repository, audio_delivery)
    return audio_url_to_dict(use_case.execute(user.sub, book_id, index))


@router.get("/books/{book_id}/manifest")
def get_book_manifest(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    marks_storage: ObjectStorage = Depends(get_marks_storage),
) -> Response:
    """Returns ``marks/<u>/<b>/book.json`` **verbatim** -- no
    re-serialization. The document is already the contract
    (PLANS/phase-5.md §7.2 calls it one in so many words); re-shaping it here
    would create a second place for the schema to drift from the stitcher
    that writes it."""
    use_case = GetBookManifest(book_repository, marks_storage)
    payload = use_case.execute(user.sub, book_id)
    return Response(
        content=payload,
        media_type=_JSON_MEDIA_TYPE,
        headers={"Cache-Control": _MANIFEST_CACHE_CONTROL},
    )


@router.get("/books/{book_id}/chunks/{index}/marks")
def get_chunk_marks(
    book_id: str,
    index: int,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    chunk_repository: ChunkRepository = Depends(get_chunk_repository),
    marks_storage: ObjectStorage = Depends(get_marks_storage),
) -> Response:
    """Returns the per-chunk marks document verbatim. A **404 is the normal
    case** for every chunk in a PR environment (no chunk ever synthesized),
    which is exactly why the client negatively caches it rather than
    retrying."""
    use_case = GetChunkMarks(book_repository, chunk_repository, marks_storage)
    payload = use_case.execute(user.sub, book_id, index)
    return Response(
        content=payload,
        media_type=_JSON_MEDIA_TYPE,
        headers={"Cache-Control": _MARKS_CACHE_CONTROL},
    )


# --- phase 6: resynthesis (PLANS/phase-6.md §5) -----------------------------


@router.post("/books/{book_id}/resynthesize", status_code=202)
def resynthesize_book(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    book_repository: BookRepository = Depends(get_book_repository),
    chunk_repository: ChunkRepository = Depends(get_chunk_repository),
    synthesis_queue: SynthesisQueue = Depends(get_synthesis_queue),
    stitch_queue: StitchQueue = Depends(get_stitch_queue),
    clock: Clock = Depends(get_clock),
) -> dict:
    """``202``, not ``200``: nothing has been resynthesized yet -- N messages
    have been published. ``409`` on any status but ``PARTIAL``; the
    conditional book update is the exactly-once gate, so a double-clicked
    button produces one 202 and one 409 (PLANS/phase-6.md §5.2)."""
    use_case = ResynthesizeBook(
        book_repository, chunk_repository, synthesis_queue, stitch_queue, clock
    )
    result = use_case.execute(user.sub, book_id)
    # Reloaded, not patched in memory: the client drops this straight into
    # its poll state, so it must be what the table actually says.
    book = GetBook(book_repository).execute(user.sub, book_id)
    return resynthesis_to_dict(result, book)


@router.get("/books/{book_id}/chat")
def list_book_chat(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    use_case = Depends(get_list_book_chat),
    model: ChatModel = Depends(get_chat_model),
    chat_quota_repository: ChatQuotaRepository = Depends(get_chat_quota_repository),
    clock: Clock = Depends(get_clock),
) -> dict:
    """PLANS/phase-7.md §5.3: ``chat.enabled``/``reason`` here is the signal
    the sidebar disables the composer on *before* a wasted turn, so it comes
    from the same ``get_chat_model()`` gate the streaming endpoint uses --
    never hardcoded."""
    from src.shared_kernel.domain.errors import ConflictError
    from src.contexts.library.interface.schemas import chat_list_to_dict

    today = clock.now().strftime("%Y-%m-%d")
    used_today = chat_quota_repository.get_count(user.sub, today)
    enabled = model.enabled
    reason = model.reason.value if model.reason else None
    reported_model = model.name if enabled else None

    try:
        messages = use_case.execute(user_id=user.sub, book_id=book_id)
        return chat_list_to_dict(
            messages,
            enabled=enabled,
            reason=reason,
            model=reported_model,
            daily_limit=settings.chat_daily_limit,
            used_today=used_today,
        )
    except ConflictError as e:
        if "NO_TEXT" in str(e):
            return chat_list_to_dict(
                [],
                enabled=False,
                reason="NO_TEXT",
                model=None,
                daily_limit=settings.chat_daily_limit,
                used_today=used_today,
            )
        raise

@router.delete("/books/{book_id}/chat")
def clear_book_chat(
    book_id: str,
    user: CurrentUser = Depends(get_current_user),
    use_case = Depends(get_clear_book_chat),
) -> dict:
    deleted = use_case.execute(user_id=user.sub, book_id=book_id)
    return {"deleted": deleted}
