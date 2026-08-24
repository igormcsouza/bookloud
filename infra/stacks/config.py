"""Naming helpers and env var name constants shared across every stack.

The env var name constants (``ENV_*``) must stay in lockstep with
``backend/src/config.py``'s field names — separately deployed projects,
cannot share an import; rename both or neither. The ``ENV_COGNITO_*``
constants must also stay in lockstep with ``frontend/lib/cognito.ts``, which
reads the same names off the SSR Lambda's runtime environment.
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

    # --- phase 4: synthesize Lambda env vars, read by backend/src/config.py ---
    ENV_SYNTHESIZE_QUEUE_URL = "SYNTHESIZE_QUEUE_URL"
    ENV_EDGE_TTS_VOICE = "EDGE_TTS_VOICE"
    ENV_GOOGLE_TTS_VOICE = "GOOGLE_TTS_VOICE"
    ENV_GOOGLE_TTS_SECRET_NAME = "GOOGLE_TTS_SECRET_NAME"
    ENV_SYNTHESIZE_MAX_RECEIVE_COUNT = "SYNTHESIZE_MAX_RECEIVE_COUNT"

    DEFAULT_EDGE_TTS_VOICE = "en-US-AriaNeural"
    DEFAULT_GOOGLE_TTS_VOICE = "en-US-Neural2-C"
    # Out-of-band secret (PLANS/phase-4.md §6.4): CDK never sees the value.
    # One shared secret across every environment -- a personal app with one
    # GCP key has nothing to isolate per-PR.
    GOOGLE_TTS_SECRET_NAME = "bookloud/google-tts-api-key"
    # 5, not 3: throttling from the ESM's MaximumConcurrency returns the
    # message to the queue and *does* bump ApproximateReceiveCount, so a
    # 3-attempt budget can be consumed by backpressure alone, DLQ-ing
    # perfectly good chunks on a large book. Kept in lockstep with
    # backend/src/config.py's synthesize_max_receive_count.
    SYNTHESIZE_MAX_RECEIVE_COUNT = 5

    # --- phase 5: stitch Lambda env vars, read by backend/src/config.py ---
    ENV_STITCH_QUEUE_URL = "STITCH_QUEUE_URL"
    ENV_STITCH_MAX_RECEIVE_COUNT = "STITCH_MAX_RECEIVE_COUNT"

    # 3, not 5 (unlike the synthesize queue): there is no ESM-throttle
    # backpressure to burn attempts here, and each attempt costs up to 15
    # minutes. Kept in lockstep with backend/src/config.py's
    # stitch_max_receive_count.
    STITCH_MAX_RECEIVE_COUNT = 3

    # --- scheduled polling (replaces the extract/synthesize/stitch ESMs --
    # see pipeline_stack.py's _add_scheduled_pollers) ---
    #
    # Each interval must clear its Lambda's own timeout with real margin: an
    # EventBridge Rule has no "skip if the last invocation is still running"
    # option, so if a tick fired while the previous one was still draining a
    # backlog, two invocations could poll (and, on stitch, concatenate) the
    # same queue concurrently. interval > timeout with headroom makes that
    # physically impossible instead of relying on scheduling luck.
    #
    # extract: 120s timeout -> 5 min (2.5x). synthesize: 300s timeout -> 10
    # min (2x). stitch: 900s timeout -> 20 min (~1.33x, the tightest margin
    # since it's already at Lambda's own 15-minute ceiling).
    #
    # This is the throughput knob "increase later if needed" refers to --
    # shortening any of these is a one-line change plus a deploy, no
    # architecture change.
    EXTRACT_POLL_INTERVAL_MINUTES = 5
    SYNTHESIZE_POLL_INTERVAL_MINUTES = 10
    STITCH_POLL_INTERVAL_MINUTES = 20

    # S3 key prefix for uploaded source PDFs -- must stay in lockstep with
    # backend/src/contexts/library/infrastructure/s3_keys.py's
    # ``SOURCE_PREFIX`` and local/setup.sh's notification filter (separately
    # deployed projects, cannot share an import). Used as the S3 event
    # notification's key-prefix filter in PipelineStack (PLANS/phase-3.md
    # §6.1).
    SOURCE_PDF_PREFIX = "books/"

    # --- Cognito env vars, read by backend/src/config.py (the chat Lambda) ---
    ENV_COGNITO_CLIENT_ID = "COGNITO_CLIENT_ID"
    ENV_COGNITO_USER_POOL_ID = "COGNITO_USER_POOL_ID"
    ENV_COGNITO_REGION = "COGNITO_REGION"
    ENV_COGNITO_ENDPOINT = "COGNITO_ENDPOINT"  # local dev only (cognito-local); unset in AWS

    # --- phase 7: Chat Lambda env vars, read by backend/src/config.py ---
    ENV_OPENAI_SECRET_NAME = "OPENAI_SECRET_NAME"
    ENV_OPENAI_MODEL = "OPENAI_MODEL"
    ENV_OPENAI_MAX_OUTPUT_TOKENS = "OPENAI_MAX_OUTPUT_TOKENS"
    ENV_CHAT_DAILY_LIMIT = "CHAT_DAILY_LIMIT"

    # Documented *name* the out-of-band secret is created under (PLANS/
    # phase-7.md §5.2's two-step key setup) -- not a default for
    # ENV_OPENAI_SECRET_NAME itself, which stays "" everywhere including
    # prod (app.py) until a human deliberately deploys with the context flag.
    OPENAI_SECRET_NAME = "bookloud/openai-api-key"
    # Deployed default for OPENAI_MODEL/OPENAI_MAX_OUTPUT_TOKENS/
    # CHAT_DAILY_LIMIT -- kept in lockstep with backend/src/config.py's
    # Settings defaults so a stack that omits the context flag still gets
    # the same numbers the backend would fall back to on its own.
    DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
    OPENAI_MAX_OUTPUT_TOKENS = 700
    CHAT_DAILY_LIMIT = 50


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
