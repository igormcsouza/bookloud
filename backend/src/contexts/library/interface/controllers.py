"""``GET /books``, ``GET /books/{book_id}`` -- read-only routes, deliberately
no ``POST /books`` (phase 3 owns creation via the presigned-POST endpoint;
see ``PLANS/phase-2.md`` §5). Both routes sit under the existing
``/{proxy+}`` JWT authorizer -- no ``api_stack.py`` change, no new public
path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth.dependencies import CurrentUser, get_current_user
from src.contexts.library.application.use_cases import GetBook, ListBooks
from src.contexts.library.domain.repository import BookRepository
from src.contexts.library.interface.dependencies import get_book_repository
from src.contexts.library.interface.schemas import book_to_dict

router = APIRouter(tags=["library"])


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
