import pytest

from src.contexts.library.domain.chat import ChatContext, ContextChunk, ChatMessage, ChatRole
from src.contexts.library.infrastructure.openai_prompt import render_messages


def test_render_messages_basic():
    ctx = ChatContext(
        book_id="b1",
        book_title="Moby-Dick",
        chunks_total=330,
        anchor_chunk=12,
        chunks=(
            ContextChunk(index=10, text="Call me Ishmael.", page_start=41, page_end=43, is_anchor=False),
            ContextChunk(index=12, text="The whale.", page_start=45, page_end=47, is_anchor=True),
        ),
        history=(
            ChatMessage(book_id="b1", message_id="1", user_id="u", role=ChatRole.USER, content="Who?", anchored_chunk=0, created_at=""),
            ChatMessage(book_id="b1", message_id="2", user_id="u", role=ChatRole.ASSISTANT, content="Ishmael.", anchored_chunk=0, created_at=""),
        ),
        question="Why?"
    )
    
    msgs = render_messages(ctx)
    assert len(msgs) == 4
    
    # Check system message
    assert msgs[0]["role"] == "system"
    sys_content = msgs[0]["content"]
    assert 'title="Moby-Dick"' in sys_content
    assert 'sections="10-12" of="330"' in sys_content
    assert '<section index="10" pages="41-43">Call me Ishmael.</section>' in sys_content
    assert '<section index="12" pages="45-47" reading-here="true">The whale.</section>' in sys_content
    
    # Check history
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "Who?"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "Ishmael."
    
    # Check question
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"] == "Why?"


def test_render_messages_xml_escaping():
    ctx = ChatContext(
        book_id="b1",
        book_title='Book <"title">&',
        chunks_total=1,
        anchor_chunk=0,
        chunks=(
            ContextChunk(index=0, text="Text with < > &.", page_start=0, page_end=0, is_anchor=True),
        ),
        history=(),
        question="q"
    )
    
    msgs = render_messages(ctx)
    sys_content = msgs[0]["content"]
    assert 'title="Book &lt;&quot;title&quot;&gt;&amp;"' in sys_content
    assert '>Text with &lt; &gt; &amp;.</section>' in sys_content


def test_render_messages_single_section():
    ctx = ChatContext(
        book_id="b1",
        book_title="A",
        chunks_total=5,
        anchor_chunk=2,
        chunks=(
            ContextChunk(index=2, text="text", page_start=0, page_end=0, is_anchor=True),
        ),
        history=(),
        question="q"
    )
    msgs = render_messages(ctx)
    assert 'sections="2" of="5"' in msgs[0]["content"]


def test_render_messages_no_leak():
    # Test that there is no text outside the window in the system prompt.
    ctx = ChatContext(
        book_id="b1",
        book_title="A",
        chunks_total=1,
        anchor_chunk=0,
        chunks=(),
        history=(),
        question="q"
    )
    msgs = render_messages(ctx)
    sys_content = msgs[0]["content"]
    assert 'sections="0" of="1"' in sys_content
    assert "<section" not in sys_content

def test_render_messages_no_pages():
    ctx = ChatContext(
        book_id="b1",
        book_title="A",
        chunks_total=1,
        anchor_chunk=0,
        chunks=(
            ContextChunk(index=0, text="text", page_start=0, page_end=0, is_anchor=True),
        ),
        history=(),
        question="q"
    )
    msgs = render_messages(ctx)
    sys_content = msgs[0]["content"]
    assert 'pages=' not in sys_content
