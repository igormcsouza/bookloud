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

# CORS middleware
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
    import logging
    logger = logging.getLogger("bookloud")
    logger.error("AWS error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=503, content={"detail": "Storage temporarily unavailable"}
    )

@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
