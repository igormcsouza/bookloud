"""Env-driven application settings.

This is the single source of truth for the backend's environment variable
*names*. ``infra/stacks/config.py`` sets these same names on the Lambda
(and docker-compose sets them for local dev) — the two files must stay in
lockstep since these are separately deployed projects that cannot share an
import. If you rename a field here, rename the matching env var in
``infra/stacks/config.py`` too.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    environment: str = "local"
    git_sha: str = "local"

    table_name: str = ""
    pdf_bucket: str = ""
    audio_bucket: str = ""
    marks_bucket: str = ""
    extract_queue_url: str = ""

    log_level: str = "INFO"

    # Empty in real AWS (boto3 resolves the real endpoints); set to
    # http://localstack:4566 (or http://localhost:4566 outside compose) for
    # local dev so every AWS client talks to LocalStack instead.
    aws_endpoint_url: str = ""

    # PLANS/phase-3.md §9.4: generate_presigned_post builds URLs from the
    # signing client's endpoint, so inside compose that would be
    # http://localstack:4566/... -- unreachable from the browser/host. Set to
    # http://localhost:4566 in compose so presigned upload URLs are
    # host-reachable; empty (falls back to aws_endpoint_url, i.e. None in
    # real AWS) everywhere else. Never set by CDK.
    s3_public_endpoint_url: str = ""


settings = Settings()
