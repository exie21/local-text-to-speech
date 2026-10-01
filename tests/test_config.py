import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


@pytest.fixture(autouse=True)
def clear_path_environment(monkeypatch):
    for name in ("MODEL_DIR", "TEMP_DIR", "DATABASE_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("working_directory", [PROJECT_ROOT, PROJECT_ROOT / "backend"])
def test_defaults_and_relative_overrides_are_independent_of_cwd(monkeypatch, working_directory):
    monkeypatch.chdir(working_directory)
    defaults = Settings(_env_file=None)
    assert defaults.model_dir == PROJECT_ROOT / "models"
    assert defaults.temp_dir == PROJECT_ROOT / "data/temp"
    assert defaults.database_dir == PROJECT_ROOT / "data/database"

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
