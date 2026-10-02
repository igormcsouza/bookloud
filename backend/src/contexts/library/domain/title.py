"""Port for inferring a clean book title from the PDF's opening text.
Uploads are titled with their filename, which is often messy; extraction
asks this port for a better one. ``None`` means "no confident answer" and
the book keeps its current title."""

from __future__ import annotations

from typing import Protocol


class TitleInferrer(Protocol):
    def infer(self, text: str) -> str | None: ...  # pragma: no cover
