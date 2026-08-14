import pytest

from src.config import settings
from src.contexts.library.interface.dependencies import get_chat_model
from src.contexts.library.infrastructure.stub_chat_model import StubChatModel
from src.contexts.library.infrastructure.openai_chat_model import OpenAiChatModel
from src.contexts.library.domain.chat import ChatDisabledReason


def test_get_chat_model_non_prod(monkeypatch):
    monkeypatch.setattr(settings, "environment", "local")
    model = get_chat_model()
    assert isinstance(model, StubChatModel)
    assert model._reason == ChatDisabledReason.NON_PROD


def test_get_chat_model_prod_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "")
    model = get_chat_model()
    assert isinstance(model, StubChatModel)
    assert model._reason == ChatDisabledReason.NOT_CONFIGURED


def test_get_chat_model_prod_configured(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "my-secret")
    model = get_chat_model()
    assert isinstance(model, OpenAiChatModel)
    assert model.secret_name == "my-secret"
