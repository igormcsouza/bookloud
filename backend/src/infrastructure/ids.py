"""``IdGenerator`` implementation used outside of tests. Deterministic stub
generators live in ``tests/contexts/library/conftest.py``."""

from __future__ import annotations

from uuid import uuid4

class Uuid4IdGenerator:
    """Satisfies ``shared_kernel``'s ``IdGenerator`` Protocol structurally
    (no inheritance). Used for ``Book.id`` — chunks have no generated id,
    their identity is ``(book_id, index)``."""

    def new_id(self) -> str:
        return str(uuid4())
