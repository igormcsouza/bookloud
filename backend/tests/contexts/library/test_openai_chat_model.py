import json
import urllib.request
import pytest

from src.contexts.library.domain.chat import ChatContext, ContextChunk
from src.contexts.library.infrastructure.openai_chat_model import OpenAiChatModel
from src.shared_kernel.domain.errors import ChatStreamError


class MockResponse:
    def __init__(self, lines, should_fail_mid_stream=False):
        self.lines = lines
        self.should_fail_mid_stream = should_fail_mid_stream
        
    def __enter__(self):
        return self
        
    def __exit__(self, *args):
        pass
        
    def __iter__(self):
        for line in self.lines:
            yield line
        if self.should_fail_mid_stream:
            raise ConnectionError("Dropped connection")


@pytest.fixture
def mock_urlopen(monkeypatch):
    def _mock(response_obj):
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: response_obj)
    return _mock


@pytest.fixture
def mock_secret(monkeypatch):
    import src.contexts.library.infrastructure.secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "get_secret", lambda name: "fake_key")


@pytest.fixture
def context():
    return ChatContext(
        book_id="b1", book_title="T", chunks_total=1,
        anchor_chunk=0, chunks=(ContextChunk(0, "txt", 0, 0, True),),
        history=(), question="Q"
    )


def test_openai_chat_model_success(mock_urlopen, mock_secret, context):
    model = OpenAiChatModel("sec", "gpt", 100)
    lines = [
        b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n',
        b'data: {"choices": [{"delta": {"content": " World"}}]}\n',
        b'data: [DONE]\n'
    ]
    mock_urlopen(MockResponse(lines))
    
    deltas = list(model.stream(context))
    assert len(deltas) == 2
    assert deltas[0].text == "Hello"
    assert deltas[1].text == " World"


def test_openai_chat_model_connect_error(monkeypatch, mock_secret, context):
    model = OpenAiChatModel("sec", "gpt", 100)
    def fail(*args, **kwargs):
        raise ValueError("Cannot connect")
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    
    with pytest.raises(ChatStreamError) as exc:
        list(model.stream(context))
    assert exc.value.code == "CONNECT_ERROR"


def test_openai_chat_model_mid_stream_error(mock_urlopen, mock_secret, context):
    model = OpenAiChatModel("sec", "gpt", 100)
    lines = [
        b'data: {"choices": [{"delta": {"content": "Hello"}}]}\n',
    ]
    mock_urlopen(MockResponse(lines, should_fail_mid_stream=True))
    
    stream = model.stream(context)
    
    # First token works
    d1 = next(stream)
    assert d1.text == "Hello"
    
    # Next raises
    with pytest.raises(ChatStreamError) as exc:
        next(stream)
    assert exc.value.code == "STREAM_ERROR"
