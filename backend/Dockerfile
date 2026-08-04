# Multi-target image for the Bookloud backend.
#   `dev`    — plain uvicorn server with hot reload, used by docker-compose.
#   `lambda` — Lambda container image (the default/final stage, so
#              `DockerImageCode.from_image_asset` in CDK picks it up without
#              an explicit `target=`). Behavior is driven entirely by
#              environment variables in both targets.
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

# --- lambda (default/final stage) -------------------------------------
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
