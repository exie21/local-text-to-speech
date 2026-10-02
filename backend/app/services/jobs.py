"""SQLite-backed FIFO speech jobs and a single local synthesis worker."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
from threading import Event, Thread
from uuid import UUID, uuid4

from app.config import Settings
from app.services.audio_assembly import AssemblyError, assemble_chunks, output_path
from app.services.tts.audio import encode_wav
from app.services.tts.base import TTSError
from app.services.tts.registry import get_tts_engine

_LOG = logging.getLogger(__name__)
_INTERRUPTED = "Generation was interrupted. Please submit the text again."
_UNEXPECTED = "Speech generation failed. Please try again."
_STATUSES = "'queued','processing','assembling','completed','failed','expired'"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Short-lived SQLite connections keep HTTP and worker threads independent."""

    def __init__(self, database_dir: Path):
        database_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = database_dir / "jobs.sqlite3"
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
        except FileExistsError:
            pass
        with self._connect() as db:
            db.executescript(f"""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ({_STATUSES})),
                    voice TEXT NOT NULL,
                    generation_speed REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    expires_at TEXT,
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    current_chunk INTEGER NOT NULL DEFAULT 0,
                    total_chunks INTEGER NOT NULL CHECK(total_chunks > 0),
                    audio_path TEXT,
                    audio_duration REAL,
                    error TEXT,
                    chunks_json TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at, id);
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs((1))
                    WHERE status IN ('processing', 'assembling');
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA secure_delete=ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, chunks: list[str], voice: str, speed: float) -> dict:
        job_id = str(uuid4())
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO jobs (id, status, voice, generation_speed, created_at,
                   updated_at, total_chunks, chunks_json)
                   VALUES (?, 'queued', ?, ?, ?, ?, ?, ?)""",
                (job_id, voice, speed, now, now, len(chunks), json.dumps(chunks, ensure_ascii=False)),
            )
        return {"id": job_id, "status": "queued"}

    def get(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id, status, voice, generation_speed, created_at, updated_at,
                   completed_at, expires_at, progress, current_chunk, total_chunks,
                   audio_duration, error FROM jobs WHERE id = ?""", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def claim_next(self) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('processing','assembling') LIMIT 1").fetchone():
                return None
            # Phase 6 completed rows contain WAV chunks but no MP3. Migrate
            # them through the same assembly step before accepting new work.
            legacy = db.execute(
                """SELECT * FROM jobs WHERE status = 'completed' AND audio_path IS NULL
                   ORDER BY created_at, rowid LIMIT 1"""
            ).fetchone()
            if legacy is not None:
                db.execute(
                    """UPDATE jobs SET status = 'assembling', updated_at = ?,
                       completed_at = NULL, audio_duration = NULL WHERE id = ?""",
                    (_now(), legacy["id"]),
                )
                return dict(legacy)
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status = 'processing', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            return dict(row)

    def mark_progress(self, job_id: str, chunk_number: int, total: int) -> None:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE jobs SET current_chunk = ?, progress = ?, updated_at = ?
                   WHERE id = ? AND status = 'processing'""",
                (chunk_number, int(chunk_number * 100 / total), _now(), job_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Job was removed during generation")

    def mark_assembling(self, job_id: str) -> None:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE jobs SET status = 'assembling', updated_at = ?
                   WHERE id = ? AND status = 'processing'""",
                (_now(), job_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Job was not processing")

    def complete(self, job_id: str, duration: float, audio_path: Path, ttl: timedelta) -> None:
        completed = datetime.now(timezone.utc)
        now = completed.isoformat()
        expires = (completed + ttl).isoformat()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE jobs SET status = 'completed', progress = 100, updated_at = ?,
                   completed_at = ?, expires_at = ?, audio_duration = ?, audio_path = ?,
                   error = NULL, chunks_json = NULL
                   WHERE id = ? AND status = 'assembling'""",
                (now, now, expires, duration, str(audio_path), job_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Job was not assembling")

    def fail(self, job_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE jobs SET status = 'failed', error = ?, updated_at = ?,
                   completed_at = NULL, audio_path = NULL, audio_duration = NULL,
                   chunks_json = NULL WHERE id = ? AND status IN ('processing','assembling')""",
                (error, _now(), job_id),
            )

    def recover_interrupted(self) -> list[str]:
        """A dead worker cannot resume its partial generation safely."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM jobs WHERE status IN ('processing','assembling')"
            ).fetchall()
            ids = [row["id"] for row in rows]
            db.execute(
                """UPDATE jobs SET status = 'failed', error = ?, updated_at = ?,
                   completed_at = NULL, audio_path = NULL, audio_duration = NULL,
                   chunks_json = NULL WHERE status IN ('processing','assembling')""",
                (_INTERRUPTED, _now()),
            )
        return ids

    def completed_ids(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM jobs WHERE status = 'completed' AND audio_path IS NOT NULL"
            ).fetchall()
        return [row["id"] for row in rows]

    def legacy_chunk_ids(self) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id FROM jobs WHERE status = 'completed' AND audio_path IS NULL"""
            ).fetchall()
        return {row["id"] for row in rows}

    def backfill_expirations(self, ttl: timedelta) -> None:
        """Phase 7 completed MP3s predate the expiration column's use."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT id, completed_at FROM jobs WHERE status = 'completed'
                   AND audio_path IS NOT NULL AND expires_at IS NULL"""
            ).fetchall()
            for row in rows:
                try:
                    completed = datetime.fromisoformat(row["completed_at"])
                    if completed.tzinfo is None:
                        completed = completed.replace(tzinfo=timezone.utc)
                    expiration = completed + ttl
                except (TypeError, ValueError, OverflowError):
                    expiration = datetime.now(timezone.utc)
                db.execute(
                    "UPDATE jobs SET expires_at = ? WHERE id = ?",
                    (expiration.isoformat(), row["id"]),
                )

    def expire_due(self, now: str, job_id: str | None = None) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            params: tuple = (now,)
            filter_id = ""
            if job_id is not None:
                filter_id = " AND id = ?"
                params = (now, job_id)
            rows = db.execute(
                """SELECT id FROM jobs WHERE
                   ((status = 'completed' AND expires_at IS NOT NULL AND expires_at <= ?)
                   OR (status = 'expired' AND audio_path IS NOT NULL))""" + filter_id,
                params,
            ).fetchall()
            ids = [row["id"] for row in rows]
            for item_id in ids:
                db.execute(
                    """UPDATE jobs SET status = 'expired', updated_at = ?
                       WHERE id = ? AND status = 'completed'""",
                    (now, item_id),
                )
        return ids

    def clear_expired_audio(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET audio_path = NULL WHERE id = ? AND status = 'expired'",
                (job_id,),
            )

    def audio_record(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status, audio_path FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete(self, job_id: str) -> bool:
        with self._connect() as db:
            changed = db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return changed.rowcount == 1

    def cancel(self, job_id: str) -> bool:
        now = _now()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE jobs SET status = 'expired', updated_at = ?,
                   expires_at = ?, chunks_json = NULL WHERE id = ?""",
                (now, now, job_id),
            )
        return changed.rowcount == 1


class JobManager:
    def __init__(self, settings: Settings, cleanup_interval_seconds: float = 60):
        self.store = JobStore(settings.database_dir)
        self.store.backfill_expirations(timedelta(minutes=settings.audio_ttl_minutes))
        self.temp_dir = settings.temp_dir
        self._ttl = timedelta(minutes=settings.audio_ttl_minutes)
        self._cleanup_interval = cleanup_interval_seconds
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._cleanup_thread: Thread | None = None
        self._lock_path = settings.database_dir / "jobs.worker.lock"

    def start(self) -> None:
        self._cleanup_thread = Thread(target=self._run_cleanup, name="edspeech-expiration", daemon=True)
        self._cleanup_thread.start()
        self._thread = Thread(target=self._run, name="edspeech-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()
        if self._cleanup_thread:
            self._cleanup_thread.join()

    def notify(self) -> None:
        self._wake.set()

    def expire_due(self, job_id: str | None = None) -> None:
        for item_id in self.store.expire_due(_now(), job_id):
            try:
                self._clean_output(item_id)
                self.store.clear_expired_audio(item_id)
            except OSError:
                # The expired state already blocks access; retry file cleanup
                # on the next timer tick or request.
                _LOG.error("Expired audio could not be removed; will retry.")

    def get_status(self, job_id: str) -> dict | None:
        self.expire_due(job_id)
        return self.store.get(job_id)

    def live_audio_path(self, job_id: str) -> Path | None:
        """Phase 9 playback/download handlers must call this on each request."""
        self.expire_due(job_id)
        record = self.store.audio_record(job_id)
        if record is None or record["status"] != "completed":
            return None
        expected = output_path(self.temp_dir, job_id)
        if record["audio_path"] != str(expected) or not expected.is_file():
            return None
        return expected

    def delete_job(self, job_id: str) -> bool:
        if not self.store.cancel(job_id):
            return False
        self._clean_chunks(job_id)
        self._clean_output(job_id)
        self.store.delete(job_id)
        return True

    def _run_cleanup(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.backfill_expirations(self._ttl)
                self.expire_due()
            except Exception:
                _LOG.error("Audio expiration check failed; will retry.")
            self._stop.wait(self._cleanup_interval)

    def _run(self) -> None:
        """An OS file lock elects one worker across processes using this DB."""
        try:
            lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(lock_fd, "a+b") as lock_file:
                while not self._stop.is_set():
                    try:
                        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        self._stop.wait(1)
                if self._stop.is_set():
                    return
                try:
                    recovery_pending = True
                    while not self._stop.is_set():
                        try:
                            if recovery_pending:
                                for job_id in self.store.recover_interrupted():
                                    self._clean_chunks(job_id)
                                    self._clean_output(job_id)
                                self.store.backfill_expirations(self._ttl)
                                self.expire_due()
                                self._clean_orphans()
                                # A crash just after the DB commit may leave
                                # source WAVs despite a usable final MP3.
                                for job_id in self.store.completed_ids():
                                    self._clean_chunks(job_id)
                                recovery_pending = False
                            job = self.store.claim_next()
                            if job is None:
                                self._wake.wait(1)
                                self._wake.clear()
                            else:
                                self._process(job)
                        except Exception:
                            # Keep the worker alive after a transient SQLite or file error.
                            _LOG.error("Job worker loop failed; retrying.")
                            recovery_pending = True
                            self._stop.wait(1)
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            _LOG.error("Job worker could not start.")

    def _clean_chunks(self, job_id: str) -> None:
        shutil.rmtree(self.temp_dir / "jobs" / job_id, ignore_errors=True)

    def _clean_orphans(self) -> None:
        """Only remove files with our canonical UUID naming scheme."""
        keep_chunks = self.store.legacy_chunk_ids()
        keep_audio = set(self.store.completed_ids())
        chunks_root = self.temp_dir / "jobs"
        if chunks_root.is_dir():
            for path in chunks_root.iterdir():
                if path.is_symlink() or not path.is_dir():
                    continue
                if _is_job_id(path.name) and path.name not in keep_chunks:
                    shutil.rmtree(path, ignore_errors=True)
        audio_root = self.temp_dir / "audio"
        if audio_root.is_dir():
            for path in audio_root.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                name = path.name
                if name.endswith(".partial.mp3"):
                    job_id = name.removesuffix(".partial.mp3")
                    if _is_job_id(job_id):
                        path.unlink(missing_ok=True)
                elif name.endswith(".mp3"):
                    job_id = name.removesuffix(".mp3")
                    if _is_job_id(job_id) and job_id not in keep_audio:
                        path.unlink(missing_ok=True)

    def _clean_output(self, job_id: str) -> None:
        final = output_path(self.temp_dir, job_id)
        final.unlink(missing_ok=True)
        final.with_name(f"{job_id}.partial.mp3").unlink(missing_ok=True)

    def _process(self, job: dict) -> None:
        job_id = job["id"]
        directory = self.temp_dir / "jobs" / job_id
        try:
            if job["status"] == "queued":
                chunks = json.loads(job["chunks_json"])
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                engine = get_tts_engine()
                for number, text in enumerate(chunks, start=1):
                    if self._stop.is_set():
                        raise RuntimeError("Worker shutting down")
                    audio = engine.synthesize(text, job["voice"], job["generation_speed"])
                    path = directory / f"{number:06d}.wav"
                    partial = directory / f"{number:06d}.part"
                    try:
                        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(fd, "wb") as output:
                            output.write(encode_wav(audio))
                        os.replace(partial, path)
                    finally:
                        partial.unlink(missing_ok=True)
                    self.store.mark_progress(job_id, number, len(chunks))
                self.store.mark_assembling(job_id)
            if self._stop.is_set():
                raise RuntimeError("Worker shutting down")
            final, duration = assemble_chunks(self.temp_dir, job_id, job["total_chunks"])
            self.store.complete(job_id, duration, final, self._ttl)
            self._clean_chunks(job_id)
        except Exception as error:
            message = str(error) if isinstance(error, (TTSError, AssemblyError)) else _UNEXPECTED
            if self._stop.is_set():
                message = _INTERRUPTED
            try:
                self.store.fail(job_id, message)
            finally:
                self._clean_chunks(job_id)
                self._clean_output(job_id)


def _is_job_id(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False
