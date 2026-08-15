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
    # The ESM's max_concurrency -- the real throttle on concurrent synthesize
    # invocations (see pipeline_stack.py's reserved-concurrency comment for
    # the full reasoning). A single named constant so it's a one-line change
    # if the 403 rate from edge-tts's free endpoint stays at zero.
    #
    # Lowered 5 -> 2 in phase 5 (PLANS/phase-5.md OQ-2). The account's *total*
    # Lambda concurrency is 10. With the stitch function now competing too, a
    # large book in flight could consume 5 (synthesize) + 1 (extract) +
    # 2 (stitch) = 8, leaving 2 for the API Lambda and the frontend SSR
    # Lambda combined -- so the user's own status polling would get throttled
    # precisely while their book is processing.
    #
    # 2 is the deliberate FLOOR, not a tuned value: start minimal, measure a
    # real prod book, bump once there is data. Nothing has ever run the real
    # edge-tts engine, so per-chunk latency is unmeasured and any "tuned"
    # number here would be invented. 2 + 1 + 2 = 5 leaves 5 for API + SSR.
    #
    # 2 rather than 1 because ScalingConfig.MaximumConcurrency's minimum
    # accepted value is 2 -- 1 is only expressible by dropping the
    # ScalingConfig entirely, which would lose the poller back-off and let
    # throttling burn ApproximateReceiveCount toward SYNTHESIZE_MAX_RECEIVE_
    # COUNT, DLQ-ing chunks for backpressure rather than for any TTS failure.
    #
    # Signals that it is too low: time-to-READY on a real book, or chunks
    # DLQ-ing under throttling. Bumping is this one line plus a deploy.
    SYNTHESIZE_MAX_CONCURRENCY = 2

    # --- phase 5: stitch Lambda env vars, read by backend/src/config.py ---
    ENV_STITCH_QUEUE_URL = "STITCH_QUEUE_URL"
    ENV_STITCH_MAX_RECEIVE_COUNT = "STITCH_MAX_RECEIVE_COUNT"

    # 3, not 5 (unlike the synthesize queue): there is no ESM-throttle
    # backpressure to burn attempts here -- max_concurrency 2 with one
    # message per book means the poller essentially never throttles -- and
    # each attempt costs up to 15 minutes. Kept in lockstep with
    # backend/src/config.py's stitch_max_receive_count.
    STITCH_MAX_RECEIVE_COUNT = 3
    # The ESM minimum. One book at a time is the real workload, and this is
    # the memory-heaviest function in the system against a 10-execution
    # account ceiling (PLANS/phase-5.md §6.3).
    STITCH_MAX_CONCURRENCY = 2

    # S3 key prefix for uploaded source PDFs -- must stay in lockstep with
    # backend/src/contexts/library/infrastructure/s3_keys.py's
    # ``SOURCE_PREFIX`` and local/setup.sh's notification filter (separately
    # deployed projects, cannot share an import). Used as the S3 event
    # notification's key-prefix filter in PipelineStack (PLANS/phase-3.md
    # §6.1).
    SOURCE_PDF_PREFIX = "books/"

    # --- env var name read by frontend/lib/api.ts ---
    ENV_API_BASE_URL = "NEXT_PUBLIC_API_BASE_URL"
    ENV_CHAT_BASE_URL = "NEXT_PUBLIC_CHAT_BASE_URL"

    # --- env vars set on the SSR Lambda, read by frontend/lib/cognito.ts ---
    # Deliberately NOT NEXT_PUBLIC_* -- server-only, read at runtime by the
    # Next.js BFF route handlers, never baked into the client bundle.
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
