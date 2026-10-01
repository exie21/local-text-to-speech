from concurrent.futures import ThreadPoolExecutor
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import numpy as np
import pytest

from app.config import Settings
from app.services.tts.base import InvalidTTSInput, TTSError
from app.services.tts.kokoro import KokoroEngine
from app.services.tts import registry


def settings_for(tmp_path):
    return Settings(
        _env_file=None,
        model_dir=tmp_path,
        kokoro_model_path=tmp_path / "model.onnx",
        kokoro_voices_path=tmp_path / "voices.bin",
        tts_threads=2,
    )


@pytest.mark.parametrize("text,voice,speed", [
    ("", "heart", 1.0),
    ("   ", "heart", 1.0),
    ("Hello", "unknown", 1.0),
    ("Hello", "heart", float("nan")),
    ("Hello", "heart", float("inf")),
    ("Hello", "heart", 0.4),
    ("Hello", "heart", 2.1),
    ("Hello", "heart", True),
])
def test_invalid_input_is_rejected_before_model_loading(tmp_path, text, voice, speed):
    with pytest.raises(InvalidTTSInput):
        KokoroEngine(settings_for(tmp_path)).synthesize(text, voice, speed)


def test_missing_model_health_is_useful_and_does_not_leak_paths(tmp_path):
    health = KokoroEngine(settings_for(tmp_path)).health()
    assert not health.ready
    assert "model setup" in health.message
    assert str(tmp_path) not in health.message


@pytest.fixture
def fake_runtime(monkeypatch, tmp_path):
    (tmp_path / "model.onnx").touch()
    (tmp_path / "voices.bin").touch()
    runtime = SimpleNamespace(
        disable_telemetry_events=Mock(),
        SessionOptions=SimpleNamespace,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=Mock(return_value=object()),
    )
    vendor = Mock()
    vendor.voices = MagicMock()
    vendor.voices.__getitem__.return_value = np.ones((510, 1, 256), dtype=np.float32)
    vendor.get_voices.return_value = ["af_heart"]
    vendor.create.return_value = (np.array([0.1, -0.1], dtype=np.float32), 24000)
    factory = Mock(return_value=vendor)
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=SimpleNamespace(from_session=factory)))
    return runtime, factory, vendor


def test_concurrent_calls_load_once_and_serialize_inference(tmp_path, fake_runtime):
    runtime, factory, vendor = fake_runtime
    bank = vendor.voices
    active = 0
    peak = 0

    def create(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.005)
        active -= 1
        return np.array([0.1, -0.1], dtype=np.float32), 24000

    vendor.create.side_effect = create
    engine = KokoroEngine(settings_for(tmp_path))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: engine.synthesize("Hello", "heart", 1.25), range(8)))

    assert all(result.sample_rate == 24000 for result in results)
    assert factory.call_count == 1
    assert runtime.InferenceSession.call_count == 1
    assert peak == 1
    assert bank.__getitem__.call_count == 1
    bank.close.assert_called_once()
    assert engine.list_voices()[0].id == "heart"
    assert vendor.create.call_args.kwargs == {"voice": "af_heart", "speed": 1.25, "lang": "en-us"}
    assert runtime.InferenceSession.call_args.kwargs["providers"] == ["CPUExecutionProvider"]
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"


def test_load_failure_is_sanitized_and_can_be_retried(tmp_path, fake_runtime):
    runtime, _, _ = fake_runtime
    engine = KokoroEngine(settings_for(tmp_path))
    runtime.InferenceSession.side_effect = RuntimeError(f"Corrupt model at {tmp_path}")
    health = engine.health()
    assert not health.ready
    assert str(tmp_path) not in health.message
    runtime.InferenceSession.side_effect = None
    assert engine.health().ready


def test_loaded_voice_metadata_does_not_wait_for_inference_lock(tmp_path, fake_runtime):
    engine = KokoroEngine(settings_for(tmp_path))
    assert engine.list_voices()[0].id == "heart"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine._lock:
            assert pool.submit(engine.list_voices).result(timeout=0.5)[0].id == "heart"


@pytest.mark.parametrize("output", [np.array([]), np.array([float("nan")]), np.zeros((2, 2))])
def test_invalid_audio_is_not_returned(tmp_path, fake_runtime, output):
    _, _, vendor = fake_runtime
    vendor.create.return_value = (output, 24000)
    with pytest.raises(TTSError, match="Speech generation failed"):
        KokoroEngine(settings_for(tmp_path)).synthesize("Hello", "heart")


def test_synthesis_exception_does_not_expose_private_text(tmp_path, fake_runtime):
    _, _, vendor = fake_runtime
    vendor.create.side_effect = RuntimeError("private text from a document")
    with pytest.raises(TTSError) as error:
        KokoroEngine(settings_for(tmp_path)).synthesize("private text from a document", "heart")
    assert "private text" not in str(error.value)


def test_registry_reuses_one_instance_on_concurrent_first_access(monkeypatch):
    sentinel = object()
    factory = Mock(return_value=sentinel)
    monkeypatch.setattr(registry, "_engine", None)
    monkeypatch.setattr("app.services.tts.kokoro.KokoroEngine", factory)
    with ThreadPoolExecutor(max_workers=4) as pool:
        engines = list(pool.map(lambda _: registry.get_tts_engine(), range(8)))
    assert all(engine is sentinel for engine in engines)
    assert factory.call_count == 1
