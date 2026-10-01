"""SQLite-backed FIFO speech jobs and a single local synthesis worker."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
from threading import Event, Thread
from uuid import uuid4

from app.config import Settings
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
            db.execute(
                """UPDATE jobs SET current_chunk = ?, progress = ?, updated_at = ?
                   WHERE id = ? AND status = 'processing'""",
                (chunk_number, int(chunk_number * 100 / total), _now(), job_id),
            )

    def complete(self, job_id: str, duration: float) -> None:
        now = _now()
        with self._connect() as db:
            db.execute(
                """UPDATE jobs SET status = 'completed', progress = 100, updated_at = ?,
                   completed_at = ?, audio_duration = ?, chunks_json = NULL
                   WHERE id = ? AND status = 'processing'""",
                (now, now, duration, job_id),
            )

    def fail(self, job_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE jobs SET status = 'failed', error = ?, updated_at = ?,
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
                   chunks_json = NULL WHERE status IN ('processing','assembling')""",
                (_INTERRUPTED, _now()),
            )
        return ids


class JobManager:
    def __init__(self, settings: Settings):
        self.store = JobStore(settings.database_dir)
        self.temp_dir = settings.temp_dir
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._lock_path = settings.database_dir / "jobs.worker.lock"

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="localreader-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()

    def notify(self) -> None:
        self._wake.set()

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

    def _process(self, job: dict) -> None:
        job_id = job["id"]
        duration = 0.0
        directory = self.temp_dir / "jobs" / job_id
        try:
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
                duration += audio.duration
                self.store.mark_progress(job_id, number, len(chunks))
            self.store.complete(job_id, duration)
        except Exception as error:
            message = str(error) if isinstance(error, TTSError) else _UNEXPECTED
            if self._stop.is_set():
                message = _INTERRUPTED
            try:
                self.store.fail(job_id, message)
            finally:
                self._clean_chunks(job_id)
