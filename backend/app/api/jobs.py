"""Create and poll local speech-generation jobs."""

import math
import os
import re
from typing import BinaryIO
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app.services.tts.base import TTSUnavailable
from app.services.tts.registry import get_tts_engine
from app.utils.text import EmptyTextError, TextTooLongError, chunk_text

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    text: str
    voice: str
    generation_speed: float = 1.0

    @field_validator("generation_speed", mode="before")
    @classmethod
    def numeric_speed(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Generation speed must be a number between 0.5 and 2.0.")
        return value

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


def _validate_job_id(job_id: str) -> None:
    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found.") from None


@router.get("/{job_id}", response_model=JobStatus)
def get_job(job_id: str, request: Request, response: Response) -> JobStatus:
    _validate_job_id(job_id)
    response.headers["Cache-Control"] = "no-store"
    job = request.app.state.jobs.get_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobStatus(**job)


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request) -> Response:
    _validate_job_id(job_id)
    try:
        removed = request.app.state.jobs.delete_job(job_id)
    except OSError:
        raise HTTPException(status_code=503, detail="Audio could not be removed. Try again.") from None
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found.")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


_RANGE = re.compile(r"bytes=(\d*)-(\d*)\Z")
_STREAM_BLOCK = 64 * 1024


def _requested_bytes(value: str | None, size: int) -> tuple[int, int, bool]:
    """Return an inclusive single range, or the whole file without a Range."""
    if value is None:
        return 0, size - 1, False
    match = _RANGE.fullmatch(value)
    if match is None or not any(match.groups()):
        raise ValueError("Invalid byte range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix < 1:
            raise ValueError("Empty suffix range")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise ValueError("Unsatisfiable byte range")
    return start, end, True


def _stream(handle: BinaryIO, start: int, end: int):
    try:
        handle.seek(start)
        remaining = end - start + 1
        while remaining:
            block = handle.read(min(_STREAM_BLOCK, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block
    finally:
        handle.close()


def _audio_response(job_id: str, request: Request, *, download: bool) -> StreamingResponse:
    _validate_job_id(job_id)
    manager = request.app.state.jobs
    path = manager.live_audio_path(job_id)
    if path is None:
        job = manager.get_status(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job["status"] == "expired":
            raise HTTPException(status_code=410, detail="This audio has expired.")
        if job["status"] == "completed":
            raise HTTPException(status_code=410, detail="Audio is unavailable. Generate it again.")
        if job["status"] == "failed":
            raise HTTPException(status_code=409, detail=job["error"] or "Audio generation failed.")
        raise HTTPException(status_code=409, detail="Audio is still being generated.")

    try:
        handle = path.open("rb")
    except OSError:
        raise HTTPException(status_code=410, detail="Audio is unavailable. Generate it again.") from None
    try:
        # Recheck after opening to close the gap with concurrent expiry/deletion.
        current = manager.get_status(job_id)
        if current is None or current["status"] != "completed":
            raise HTTPException(status_code=410, detail="This audio has expired or was removed.")
        size = os.fstat(handle.fileno()).st_size
        if size < 1:
            raise HTTPException(status_code=410, detail="Audio is unavailable. Generate it again.")
        try:
            start, end, partial = _requested_bytes(request.headers.get("range"), size)
        except ValueError:
            raise HTTPException(
                status_code=416,
                detail="Requested audio range is unavailable.",
                headers={"Content-Range": f"bytes */{size}", "Cache-Control": "no-store"},
            ) from None
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": (
                f'{"attachment" if download else "inline"}; filename="edspeech-{job_id}.mp3"'
            ),
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            _stream(handle, start, end),
            status_code=206 if partial else 200,
            media_type="audio/mpeg",
            headers=headers,
            background=BackgroundTask(handle.close),
        )
    except BaseException:
        handle.close()
        raise


@router.get("/{job_id}/audio", response_class=StreamingResponse)
def play_audio(job_id: str, request: Request) -> StreamingResponse:
    return _audio_response(job_id, request, download=False)


@router.get("/{job_id}/download", response_class=StreamingResponse)
def download_audio(job_id: str, request: Request) -> StreamingResponse:
    return _audio_response(job_id, request, download=True)
