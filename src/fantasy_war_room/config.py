from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fantasy_war_room.errors import ConfigurationError

APP_NAME = "fantasy-war-room"


def app_dirs() -> PlatformDirs:
    return PlatformDirs(APP_NAME, appauthor=False)


def config_file_path() -> Path:
    return Path(app_dirs().user_config_dir) / "config.json"


def default_db_path() -> Path:
    return Path(app_dirs().user_data_dir) / "fantasy-war-room.duckdb"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FWR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sleeper_username: str | None = None
    sleeper_user_id: str | None = None
    sleeper_league_id: str | None = None
    season: str = Field(default_factory=lambda: str(datetime.now(UTC).year))
    db_path: Path = Field(default_factory=default_db_path)
    poll_seconds: float = 2.0
    sleeper_base_url: str = "https://api.sleeper.app/v1"
    http_timeout_seconds: float = 10.0
    strategy: str | None = None

    @field_validator("poll_seconds", "http_timeout_seconds")
    @classmethod
    def positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value


def _file_values(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", f"Cannot read configuration: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("invalid_config", "Configuration must be a JSON object")
    return value


def load_settings(**cli_values: Any) -> Settings:
    """Load defaults < file < dotenv/environment < explicit CLI values."""
    file_settings = Settings(**_file_values(config_file_path()))
    environment_settings = Settings()
    merged = file_settings.model_dump()
    fields_set = environment_settings.model_fields_set
    for name in fields_set:
        merged[name] = getattr(environment_settings, name)
    merged.update({key: value for key, value in cli_values.items() if value is not None})
    return Settings(**merged)


def save_settings(settings: Settings) -> Path:
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    persisted = {
        "sleeper_username": settings.sleeper_username,
        "sleeper_user_id": settings.sleeper_user_id,
        "sleeper_league_id": settings.sleeper_league_id,
        "season": settings.season,
        "db_path": str(settings.db_path),
        "poll_seconds": settings.poll_seconds,
        "strategy": settings.strategy,
    }
    path.write_text(json.dumps(persisted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def ensure_directories(settings: Settings) -> None:
    Path(app_dirs().user_config_dir).mkdir(parents=True, exist_ok=True)
    Path(app_dirs().user_data_dir).mkdir(parents=True, exist_ok=True)
    Path(app_dirs().user_cache_dir).mkdir(parents=True, exist_ok=True)
    settings.db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
