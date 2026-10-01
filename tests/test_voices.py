import asyncio
from io import BytesIO
import wave
from unittest.mock import Mock

from httpx import ASGITransport, AsyncClient
import numpy as np
import pytest

from app.api.voices import PREVIEW_TEXT
from app.main import app
from app.services.tts.base import AudioResult, InvalidTTSInput, TTSError, TTSUnavailable, Voice


async def request(method: str, path: str):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path)


@pytest.fixture
def engine(monkeypatch):
    fake = Mock()
    fake.list_voices.return_value = (Voice("heart", "Heart", "en-US", "kokoro"),)
    fake.synthesize.return_value = AudioResult(
        samples=np.array([0.0, 0.25, -0.25], dtype=np.float32), sample_rate=24000
    )
    monkeypatch.setattr("app.api.voices.get_tts_engine", lambda: fake)
    return fake


def test_voice_list_has_stable_public_metadata(engine):
    response = asyncio.run(request("GET", "/api/voices"))
    assert response.status_code == 200
    assert response.json() == {"voices": [{
        "id": "heart", "display_name": "Heart", "language": "en-US", "engine": "kokoro"
    }]}
    engine.list_voices.assert_called_once_with()


def test_preview_is_fixed_text_playable_wav_and_leaves_no_files(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    response = asyncio.run(request("POST", "/api/voices/heart/preview"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert response.content[:4] == b"RIFF"
    with wave.open(BytesIO(response.content), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 24000, 3)
    engine.synthesize.assert_called_once_with(PREVIEW_TEXT, "heart")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error,status", [
    (InvalidTTSInput("Choose an available voice."), 404),
    (TTSUnavailable("Kokoro model or voices are missing. Run the model setup command first."), 503),
    (TTSError("Speech generation failed."), 502),
])
def test_preview_maps_engine_errors(engine, error, status):
    engine.synthesize.side_effect = error
    response = asyncio.run(request("POST", "/api/voices/invalid/preview"))
    assert response.status_code == status
    assert response.json() == {"detail": str(error)}


def test_model_failure_does_not_break_health(engine):
    engine.list_voices.side_effect = TTSUnavailable("Model unavailable.")

    async def check():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/api/voices"), await client.get("/api/health")

    voices, health = asyncio.run(check())
    assert voices.status_code == 503
    assert voices.json() == {"detail": "Model unavailable."}
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
