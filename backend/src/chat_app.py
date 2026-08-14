from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.auth.function_url import FunctionUrlAuthMiddleware
from src.contexts.library.interface.chat_controllers import chat_router
from src.health.controllers import router as health_router

app = FastAPI(title="Bookloud Chat API")

# Add auth middleware
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
