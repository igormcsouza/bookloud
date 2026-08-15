import pytest
from botocore.exceptions import ClientError

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


@pytest.mark.parametrize("environment", ["local", "dev", "pr-1", "pr-42", "staging"])
def test_get_chat_model_never_stubs_regardless_of_secret_name(monkeypatch, environment):
    """The constraint-1 regression test: the environment gate is checked
    FIRST and UNCONDITIONALLY. An OPENAI_SECRET_NAME accidentally set on any
    non-prod stack still cannot produce a network call, because get_secret
    is never reached outside prod."""
    import src.contexts.library.interface.dependencies as deps_mod

    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "openai_secret_name", "bookloud/openai-api-key")

    def spy(*args, **kwargs):
        raise AssertionError("get_secret must never be called outside prod")

    monkeypatch.setattr(deps_mod, "get_secret", spy)

    model = get_chat_model()

    assert isinstance(model, StubChatModel)
    assert model._reason == ChatDisabledReason.NON_PROD


def test_get_chat_model_prod_with_absent_secret_propagates_client_error(monkeypatch):
    """§4.5 step 7 / §6.4 OQ-A: a prod stack whose OPENAI_SECRET_NAME points
    at a secret that does not exist (or that ChatFunction can't read) must
    raise before any model is constructed -- chat_app.py's exception handler
    is what turns this into a clean 503 before the first byte."""
    import src.contexts.library.interface.dependencies as deps_mod

    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "bookloud/does-not-exist")

    def fake_get_secret(name):
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetSecretValue",
        )

    monkeypatch.setattr(deps_mod, "get_secret", fake_get_secret)

    with pytest.raises(ClientError):
        get_chat_model()


def test_get_chat_model_prod_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "")
    model = get_chat_model()
    assert isinstance(model, StubChatModel)
    assert model._reason == ChatDisabledReason.NOT_CONFIGURED


def test_get_chat_model_prod_configured(monkeypatch):
    import src.contexts.library.interface.dependencies as deps_mod

    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "my-secret")
    calls = []

    def fake_get_secret(name):
        calls.append(name)
        return "sk-fake-key"

    monkeypatch.setattr(deps_mod, "get_secret", fake_get_secret)

    model = get_chat_model()

    assert isinstance(model, OpenAiChatModel)
    assert calls == ["my-secret"]  # eager: called while building the model
    assert model._api_key == "sk-fake-key"
