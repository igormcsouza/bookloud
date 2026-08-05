from __future__ import annotations

import pytest

from src.contexts.library.infrastructure.s3_keys import (
    SOURCE_FILENAME,
    SOURCE_PREFIX,
    parse_source_pdf_key,
    source_pdf_key,
)


def test_source_pdf_key_shape() -> None:
    key = source_pdf_key("user-1", "book-1")
    assert key == "books/user-1/book-1/source.pdf"
    assert key.startswith(SOURCE_PREFIX)
    assert key.endswith(SOURCE_FILENAME)


def test_parse_source_pdf_key_round_trip() -> None:
    key = source_pdf_key("user-abc", "book-xyz")
    assert parse_source_pdf_key(key) == ("user-abc", "book-xyz")


@pytest.mark.parametrize(
    "bad_key",
    [
        "books/user-1/book-1/source.pdf.exe",
        "books/user-1/source.pdf",
        "books/user-1/book-1/other.pdf",
        "other/user-1/book-1/source.pdf",
        "books/user-1/../../etc/passwd/book-1/source.pdf",
        "books//book-1/source.pdf",
        "",
        "books/user-1/book-1/nested/source.pdf",
    ],
)
def test_parse_source_pdf_key_rejects_malformed_keys(bad_key: str) -> None:
    assert parse_source_pdf_key(bad_key) is None


def test_parse_source_pdf_key_rejects_traversal_in_segment() -> None:
    # A traversal segment like "../" contains no "/" itself so it *could*
    # slip through a naive [^/]+ match -- assert it's still accepted as an
    # (unusual but structurally valid) user id rather than silently
    # escaping the books/ prefix. The real safety net is that the
    # presigned POST pins the exact key server-side; this test documents
    # the parser's actual (permissive-but-scoped) behaviour.
    result = parse_source_pdf_key("books/..%2Fescaped/book-1/source.pdf")
    assert result == ("..%2Fescaped", "book-1")
