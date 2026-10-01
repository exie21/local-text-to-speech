"""LocalReader's API entry point."""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.responses import JSONResponse

from app.api.documents import router as documents_router
from app.api.jobs import router as jobs_router
from app.api.voices import router as voices_router
from app.config import Settings
from app.services.jobs import JobManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings()
    app.state.jobs = JobManager(app.state.settings)
    app.state.jobs.start()
    try:
        yield
    finally:
        app.state.jobs.stop()


app = FastAPI(title="LocalReader", lifespan=lifespan)
app.include_router(voices_router)
app.include_router(documents_router)
app.include_router(jobs_router)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, error: RequestValidationError) -> JSONResponse:
    """Keep submitted document text out of validation responses and logs."""
    return JSONResponse(
        status_code=422,
        content={"detail": [
            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]},
    )


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report API availability without requiring a TTS model or database."""
    return HealthResponse()
