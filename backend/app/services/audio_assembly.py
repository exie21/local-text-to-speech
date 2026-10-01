"""Assemble server-named WAV chunks into one private MP3 with FFmpeg."""

from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
from uuid import UUID


class AssemblyError(Exception):
    """Safe, user-facing assembly failure without filenames or document text."""


def output_path(temp_dir: Path, job_id: str) -> Path:
    # The ID comes from our own SQLite rows, but validate before forming a path.
    if str(UUID(job_id)) != job_id:
        raise AssemblyError("Audio job identifier is invalid.")
    return temp_dir / "audio" / f"{job_id}.mp3"


def assemble_chunks(temp_dir: Path, job_id: str, total_chunks: int) -> tuple[Path, float]:
    """Publish only after FFmpeg and ffprobe both succeed.

    The caller retains WAVs until it commits the completed job in SQLite.
    """
    if total_chunks < 1:
        raise AssemblyError("Audio chunks are missing.")
    final = output_path(temp_dir, job_id)
    chunk_dir = temp_dir / "jobs" / job_id
    names = [f"{number:06d}.wav" for number in range(1, total_chunks + 1)]
    if not all((chunk_dir / name).is_file() for name in names):
        raise AssemblyError("Audio chunks are missing.")

    final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = final.with_name(f"{job_id}.partial.mp3")
    manifest = chunk_dir / "chunks.ffconcat"
    try:
        fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("ffconcat version 1.0\n")
            for name in names:
                output.write(f"file '{name}'\n")

        # Pre-create with owner-only permissions; FFmpeg truncates this file.
        os.close(os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "1", "-i", str(manifest), "-vn",
             "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", "-threads", "1",
             str(staged)],
            check=True, capture_output=True, timeout=3600,
        )
        if not staged.is_file() or staged.stat().st_size == 0:
            raise AssemblyError("Audio assembly produced no MP3.")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(staged)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        duration = float(probe.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            raise AssemblyError("Audio duration could not be determined.")
        os.replace(staged, final)
        return final, duration
    except FileNotFoundError as error:
        if error.filename in {"ffmpeg", "ffprobe"}:
            raise AssemblyError("FFmpeg is unavailable. Install ffmpeg and ffprobe.") from None
        raise AssemblyError("Audio assembly failed. Please try again.") from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError):
        raise AssemblyError("Audio assembly failed. Please try again.") from None
    finally:
        manifest.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
