"""Create and poll local speech-generation jobs."""

import math
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.services.tts.base import TTSUnavailable
from app.services.tts.registry import get_tts_engine
from app.utils.text import EmptyTextError, TextTooLongError, chunk_text

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    text: str
    voice: str
    generation_speed: float = 1.0

    @field_validator("generation_speed")
    @classmethod
    def valid_speed(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.5 <= value <= 2.0:
            raise ValueError("Generation speed must be between 0.5 and 2.0.")
        return value


class JobCreated(BaseModel):
    id: str
    status: str


class JobStatus(BaseModel):
    id: str
    status: str
    voice: str
    generation_speed: float
    created_at: str
    updated_at: str
    completed_at: str | None
    expires_at: str | None
    progress: int
    current_chunk: int
    total_chunks: int
    audio_duration: float | None
    error: str | None


@router.post("", response_model=JobCreated, status_code=202)
def create_job(payload: JobCreate, request: Request) -> JobCreated:
    settings = request.app.state.settings
    try:
        chunks = chunk_text(
            payload.text, chunk_size=settings.tts_chunk_size, max_chars=settings.max_text_chars
        )
    except EmptyTextError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except TextTooLongError as error:
        raise HTTPException(status_code=413, detail=str(error)) from None

    try:
        voices = get_tts_engine().list_voices()
    except TTSUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    if payload.voice not in {voice.id for voice in voices}:
        raise HTTPException(status_code=422, detail="Choose an available voice.")

    manager = request.app.state.jobs
    created = manager.store.create(chunks, payload.voice, payload.generation_speed)
    manager.notify()
    return JobCreated(**created)


@router.get("/{job_id}", response_model=JobStatus)
def get_job(job_id: str, request: Request) -> JobStatus:
    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found.") from None
    job = request.app.state.jobs.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobStatus(**job)
