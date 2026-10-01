import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


@pytest.fixture(autouse=True)
def clear_path_environment(monkeypatch):
    for name in ("MODEL_DIR", "TEMP_DIR", "DATABASE_DIR", "TTS_CHUNK_SIZE", "MAX_TEXT_CHARS", "MAX_UPLOAD_BYTES", "AUDIO_TTL_MINUTES"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("working_directory", [PROJECT_ROOT, PROJECT_ROOT / "backend"])
def test_defaults_and_relative_overrides_are_independent_of_cwd(monkeypatch, working_directory):
    monkeypatch.chdir(working_directory)
    defaults = Settings(_env_file=None)
    assert defaults.model_dir == PROJECT_ROOT / "models"
    assert defaults.temp_dir == PROJECT_ROOT / "data/temp"
    assert defaults.database_dir == PROJECT_ROOT / "data/database"
    assert defaults.tts_chunk_size == 800
    assert defaults.max_text_chars == 100_000
    assert defaults.max_upload_bytes == 10_000_000
    assert defaults.audio_ttl_minutes == 30

    monkeypatch.setenv("TEMP_DIR", "data/custom-temp")
    assert Settings(_env_file=None).temp_dir == PROJECT_ROOT / "data/custom-temp"


def test_environment_overrides_dotenv_and_keeps_absolute_paths(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_DIR=local-models\nAPI_PROXY_TARGET=http://localhost:8000\n")
    assert Settings(_env_file=env_file).model_dir == PROJECT_ROOT / "local-models"

    absolute = tmp_path / "override-models"
    monkeypatch.setenv("MODEL_DIR", str(absolute))
    assert Settings(_env_file=env_file).model_dir == absolute
    assert not absolute.exists(), "Reading settings must not create runtime directories"


def test_empty_directory_setting_is_rejected(monkeypatch):
    monkeypatch.setenv("TEMP_DIR", "")
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings(_env_file=None)


@pytest.mark.parametrize("name,value", [
    ("TTS_CHUNK_SIZE", "499"), ("TTS_CHUNK_SIZE", "1001"),
    ("MAX_TEXT_CHARS", "0"), ("MAX_UPLOAD_BYTES", "0"),
    ("AUDIO_TTL_MINUTES", "0"), ("AUDIO_TTL_MINUTES", "1441"),
])
def test_invalid_text_processing_limits_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_text_processing_limits_can_be_configured(monkeypatch):
    monkeypatch.setenv("TTS_CHUNK_SIZE", "900")
    monkeypatch.setenv("MAX_TEXT_CHARS", "250000")
    monkeypatch.setenv("AUDIO_TTL_MINUTES", "0.1")
    settings = Settings(_env_file=None)
    assert settings.tts_chunk_size == 900
    assert settings.max_text_chars == 250_000
    assert settings.audio_ttl_minutes == 0.1
