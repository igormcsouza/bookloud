from __future__ import annotations

import pytest

from src.contexts.library.infrastructure.keys import (
    BOOK_PREFIX,
    CHUNK_PREFIX,
    USER_PREFIX,
    book_id_from_sk,
    chunk_index_from_sk,
    pk_book,
    pk_user,
    sk_book,
    sk_chunk,
)


def test_pk_user() -> None:
    assert pk_user("abc-123") == f"{USER_PREFIX}abc-123"


def test_sk_book() -> None:
    assert sk_book("book-1") == f"{BOOK_PREFIX}book-1"


def test_pk_book() -> None:
    assert pk_book("book-1") == f"{BOOK_PREFIX}book-1"


def test_sk_chunk_zero_pads_to_width_six() -> None:
    assert sk_chunk(7) == "CHUNK#000007"


def test_sk_chunk_ordering_regression() -> None:
    """The load-bearing regression test: zero-padding means CHUNK#2 sorts
    before CHUNK#10 lexicographically, which is what a bare (unpadded)
    string SK would get wrong."""
    assert sk_chunk(2) < sk_chunk(10)


@pytest.mark.parametrize("n", [0, 9, 10, 999999])
def test_chunk_index_round_trips(n: int) -> None:
    assert chunk_index_from_sk(sk_chunk(n)) == n


def test_book_id_from_sk() -> None:
    assert book_id_from_sk(sk_book("book-42")) == "book-42"


def test_chunk_prefix_constant() -> None:
    assert CHUNK_PREFIX == "CHUNK#"
