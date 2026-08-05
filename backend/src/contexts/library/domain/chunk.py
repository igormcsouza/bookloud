"""The ``Chunk`` entity — its own aggregate, not embedded in ``Book``. See
``book.py``'s module docstring for why.

No ``created_at``: the spec's chunk row has none, and the zero-padded SK
already orders chunks correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.contexts.library.domain.value_objects import ChunkStatus
from src.shared_kernel.domain.errors import ValidationError


@dataclass
class Chunk:
    book_id: str
    index: int  # ordinal from extraction; identity is (book_id, index)
    user_id: str  # the owning book's sub -- NOT an auth mechanism, see §6
    text: str
    char_start: int
    char_end: int
    audio_key: str | None
    marks_key: str | None
    status: ChunkStatus
    page_start: int = 0  # 1-based, inclusive; 0 default keeps phase-2 fixtures compiling
    page_end: int = 0  # 1-based, inclusive

    @classmethod
    def create(
        cls,
        *,
        book_id: str,
        user_id: str,
        index: int,
        text: str,
        char_start: int,
        char_end: int,
        page_start: int = 0,
        page_end: int = 0,
    ) -> Chunk:
        if index < 0:
            raise ValidationError("Chunk index must be non-negative")
        if char_end < char_start:
            raise ValidationError("char_end must be >= char_start")
        if page_end < page_start:
            raise ValidationError("page_end must be >= page_start")

        return cls(
            book_id=book_id,
            index=index,
            user_id=user_id,
            text=text,
            char_start=char_start,
            char_end=char_end,
            audio_key=None,
            marks_key=None,
            status=ChunkStatus.PENDING,
            page_start=page_start,
            page_end=page_end,
        )
