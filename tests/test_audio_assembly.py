import subprocess

import numpy as np
import pytest

from app.services.audio_assembly import AssemblyError, assemble_chunks
from app.services.tts.audio import encode_wav
from app.services.tts.base import AudioResult


def _write_tone(path, frequency):
    samples = np.arange(9600, dtype=np.float32)
    tone = (0.3 * np.sin(2 * np.pi * frequency * samples / 24000)).astype(np.float32)
    path.write_bytes(encode_wav(AudioResult(tone, 24000)))


def _dominant_frequency(samples):
    spectrum = np.abs(np.fft.rfft(samples))
    frequencies = np.fft.rfftfreq(len(samples), 1 / 24000)
    return frequencies[np.argmax(spectrum)]


def test_two_wavs_become_one_ordered_mp3(tmp_path):
    job_id = "1baaa64e-1e26-495b-a3b0-c62a612848a4"
    chunk_dir = tmp_path / "jobs" / job_id
    chunk_dir.mkdir(parents=True)
    _write_tone(chunk_dir / "000001.wav", 440)
    _write_tone(chunk_dir / "000002.wav", 880)

    final, duration = assemble_chunks(tmp_path, job_id, 2)

    assert final == tmp_path / "audio" / f"{job_id}.mp3"
    assert final.stat().st_size > 1000
    assert 0.75 < duration < 0.95
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(final), "-f", "f32le",
         "-ac", "1", "-ar", "24000", "pipe:1"],
        capture_output=True, check=True,
    )
    audio = np.frombuffer(decoded.stdout, dtype="<f4")
    assert len(audio) >= 19200
    assert abs(_dominant_frequency(audio[2400:7200]) - 440) < 10
    assert abs(_dominant_frequency(audio[12000:16800]) - 880) < 10
    assert not (chunk_dir / "chunks.ffconcat").exists()
    assert not (tmp_path / "audio" / f"{job_id}.partial.mp3").exists()


def test_missing_chunk_fails_without_publishing(tmp_path):
    job_id = "1baaa64e-1e26-495b-a3b0-c62a612848a4"
    chunk_dir = tmp_path / "jobs" / job_id
    chunk_dir.mkdir(parents=True)
    _write_tone(chunk_dir / "000001.wav", 440)

    with pytest.raises(AssemblyError, match="Audio chunks are missing"):
        assemble_chunks(tmp_path, job_id, 2)
    assert not (tmp_path / "audio").exists()


def test_ffmpeg_failure_cleans_staged_files(tmp_path, monkeypatch):
    job_id = "1baaa64e-1e26-495b-a3b0-c62a612848a4"
    chunk_dir = tmp_path / "jobs" / job_id
    chunk_dir.mkdir(parents=True)
    _write_tone(chunk_dir / "000001.wav", 440)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"private path")

    monkeypatch.setattr("app.services.audio_assembly.subprocess.run", fail)
    with pytest.raises(AssemblyError, match="Audio assembly failed") as error:
        assemble_chunks(tmp_path, job_id, 1)
    assert "private path" not in str(error.value)
    assert not (chunk_dir / "chunks.ffconcat").exists()
    assert not list((tmp_path / "audio").iterdir())
