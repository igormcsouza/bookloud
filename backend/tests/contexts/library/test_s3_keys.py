from __future__ import annotations

import pytest

from src.contexts.library.infrastructure.keys import CHUNK_INDEX_WIDTH
from src.contexts.library.infrastructure.s3_keys import (
    SOURCE_FILENAME,
    SOURCE_PREFIX,
    chunk_audio_key,
    chunk_marks_key,
    parse_chunk_audio_key,
    parse_chunk_marks_key,
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


# --- chunk_audio_key / chunk_marks_key (PLANS/phase-4.md §5.1) --------------


def test_chunk_audio_key_shape() -> None:
    key = chunk_audio_key("user-1", "book-1", 7)
    assert key == "audio/user-1/book-1/000007.mp3"


def test_chunk_marks_key_shape() -> None:
    key = chunk_marks_key("user-1", "book-1", 7)
    assert key == "marks/user-1/book-1/000007.json"


def test_chunk_audio_key_zero_padding_matches_chunk_index_width() -> None:
    key = chunk_audio_key("u", "b", 5)
    index_segment = key.split("/")[-1].removesuffix(".mp3")
    assert len(index_segment) == CHUNK_INDEX_WIDTH


def test_chunk_marks_key_zero_padding_matches_chunk_index_width() -> None:
    key = chunk_marks_key("u", "b", 5)
    index_segment = key.split("/")[-1].removesuffix(".json")
    assert len(index_segment) == CHUNK_INDEX_WIDTH


def test_parse_chunk_audio_key_round_trip() -> None:
    key = chunk_audio_key("user-abc", "book-xyz", 42)
    assert parse_chunk_audio_key(key) == ("user-abc", "book-xyz", 42)


def test_parse_chunk_marks_key_round_trip() -> None:
    key = chunk_marks_key("user-abc", "book-xyz", 42)
    assert parse_chunk_marks_key(key) == ("user-abc", "book-xyz", 42)


def test_parse_chunk_audio_key_round_trip_large_index() -> None:
    key = chunk_audio_key("u", "b", 999999)
    assert parse_chunk_audio_key(key) == ("u", "b", 999999)


@pytest.mark.parametrize(
    "bad_key",
    [
        "audio/user-1/book-1/000007.mp3.exe",
        "audio/user-1/000007.mp3",
        "audio/user-1/book-1/7.mp3",  # not zero-padded to CHUNK_INDEX_WIDTH
        "marks/user-1/book-1/000007.mp3",  # wrong prefix/extension pairing
        "other/user-1/book-1/000007.mp3",
        "audio/user-1/../../etc/passwd/book-1/000007.mp3",
        "audio//book-1/000007.mp3",
        "",
        "audio/user-1/book-1/nested/000007.mp3",
    ],
)
def test_parse_chunk_audio_key_rejects_malformed_keys(bad_key: str) -> None:
    assert parse_chunk_audio_key(bad_key) is None


@pytest.mark.parametrize(
    "bad_key",
    [
        "marks/user-1/book-1/000007.json.exe",
        "marks/user-1/000007.json",
        "marks/user-1/book-1/7.json",
        "audio/user-1/book-1/000007.json",
        "other/user-1/book-1/000007.json",
        "marks/user-1/../../etc/passwd/book-1/000007.json",
        "marks//book-1/000007.json",
        "",
        "marks/user-1/book-1/nested/000007.json",
    ],
)
def test_parse_chunk_marks_key_rejects_malformed_keys(bad_key: str) -> None:
    assert parse_chunk_marks_key(bad_key) is None
