"""Portable configuration shared by native development and containers."""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # The root .env also contains frontend/Compose settings.
    )

    model_dir: Path = Path("models")
    temp_dir: Path = Path("data/temp")
    database_dir: Path = Path("data/database")
    kokoro_model_path: Path | None = None
    kokoro_voices_path: Path | None = None
    tts_threads: int = Field(default=2, ge=1, le=32)

    @field_validator("model_dir", "temp_dir", "database_dir", mode="before")
    @classmethod
    def resolve_directory(cls, value: str | Path) -> Path:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Directory settings must not be empty")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()

    @field_validator("kokoro_model_path", "kokoro_voices_path", mode="before")
    @classmethod
    def resolve_optional_path(cls, value: str | Path | None) -> Path | None:
        return None if value is None else cls.resolve_directory(value)
