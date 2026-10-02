import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
import numpy as np
import pytest
from starlette.requests import Request

from app.api.jobs import _audio_response
from app.config import Settings
from app.main import app
from app.services.audio_assembly import AssemblyError, assemble_chunks
from app.services.jobs import JobManager, JobStore
from app.services.tts.audio import encode_wav
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


def test_jobs_are_fifo_single_worker_and_create_mp3(job_env):
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
                assert 1799 < (datetime.fromisoformat(done_first["expires_at"]) - datetime.fromisoformat(done_first["completed_at"])).total_seconds() < 1801
                assert done_second["progress"] == 100
                assert "audio_path" not in done_first
                assert "chunks_json" not in done_first
                assert first_chunk.strip() not in str(done_first)
                assert [call[0] for call in engine.calls] == [
                    first_chunk.strip(), second_chunk.strip(), "Later job."
                ]
                final = root / "temp" / "audio" / f"{first_id}.mp3"
                assert final.stat().st_size > 0
                assert not (root / "temp" / "jobs" / first_id).exists()
                with sqlite3.connect(root / "db" / "jobs.sqlite3") as db:
                    assert db.execute("SELECT audio_path FROM jobs WHERE id = ?", (first_id,)).fetchone()[0] == str(final)

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


def test_assembling_state_and_queued_request(job_env, monkeypatch):
    _, root = job_env
    entered = threading.Event()
    release = threading.Event()

    def wait_then_assemble(temp_dir, job_id, total_chunks):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return assemble_chunks(temp_dir, job_id, total_chunks)

    monkeypatch.setattr("app.services.jobs.assemble_chunks", wait_then_assemble)

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post("/api/jobs", json={"text": "First.", "voice": "heart"})
                assert await asyncio.to_thread(entered.wait, 3)
                second = await client.post("/api/jobs", json={"text": "Second.", "voice": "heart"})
                active = (await client.get(f"/api/jobs/{first.json()['id']}")).json()
                queued = (await client.get(f"/api/jobs/{second.json()['id']}")).json()
                assert active["status"] == "assembling"
                assert active["progress"] == 100
                assert active["audio_duration"] is None
                assert queued["status"] == "queued"
                release.set()
                await _wait_status(client, first.json()["id"], "completed")
                await _wait_status(client, second.json()["id"], "completed")
                assert (root / "temp" / "audio" / f"{first.json()['id']}.mp3").exists()

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_assembly_failure_is_safe_and_next_job_runs(job_env, monkeypatch):
    _, root = job_env
    attempts = 0

    def fail_once(temp_dir, job_id, total_chunks):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AssemblyError("Audio assembly failed. Please try again.")
        return assemble_chunks(temp_dir, job_id, total_chunks)

    monkeypatch.setattr("app.services.jobs.assemble_chunks", fail_once)

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post("/api/jobs", json={"text": "Private.", "voice": "heart"})
                second = await client.post("/api/jobs", json={"text": "Public.", "voice": "heart"})
                failed = await _wait_status(client, first.json()["id"], "failed")
                completed = await _wait_status(client, second.json()["id"], "completed")
                assert failed["error"] == "Audio assembly failed. Please try again."
                assert "Private" not in str(failed)
                assert completed["status"] == "completed"
                assert not (root / "temp" / "jobs" / first.json()["id"]).exists()
                assert not (root / "temp" / "audio" / f"{first.json()['id']}.mp3").exists()

    asyncio.run(run())


def test_phase_six_completed_wavs_are_assembled_on_upgrade(job_env):
    engine, root = job_env
    settings = Settings()
    store = JobStore(settings.database_dir)
    legacy_id = store.create(["Old text."], "heart", 1.0)["id"]
    with sqlite3.connect(store.path) as db:
        db.execute(
            """UPDATE jobs SET status = 'completed', progress = 100,
               current_chunk = 1, completed_at = created_at, chunks_json = NULL
               WHERE id = ?""", (legacy_id,)
        )
    chunk_dir = root / "temp" / "jobs" / legacy_id
    chunk_dir.mkdir(parents=True)
    chunk_dir.joinpath("000001.wav").write_bytes(encode_wav(AudioResult(
        np.zeros(2400, dtype=np.float32), 24000
    )))

    manager = JobManager(settings)
    manager.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and store.get(legacy_id)["status"] != "completed":
            time.sleep(0.02)
        # It starts completed, so wait for the MP3 record rather than status.
        while time.monotonic() < end:
            with sqlite3.connect(store.path) as db:
                row = db.execute("SELECT status, audio_path FROM jobs WHERE id = ?", (legacy_id,)).fetchone()
            if row[1]:
                break
            time.sleep(0.02)
        assert row == ("completed", str(root / "temp" / "audio" / f"{legacy_id}.mp3"))
        assert not chunk_dir.exists()
        assert engine.calls == []
    finally:
        manager.stop()


def test_interrupted_assembly_cleans_partial_mp3(job_env):
    _, root = job_env
    settings = Settings()
    store = JobStore(settings.database_dir)
    job_id = store.create(["Interrupted."], "heart", 1.0)["id"]
    store.claim_next()
    store.mark_assembling(job_id)
    chunk_dir = root / "temp" / "jobs" / job_id
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "000001.wav").write_bytes(b"partial")
    audio_dir = root / "temp" / "audio"
    audio_dir.mkdir()
    final = audio_dir / f"{job_id}.mp3"
    staged = audio_dir / f"{job_id}.partial.mp3"
    final.write_bytes(b"partial")
    staged.write_bytes(b"partial")
    manager = JobManager(settings)
    manager.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and store.get(job_id)["status"] != "failed":
            time.sleep(0.02)
        assert store.get(job_id)["status"] == "failed"
        assert not chunk_dir.exists()
        assert not final.exists()
        assert not staged.exists()
    finally:
        manager.stop()


@pytest.mark.parametrize("payload,status", [
    ({"text": "   ", "voice": "heart"}, 422),
    ({"text": "Hi.", "voice": "unknown"}, 422),
    ({"text": "Hi.", "voice": "heart", "generation_speed": 4}, 422),
    ({"text": "Hi.", "voice": "heart", "generation_speed": float("inf")}, 422),
    ({"text": "Hi.", "voice": "heart", "generation_speed": True}, 422),
    ({"text": "Hi.", "voice": "heart", "generation_speed": "1.2"}, 422),
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


def test_short_ttl_expires_automatically_without_poll_request(job_env, monkeypatch):
    _, root = job_env
    monkeypatch.setenv("AUDIO_TTL_MINUTES", "0.001")
    monkeypatch.setattr("app.main.JobManager", lambda settings: JobManager(settings, 0.03))

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/jobs", json={"text": "Short TTL.", "voice": "heart"})
                job_id = created.json()["id"]
                await _wait_status(client, job_id, "completed")
                final = root / "temp" / "audio" / f"{job_id}.mp3"
                assert final.exists()
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    record = app.state.jobs.store.get(job_id)
                    if record["status"] == "expired" and not final.exists():
                        break
                    await asyncio.sleep(0.02)
                assert record["status"] == "expired"
                assert not final.exists()
                assert app.state.jobs.live_audio_path(job_id) is None
                response = await client.get(f"/api/jobs/{job_id}")
                assert response.json()["status"] == "expired"
                assert response.headers["cache-control"] == "no-store"

    asyncio.run(run())


def test_request_time_expiration_does_not_wait_for_timer(job_env, monkeypatch):
    _, root = job_env
    monkeypatch.setenv("AUDIO_TTL_MINUTES", "0.001")
    monkeypatch.setattr("app.main.JobManager", lambda settings: JobManager(settings, 3600))

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/jobs", json={"text": "Guarded.", "voice": "heart"})
                job_id = created.json()["id"]
                await _wait_status(client, job_id, "completed")
                final = root / "temp" / "audio" / f"{job_id}.mp3"
                assert final.exists()
                await asyncio.sleep(0.09)
                assert app.state.jobs.store.get(job_id)["status"] == "completed"
                assert app.state.jobs.live_audio_path(job_id) is None
                assert (await client.get(f"/api/jobs/{job_id}")).json()["status"] == "expired"
                assert not final.exists()

    asyncio.run(run())


def test_startup_backfills_old_completed_mp3_expiration(job_env):
    _, root = job_env
    settings = Settings(audio_ttl_minutes=1)
    store = JobStore(settings.database_dir)
    job_id = store.create(["Old MP3."], "heart", 1.0)["id"]
    final = root / "temp" / "audio" / f"{job_id}.mp3"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"old mp3")
    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    with sqlite3.connect(store.path) as db:
        db.execute(
            """UPDATE jobs SET status = 'completed', completed_at = ?,
               audio_path = ?, chunks_json = NULL WHERE id = ?""",
            (old, str(final), job_id),
        )
    manager = JobManager(settings, 0.03)
    manager.start()
    try:
        end = time.monotonic() + 3
        while time.monotonic() < end and final.exists():
            time.sleep(0.02)
        assert store.get(job_id)["status"] == "expired"
        assert not final.exists()
        assert datetime.fromisoformat(store.get(job_id)["expires_at"]) == datetime.fromisoformat(old) + timedelta(minutes=1)
    finally:
        manager.stop()


def test_failed_file_delete_is_retried_without_restoring_access(job_env, monkeypatch):
    _, root = job_env
    settings = Settings()
    manager = JobManager(settings)
    job_id = manager.store.create(["Delete retry."], "heart", 1.0)["id"]
    manager.store.claim_next()
    manager.store.mark_assembling(job_id)
    final = root / "temp" / "audio" / f"{job_id}.mp3"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"mp3")
    manager.store.complete(job_id, 1.0, final, timedelta(seconds=-1))
    original = manager._clean_output

    def fail_once(_job_id):
        raise OSError("private path")

    monkeypatch.setattr(manager, "_clean_output", fail_once)
    manager.expire_due()
    assert manager.store.get(job_id)["status"] == "expired"
    assert final.exists()
    assert manager.live_audio_path(job_id) is None
    monkeypatch.setattr(manager, "_clean_output", original)
    manager.expire_due()
    assert not final.exists()
    assert manager.store.audio_record(job_id)["audio_path"] is None


def test_delete_queued_and_active_jobs(job_env):
    engine, root = job_env
    engine.block_on = "First job."

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post("/api/jobs", json={"text": "First job.", "voice": "heart"})
                first_id = first.json()["id"]
                assert await asyncio.to_thread(engine.entered.wait, 3)
                second = await client.post("/api/jobs", json={"text": "Queued job.", "voice": "heart"})
                second_id = second.json()["id"]
                deleted = await client.delete(f"/api/jobs/{second_id}")
                assert deleted.status_code == 204
                assert (await client.get(f"/api/jobs/{second_id}")).status_code == 404
                assert (await client.delete(f"/api/jobs/{second_id}")).status_code == 404
                assert (await client.delete(f"/api/jobs/{first_id}")).status_code == 204
                engine.release.set()
                third = await client.post("/api/jobs", json={"text": "After delete.", "voice": "heart"})
                await _wait_status(client, third.json()["id"], "completed")
                assert (await client.get(f"/api/jobs/{first_id}")).status_code == 404
                assert [call[0] for call in engine.calls] == ["First job.", "After delete."]
                assert not (root / "temp" / "audio" / f"{first_id}.mp3").exists()
                assert not (root / "temp" / "jobs" / first_id).exists()

    try:
        asyncio.run(run())
    finally:
        engine.release.set()


def test_failed_manual_delete_keeps_audio_inaccessible_and_can_retry(job_env, monkeypatch):
    _, root = job_env
    manager = JobManager(Settings())
    job_id = manager.store.create(["Manual delete."], "heart", 1.0)["id"]
    manager.store.claim_next()
    manager.store.mark_assembling(job_id)
    final = root / "temp" / "audio" / f"{job_id}.mp3"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"mp3")
    manager.store.complete(job_id, 1.0, final, timedelta(minutes=30))
    original = manager._clean_output

    def fail(_job_id):
        raise OSError("cannot unlink")

    monkeypatch.setattr(manager, "_clean_output", fail)
    with pytest.raises(OSError):
        manager.delete_job(job_id)
    assert manager.store.get(job_id)["status"] == "expired"
    assert manager.live_audio_path(job_id) is None
    monkeypatch.setattr(manager, "_clean_output", original)
    assert manager.delete_job(job_id)
    assert manager.store.get(job_id) is None
    assert not final.exists()


def test_orphan_cleanup_preserves_legacy_and_unrelated_files(job_env):
    _, root = job_env
    settings = Settings()
    manager = JobManager(settings)
    legacy = manager.store.create(["Legacy."], "heart", 1.0)["id"]
    with sqlite3.connect(manager.store.path) as db:
        db.execute(
            "UPDATE jobs SET status = 'completed', chunks_json = NULL WHERE id = ?",
            (legacy,),
        )
    legacy_dir = root / "temp" / "jobs" / legacy
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "000001.wav").write_bytes(b"legacy")
    orphan_dir = root / "temp" / "jobs" / str(uuid4())
    orphan_dir.mkdir()
    (orphan_dir / "000001.wav").write_bytes(b"orphan")
    audio_dir = root / "temp" / "audio"
    audio_dir.mkdir()
    orphan_mp3 = audio_dir / f"{uuid4()}.mp3"
    orphan_mp3.write_bytes(b"orphan")
    unrelated = audio_dir / "notes.mp3"
    unrelated.write_bytes(b"leave alone")

    manager._clean_orphans()

    assert legacy_dir.exists()
    assert not orphan_dir.exists()
    assert not orphan_mp3.exists()
    assert unrelated.read_bytes() == b"leave alone"


def test_orphan_cleanup_runs_at_worker_startup(job_env):
    _, root = job_env
    orphan_id = str(uuid4())
    orphan_dir = root / "temp" / "jobs" / orphan_id
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "000001.wav").write_bytes(b"orphan")
    audio_dir = root / "temp" / "audio"
    audio_dir.mkdir()
    partial = audio_dir / f"{orphan_id}.partial.mp3"
    partial.write_bytes(b"orphan")
    manager = JobManager(Settings())
    manager.start()
    try:
        end = time.monotonic() + 3
        while time.monotonic() < end and (orphan_dir.exists() or partial.exists()):
            time.sleep(0.02)
        assert not orphan_dir.exists()
        assert not partial.exists()
    finally:
        manager.stop()


def test_audio_and_download_routes_support_browser_ranges(job_env):
    _, root = job_env

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/jobs", json={"text": "Range playback.", "voice": "heart"})
                job_id = created.json()["id"]
                await _wait_status(client, job_id, "completed")
                source = (root / "temp" / "audio" / f"{job_id}.mp3").read_bytes()
                audio_url = f"/api/jobs/{job_id}/audio"
                full = await client.get(audio_url)
                assert full.status_code == 200
                assert full.content == source
                assert full.headers["content-type"] == "audio/mpeg"
                assert full.headers["accept-ranges"] == "bytes"
                assert full.headers["cache-control"] == "no-store"
                assert full.headers["x-content-type-options"] == "nosniff"
                assert full.headers["content-length"] == str(len(source))
                assert full.headers["content-disposition"].startswith("inline;")
                assert str(root) not in str(full.headers)

                download = await client.get(f"/api/jobs/{job_id}/download")
                assert download.status_code == 200
                assert download.content == source
                assert download.headers["content-disposition"] == f'attachment; filename="edspeech-{job_id}.mp3"'

                first = await client.get(audio_url, headers={"range": "bytes=0-99"})
                assert first.status_code == 206
                assert first.content == source[:100]
                assert first.headers["content-range"] == f"bytes 0-99/{len(source)}"
                assert first.headers["content-length"] == "100"
                suffix = await client.get(audio_url, headers={"range": "bytes=-50"})
                assert suffix.status_code == 206
                assert suffix.content == source[-50:]
                rest = await client.get(audio_url, headers={"range": "bytes=100-"})
                assert rest.status_code == 206
                assert rest.content == source[100:]
                for value in (f"bytes={len(source)}-", "bytes=10-5", "bytes=0-1,4-5", "bytes=-0"):
                    invalid = await client.get(audio_url, headers={"range": value})
                    assert invalid.status_code == 416
                    assert invalid.headers["content-range"] == f"bytes */{len(source)}"

    asyncio.run(run())


def test_audio_routes_reject_expired_or_missing_files(job_env, monkeypatch):
    _, root = job_env
    monkeypatch.setenv("AUDIO_TTL_MINUTES", "0.001")
    monkeypatch.setattr("app.main.JobManager", lambda settings: JobManager(settings, 3600))

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/jobs", json={"text": "Expires.", "voice": "heart"})
                job_id = created.json()["id"]
                await _wait_status(client, job_id, "completed")
                final = root / "temp" / "audio" / f"{job_id}.mp3"
                await asyncio.sleep(0.09)
                assert (await client.get(f"/api/jobs/{job_id}/audio")).status_code == 410
                assert (await client.get(f"/api/jobs/{job_id}/download")).status_code == 410
                assert not final.exists()

    asyncio.run(run())


def test_audio_routes_report_not_ready_failed_and_unknown(job_env):
    engine, root = job_env
    engine.block_on = "Wait."
    engine.fail_on = "Fail."

    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                waiting = await client.post("/api/jobs", json={"text": "Wait.", "voice": "heart"})
                waiting_id = waiting.json()["id"]
                assert await asyncio.to_thread(engine.entered.wait, 3)
                not_ready = await client.get(f"/api/jobs/{waiting_id}/audio")
                assert not_ready.status_code == 409
                assert not_ready.json() == {"detail": "Audio is still being generated."}
                engine.release.set()
                await _wait_status(client, waiting_id, "completed")
                final = root / "temp" / "audio" / f"{waiting_id}.mp3"
                final.unlink()
                missing = await client.get(f"/api/jobs/{waiting_id}/audio")
                assert missing.status_code == 410
                failed = await client.post("/api/jobs", json={"text": "Fail.", "voice": "heart"})
                failed_id = failed.json()["id"]
                await _wait_status(client, failed_id, "failed")
                error = await client.get(f"/api/jobs/{failed_id}/download")
                assert error.status_code == 409
                assert "secret document" not in error.text
                assert (await client.get("/api/jobs/not-a-uuid/audio")).status_code == 404
                assert (await client.get(f"/api/jobs/{uuid4()}/audio")).status_code == 404

    try:
        asyncio.run(run())
    finally:
        engine.release.set()


def test_validation_errors_are_safe_strings(job_env):
    async def run():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                missing = await client.post("/api/jobs", json={"voice": "heart"})
                assert missing.status_code == 422
                assert isinstance(missing.json()["detail"], str)
                assert "text" in missing.json()["detail"]
                invalid = await client.post(
                    "/api/jobs",
                    content=b'{"text":"private words","voice":"heart","generation_speed":Infinity}',
                    headers={"content-type": "application/json"},
                )
                assert invalid.status_code == 422
                assert isinstance(invalid.json()["detail"], str)
                assert "private words" not in invalid.text

    asyncio.run(run())


def test_open_audio_stream_survives_file_deletion(job_env):
    _, root = job_env
    manager = JobManager(Settings())
    job_id = manager.store.create(["Stream race."], "heart", 1.0)["id"]
    manager.store.claim_next()
    manager.store.mark_assembling(job_id)
    final = root / "temp" / "audio" / f"{job_id}.mp3"
    final.parent.mkdir(parents=True)
    payload = b"M" * 150_000
    final.write_bytes(payload)
    manager.store.complete(job_id, 1.0, final, timedelta(minutes=30))
    app.state.jobs = manager
    request = Request({
        "type": "http", "app": app, "headers": [], "method": "GET",
        "path": f"/api/jobs/{job_id}/audio", "query_string": b"",
    })

    async def consume():
        response = _audio_response(job_id, request, download=False)
        first = await anext(response.body_iterator)
        assert manager.delete_job(job_id)
        assert not final.exists()
        remaining = [block async for block in response.body_iterator]
        assert first + b"".join(remaining) == payload
        await response.background()

    asyncio.run(consume())
