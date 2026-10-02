"""Curated voice discovery and short, fixed-text previews."""

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.services.tts.audio import encode_wav
from app.services.tts.base import InvalidTTSInput, TTSError, TTSUnavailable
from app.services.tts.registry import get_tts_engine

router = APIRouter(prefix="/api/voices", tags=["voices"])
PREVIEW_TEXT = "This is a preview of the selected voice."


class VoiceResponse(BaseModel):
    id: str
    display_name: str
    language: str
    engine: str


class VoicesResponse(BaseModel):
    voices: list[VoiceResponse]


@router.get("", response_model=VoicesResponse)
def list_voices() -> VoicesResponse:
    """Model initialization is blocking, so FastAPI runs this in a worker thread."""
    try:
        available = get_tts_engine().list_voices()
    except TTSUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return VoicesResponse(
        voices=[
            VoiceResponse(
                id=voice.id,
                display_name=voice.display_name,
                language=voice.language,
                engine=voice.engine,
            )
            for voice in available
        ]
    )


@router.post(
    "/{voice_id}/preview",
    response_class=Response,
    responses={200: {"content": {"audio/wav": {}}}},
)
def preview_voice(voice_id: str) -> Response:
    """Generate a short WAV in memory; never retain the preview on disk."""
    try:
        audio = get_tts_engine().synthesize(PREVIEW_TEXT, voice_id)
    except InvalidTTSInput as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except TTSUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    except TTSError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None

    return Response(
        content=encode_wav(audio),
        media_type="audio/wav",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
