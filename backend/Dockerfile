# Multi-target image for the Bookloud backend.
#   `dev`    — plain uvicorn server with hot reload, used by docker-compose.
#   `lambda` — Lambda container image (the RIC-based one every function
#              except ChatFunction runs). Every `DockerImageCode.
#              from_image_asset` call in infra/ passes an explicit
#              `target="lambda"` -- do not rely on "it's the default stage"
#              again; see the anonymous final stage at the bottom of this
#              file for why. Behavior is driven entirely by environment
#              variables in both targets.
#
# Container image (not a zip) because phases 3-4 pull in PyMuPDF and
# edge-tts, which blow past the Lambda zip layer size limits; establishing
# the container path now avoids a migration later.

FROM python:3.12-slim AS dev

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --group dev

COPY . .

CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- lambda -------------------------------------------------------------
FROM public.ecr.aws/lambda/python:3.12 AS lambda

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR ${LAMBDA_TASK_ROOT}

# Install runtime-only dependencies (no dev group, no caches) into the task
# root.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --no-cache -r requirements.txt --target "${LAMBDA_TASK_ROOT}" \
    && rm requirements.txt

# Copy the application code (same app object used locally and in tests).
COPY lambda_function.py ./
COPY src ./src

CMD ["lambda_function.handler"]

# --- lambda-stream (response streaming; PLANS/phase-7.md §9.2) --------------
#
# FROM lambda, so this is the SAME dependency set and the SAME src/ -- the
# pushed delta is one small layer, not a second full image.
#
# The adapter is HERE and NOT in the `lambda` stage, and that is load-bearing:
# anything in /opt/extensions/ is started as an external extension for EVERY
# function built from that image. Put it in `lambda` and the adapter would
# come up alongside the Runtime Interface Client on ExtractFunction,
# SynthesizeFunction, StitchFunction and ApiFunction, with both polling the
# Runtime API. Do not "simplify" this by moving the COPY up.
FROM lambda AS lambda-stream

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_ASYNC_INIT=true \
    PYTHONPATH=/var/task

# The base image's ENTRYPOINT is the RIC. Clear it: with the adapter present,
# the container's job is to be a web server, and the adapter owns the Runtime
# API loop.
ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "src.chat_app:app", "--host", "0.0.0.0", "--port", "8080"]

# --- default target -------------------------------------------------------
#
# `docker build` with no `--target` picks the LAST stage in the file. That
# used to be `lambda` -- silently became `lambda-stream` the moment it was
# appended above, and every function whose CDK call omitted `target=`
# started building the wrong image (uvicorn serving `src.chat_app:app`, no
# Runtime Interface Client) until it 500'd on every route including
# `/health`. Every CDK call site now passes an explicit target; this
# anonymous final stage is belt-and-suspenders so a bare `docker build .`
# still does the intuitive thing too -- content-identical to `lambda`
# (fully cached, zero extra cost), it exists only to be the last stage in
# the file again.
FROM lambda
