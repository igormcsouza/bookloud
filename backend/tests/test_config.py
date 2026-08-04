from __future__ import annotations

import pytest

from src.config import Settings


@pytest.mark.usefixtures("clean_env")
def test_defaults() -> None:
    settings = Settings()
    assert settings.environment == "local"
    assert settings.git_sha == "local"
    assert settings.table_name == ""
    assert settings.pdf_bucket == ""
    assert settings.audio_bucket == ""
    assert settings.marks_bucket == ""
    assert settings.extract_queue_url == ""
    assert settings.log_level == "INFO"
    assert settings.aws_endpoint_url == ""


@pytest.mark.usefixtures("clean_env")
def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "pr-42")
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("TABLE_NAME", "bookloud-pr-42")
    monkeypatch.setenv("PDF_BUCKET", "bookloud-pr-42-pdfs-xyz")
    monkeypatch.setenv("AUDIO_BUCKET", "bookloud-pr-42-audio-xyz")
    monkeypatch.setenv("MARKS_BUCKET", "bookloud-pr-42-marks-xyz")
    monkeypatch.setenv(
        "EXTRACT_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123456789012/bookloud-pr-42-extract",
    )
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localstack:4566")

    settings = Settings()

    assert settings.environment == "pr-42"
    assert settings.git_sha == "abc1234"
    assert settings.table_name == "bookloud-pr-42"
    assert settings.pdf_bucket == "bookloud-pr-42-pdfs-xyz"
    assert settings.audio_bucket == "bookloud-pr-42-audio-xyz"
    assert settings.marks_bucket == "bookloud-pr-42-marks-xyz"
    assert settings.extract_queue_url.endswith("bookloud-pr-42-extract")
    assert settings.log_level == "DEBUG"
    assert settings.aws_endpoint_url == "http://localstack:4566"


@pytest.mark.usefixtures("clean_env")
def test_env_names_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("environment", "lowercase-env")
    settings = Settings()
    assert settings.environment == "lowercase-env"
