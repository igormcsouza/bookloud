"""``POST /books`` (create + presigned upload, PLANS/phase-3.md §4.1),
``POST /books/{id}/upload-url`` (re-issue, §4.2), ``GET /books``,
``GET /books/{id}``, ``GET /books/{id}/chunks`` (§4.3), and
``GET /books/{id}/status`` (PLANS/phase-5.md §8). All routes sit under the
existing ``/{proxy+}`` JWT authorizer -- no ``api_stack.py`` change, no new
public path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.auth.dependencies import CurrentUser, get_current_user
from src.contexts.library.application.commands import RequestBookUploadCommand
from src.contexts.library.application.use_cases import (
    GetBook,
    ListBookChunks,
    ListBooks,
    ReissueBookUpload,
    RequestBookUpload,
)
from src.contexts.library.domain.repository import BookRepository, ChunkRepository
from src.contexts.library.domain.storage import PdfStorage
from src.contexts.library.interface.dependencies import (
    get_book_repository,
    get_chunk_repository,
    get_clock,
    get_id_generator,
    get_pdf_storage,
)
from src.contexts.library.interface.schemas import (
    book_status_to_dict,
    book_to_dict,
    chunk_to_dict,
    upload_to_dict,
)
from src.shared_kernel.application.ports import Clock, IdGenerator

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
