import socket
import pytest

from src.contexts.library.domain.chat import ChatContext, ContextChunk, ChatDisabledReason
from src.contexts.library.infrastructure.stub_chat_model import StubChatModel, PROD_NOT_CONFIGURED_MESSAGE


@pytest.fixture(autouse=True)
def no_socket(monkeypatch):
    """Socket guard to ensure StubChatModel never touches the network."""
    def block_socket(*args, **kwargs):
        raise RuntimeError("Network call blocked in stub test")
    monkeypatch.setattr(socket, "socket", block_socket)


def test_stub_chat_model_not_configured():
    model = StubChatModel(ChatDisabledReason.NOT_CONFIGURED)
    ctx = ChatContext(
        book_id="b1", book_title="Title", chunks_total=1,
        anchor_chunk=0, chunks=(), history=(), question="q?"
    )
    
    deltas = list(model.stream(ctx))
    full_text = "".join(d.text for d in deltas)
    
    assert full_text == PROD_NOT_CONFIGURED_MESSAGE
    # check it yields in chunks
    assert len(deltas) > 1


def test_stub_chat_model_non_prod():
    model = StubChatModel(ChatDisabledReason.NON_PROD)
    ctx = ChatContext(
        book_id="b1", book_title="Moby-Dick", chunks_total=1,
        anchor_chunk=12, chunks=(
            ContextChunk(index=12, text="Call me Ishmael.", page_start=0, page_end=0, is_anchor=True),
        ), history=(), question="Why?"
    )
    
    deltas = list(model.stream(ctx))
    full_text = "".join(d.text for d in deltas)
    
    assert "[Chat is turned off in this environment.]" in full_text
    assert "You asked: Why?" in full_text
    assert "I can see 1 sections of “Moby-Dick” around section 12" in full_text
    assert "which begins: “Call me Ishmael.…”" in full_text
    assert len(deltas) > 1
