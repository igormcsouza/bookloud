from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.auth.function_url import FunctionUrlAuthMiddleware
from src.contexts.library.interface.chat_controllers import chat_router
from src.health.controllers import router as health_router

app = FastAPI(title="Bookloud Chat API")

from src.auth.local_dev import LocalAuthMiddleware, should_enable
from src.config import settings

# Add auth middleware
if should_enable(settings.environment):
    app.add_middleware(LocalAuthMiddleware)
else:
    app.add_middleware(FunctionUrlAuthMiddleware)

# CORS middleware -- local only. Everywhere else, the Function URL's own
# native CORS config (api_stack.py's FunctionUrlCorsOptions) already answers
# preflight without invoking the function and stamps every real response.
# Adding this middleware unconditionally meant BOTH layers wrote an
# Access-Control-Allow-Origin header on every POST response (unlike OPTIONS,
# which the platform answers before the Lambda ever runs) -- browsers reject
# a response with more than one value for that header outright, so every
# deployed chat request failed with "Failed to fetch" despite the Lambda
# itself completing successfully. Locally there is no Function URL in front
# of uvicorn at all, so this middleware is the only thing that can answer
# CORS there.
if should_enable(settings.environment):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(health_router)
app.include_router(chat_router)

from botocore.exceptions import ClientError
from fastapi.responses import JSONResponse
from fastapi import Request
from src.shared_kernel.domain.errors import DomainError

@app.exception_handler(ClientError)
async def aws_error_handler(request: Request, exc: ClientError) -> JSONResponse:
    """Reached only by get_chat_model()'s eager get_secret() call, which runs
    as a FastAPI dependency -- i.e. before the route body, before the
    StreamingResponse, before any byte (PLANS/phase-7.md §4.5 step 7). A prod
    stack whose OPENAI_SECRET_NAME points at a secret that does not exist
    (or that ChatFunction can't read) gets one clean 503, never a half
    stream and never the raw ResourceNotFoundException."""
    import logging
    logger = logging.getLogger("bookloud")
    logger.error("AWS error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=503,
        content={"code": "LLM_UNAVAILABLE", "message": "Chat is temporarily unavailable. Try again shortly."},
    )

@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
