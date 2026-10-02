import pytest

import src.contexts.library.interface.dependencies as deps_mod
from src.config import settings
from src.contexts.library.infrastructure.openai_title_inferrer import OpenAiTitleInferrer


@pytest.mark.parametrize("environment", ["local", "dev", "pr-1", "staging"])
def test_title_inferrer_off_outside_prod(monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "openai_secret_name", "/bookloud/openai-api-key")
    monkeypatch.setattr(deps_mod, "get_parameter", lambda *_: pytest.fail("key read outside prod"))
    assert deps_mod.get_title_inferrer() is None


def test_title_inferrer_off_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "")
    assert deps_mod.get_title_inferrer() is None


def test_title_inferrer_prod_builds_openai_inferrer(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "/bookloud/openai-api-key")
    monkeypatch.setattr(deps_mod, "get_parameter", lambda *_: "sk-test")
    assert isinstance(deps_mod.get_title_inferrer(), OpenAiTitleInferrer)


def test_title_inferrer_unreadable_secret_degrades_to_none(monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "openai_secret_name", "/bookloud/openai-api-key")

    def boom(*_):
        raise RuntimeError("no such secret")

    monkeypatch.setattr(deps_mod, "get_parameter", boom)
    assert deps_mod.get_title_inferrer() is None
