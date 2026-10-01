import asyncio
from io import BytesIO
import threading
import time
from uuid import UUID
import wave

from httpx import ASGITransport, AsyncClient
import numpy as np
import pytest

from app.config import Settings
from app.main import app
from app.services.jobs import JobManager, JobStore
from app.services.tts.base import AudioResult, Voice


@pytest.fixture
def job_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "temp"))
    monkeypatch.setenv("TTS_CHUNK_SIZE", "500")
    fake = FakeEngine()
    monkeypatch.setattr("app.api.jobs.get_tts_engine", lambda: fake)
    monkeypatch.setattr("app.services.jobs.get_tts_engine", lambda: fake)
    return fake, tmp_path


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.block_on = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_on = None

    def list_voices(self):
        return (Voice("heart", "Heart", "en-US", "fake"),)

    def synthesize(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        if text == self.block_on:
            self.entered.set()
            assert self.release.wait(5), "test did not release fake synthesis"
        if text == self.fail_on:
            raise RuntimeError("secret document text in internal failure")
        return AudioResult(np.array([0.0, 0.25, -0.25], dtype=np.float32), 24000)


async def _wait_status(client, job_id, status, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = await client.get(f"/api/jobs/{job_id}")
        if response.json()["status"] == status:
            return response.json()
        await asyncio.sleep(0.02)
    pytest.fail(f"Job did not reach {status}: {response.json()}")


def test_jobs_are_fifo_single_worker_and_write_ordered_wavs(job_env):
    engine, root = job_env
    first_chunk = "First " * 70
    second_chunk = "Second " * 70
    engine.block_on = second_chunk.strip()

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post("/api/jobs", json={
                    "text": first_chunk + "\n\n" + second_chunk,
                    "voice": "heart", "generation_speed": 1.25,
                })
                assert first.status_code == 202
                assert first.json()["status"] == "queued"
                first_id = first.json()["id"]
                UUID(first_id)
                assert await asyncio.to_thread(engine.entered.wait, 3)
                second = await client.post("/api/jobs", json={
                    "text": "Later job.", "voice": "heart"
                })
                assert second.status_code == 202
                second_id = second.json()["id"]
                queued = (await client.get(f"/api/jobs/{second_id}")).json()
                assert queued["status"] == "queued"
                assert queued["progress"] == 0
                active = (await client.get(f"/api/jobs/{first_id}")).json()
                assert active["status"] == "processing"
                assert active["total_chunks"] == 2
                assert active["current_chunk"] == 1
                assert active["progress"] == 50
                engine.release.set()
                done_first = await _wait_status(client, first_id, "completed")
                done_second = await _wait_status(client, second_id, "completed")
                assert done_first["progress"] == 100
                assert done_first["current_chunk"] == 2
                assert done_first["audio_duration"] > 0
                assert done_first["error"] is None
                assert done_first["expires_at"] is None
                assert done_second["progress"] == 100
                assert "audio_path" not in done_first
                assert "chunks_json" not in done_first
                assert first_chunk.strip() not in str(done_first)
                assert [call[0] for call in engine.calls] == [
                    first_chunk.strip(), second_chunk.strip(), "Later job."
                ]
                for number in (1, 2):
                    path = root / "temp" / "jobs" / first_id / f"{number:06d}.wav"
                    with wave.open(BytesIO(path.read_bytes()), "rb") as wav:
                        assert wav.getnframes() == 3

    try:
        asyncio.run(run())
    finally:
        engine.release.set()


def test_failed_job_does_not_crash_worker_or_leak_text(job_env):
    engine, root = job_env
    engine.fail_on = "Private words."

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                failed = await client.post("/api/jobs", json={
                    "text": engine.fail_on, "voice": "heart"
                })
                following = await client.post("/api/jobs", json={
                    "text": "Good words.", "voice": "heart"
                })
                bad = await _wait_status(client, failed.json()["id"], "failed")
                good = await _wait_status(client, following.json()["id"], "completed")
                assert bad["error"] == "Speech generation failed. Please try again."
                assert "Private" not in str(bad)
                assert good["status"] == "completed"
                assert not (root / "temp" / "jobs" / failed.json()["id"]).exists()

    asyncio.run(run())


def test_restart_fails_interrupted_job_and_resumes_queued(job_env):
    engine, root = job_env
    settings = Settings()
    store = JobStore(settings.database_dir)
    interrupted = store.create(["Old text."], "heart", 1.0)["id"]
    store.claim_next()
    partial_dir = root / "temp" / "jobs" / interrupted
    partial_dir.mkdir(parents=True)
    (partial_dir / "000001.wav").write_bytes(b"partial")
    pending = store.create(["Queued text."], "heart", 1.0)["id"]
    manager = JobManager(settings)
    manager.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and store.get(pending)["status"] != "completed":
            time.sleep(0.02)
        assert store.get(pending)["status"] == "completed"
        assert store.get(interrupted)["status"] == "failed"
        assert store.get(interrupted)["error"] == "Generation was interrupted. Please submit the text again."
        assert not partial_dir.exists()
        assert [call[0] for call in engine.calls] == ["Queued text."]
    finally:
        manager.stop()


def test_second_manager_does_not_interrupt_active_worker(job_env):
    engine, _ = job_env
    engine.block_on = "First job."
    settings = Settings()
    first_manager = JobManager(settings)
    first_id = first_manager.store.create(["First job."], "heart", 1.0)["id"]
    first_manager.start()
    second_manager = None
    try:
        assert engine.entered.wait(3)
        second_id = first_manager.store.create(["Second job."], "heart", 1.0)["id"]
        second_manager = JobManager(settings)
        second_manager.start()
        time.sleep(0.15)
        assert first_manager.store.get(first_id)["status"] == "processing"
        assert first_manager.store.get(second_id)["status"] == "queued"
        assert len(engine.calls) == 1
        engine.release.set()
        end = time.monotonic() + 5
        while time.monotonic() < end and first_manager.store.get(second_id)["status"] != "completed":
            time.sleep(0.02)
        assert first_manager.store.get(first_id)["status"] == "completed"
        assert first_manager.store.get(second_id)["status"] == "completed"
        assert [call[0] for call in engine.calls] == ["First job.", "Second job."]
    finally:
        engine.release.set()
        first_manager.stop()
        if second_manager:
            second_manager.stop()


@pytest.mark.parametrize("payload,status", [
    ({"text": "   ", "voice": "heart"}, 422),
    ({"text": "Hi.", "voice": "unknown"}, 422),
    ({"text": "Hi.", "voice": "heart", "generation_speed": 4}, 422),
    ({"text": "Hi.", "voice": "heart", "generation_speed": float("inf")}, 422),
])
def test_job_validation(job_env, payload, status):
    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                if payload.get("generation_speed") == float("inf"):
                    response = await client.post(
                        "/api/jobs",
                        content=b'{"text":"Hi.","voice":"heart","generation_speed":Infinity}',
                        headers={"content-type": "application/json"},
                    )
                else:
                    response = await client.post("/api/jobs", json=payload)
                assert response.status_code == status
                assert (await client.get("/api/jobs/not-a-job")).status_code == 404

    asyncio.run(run())


def test_job_maximum_text_size(job_env, monkeypatch):
    monkeypatch.setenv("MAX_TEXT_CHARS", "5")

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/jobs", json={"text": "Too long", "voice": "heart"})
                assert response.status_code == 413

    asyncio.run(run())
