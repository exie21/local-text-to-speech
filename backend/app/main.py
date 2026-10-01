"""LocalReader's API entry point."""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.documents import router as documents_router
from app.api.voices import router as voices_router
from app.config import Settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings()
    yield


app = FastAPI(title="LocalReader", lifespan=lifespan)
app.include_router(voices_router)
app.include_router(documents_router)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report API availability without requiring a TTS model or database."""
    return HealthResponse()
