"""Naming helpers and env var name constants shared across every stack.

The env var name constants (``ENV_*``) must stay in lockstep with
``backend/src/config.py``'s field names — separately deployed projects,
cannot share an import; rename both or neither.
"""

from __future__ import annotations


class Config:
    DEFAULT_ENVIRONMENT = "dev"
    STACK_PREFIX = "Bookloud"

    # --- env var names set on the API Lambda, read by backend/src/config.py ---
    ENV_ENVIRONMENT = "ENVIRONMENT"
    ENV_GIT_SHA = "GIT_SHA"
    ENV_TABLE_NAME = "TABLE_NAME"
    ENV_PDF_BUCKET = "PDF_BUCKET"
    ENV_AUDIO_BUCKET = "AUDIO_BUCKET"
    ENV_MARKS_BUCKET = "MARKS_BUCKET"
    ENV_EXTRACT_QUEUE_URL = "EXTRACT_QUEUE_URL"
    ENV_LOG_LEVEL = "LOG_LEVEL"

    # --- env var name read by frontend/lib/api.ts ---
    ENV_API_BASE_URL = "NEXT_PUBLIC_API_BASE_URL"


def is_prod(environment: str) -> bool:
    return environment == "prod"


def stack_name(component: str, environment: str) -> str:
    """e.g. stack_name("Api", "pr-42") -> "BookloudApi-pr-42"."""
    return f"{Config.STACK_PREFIX}{component}-{environment}"


def table_name(environment: str) -> str:
    return f"bookloud-{environment}"


def bucket_logical_prefix(kind: str, environment: str) -> str:
    """Buckets are CDK-auto-named (global namespace + 63-char limit make
    explicit names a liability); this is only used for tagging/logging."""
    return f"bookloud-{environment}-{kind}"


def queue_name(kind: str, environment: str) -> str:
    return f"bookloud-{environment}-{kind}"


def cognito_pool_name(environment: str) -> str:
    return f"bookloud-{environment}"


def http_api_name(environment: str) -> str:
    return f"bookloud-{environment}"
