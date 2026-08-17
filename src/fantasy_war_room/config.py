from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from platformdirs import PlatformDirs
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fantasy_war_room.errors import ConfigurationError

APP_NAME = "fantasy-war-room"
CONFIG_SCHEMA_VERSION = "2.0"
RecommendationModelSelection = Literal["baseline-1.0", "trusted-board-1.0", "trusted-board-1.1"]


def app_dirs() -> PlatformDirs:
    return PlatformDirs(APP_NAME, appauthor=False)


def config_file_path() -> Path:
    return Path(app_dirs().user_config_dir) / "config.json"


def default_db_path() -> Path:
    return Path(app_dirs().user_data_dir) / "fantasy-war-room.duckdb"


class LeagueContext(BaseModel):
    """Local choices for one league; authoritative details remain in observations."""

    model_config = ConfigDict(frozen=True)
    schema_version: str = "1.0"
    provider: str = "sleeper"
    season: str
    league_id: str
    ranking_source: str | None = None
    recommendation_model: RecommendationModelSelection | None = None
    strategy: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FWR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    config_schema_version: str = CONFIG_SCHEMA_VERSION
    sleeper_username: str | None = None
    sleeper_user_id: str | None = None
    sleeper_league_id: str | None = None
    active_league_id: str | None = None
    league_contexts: dict[str, LeagueContext] = Field(default_factory=dict)
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

    @property
    def active_context(self) -> LeagueContext | None:
        league_id = self.sleeper_league_id or self.active_league_id
        if league_id is None:
            return None
        return self.league_contexts.get(league_id)

    @property
    def active_strategy(self) -> str | None:
        context = self.active_context
        return context.strategy if context is not None else self.strategy

    @property
    def active_ranking_source(self) -> str | None:
        context = self.active_context
        return context.ranking_source if context is not None else None

    @property
    def active_recommendation_model(self) -> str | None:
        context = self.active_context
        return context.recommendation_model if context is not None else None


def _file_values(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", f"Cannot read configuration: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("invalid_config", "Configuration must be a JSON object")
    return _migrate_file_values(value)


def _migrate_file_values(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy single-league configuration without mutating it on read."""
    migrated = dict(value)
    raw_contexts = migrated.get("league_contexts")
    contexts = dict(raw_contexts) if isinstance(raw_contexts, dict) else {}
    legacy_league_id = migrated.get("sleeper_league_id")
    active = migrated.get("active_league_id") or legacy_league_id
    if legacy_league_id and str(legacy_league_id) not in contexts:
        contexts[str(legacy_league_id)] = {
            "provider": "sleeper",
            "season": str(migrated.get("season") or datetime.now(UTC).year),
            "league_id": str(legacy_league_id),
            "strategy": migrated.get("strategy"),
        }
    migrated.update(
        {
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "active_league_id": str(active) if active else None,
            "league_contexts": contexts,
        }
    )
    return migrated


def load_settings(**cli_values: Any) -> Settings:
    """Load defaults < file < dotenv/environment < explicit CLI values."""
    file_settings = Settings(**_file_values(config_file_path()))
    environment_settings = Settings()
    merged = file_settings.model_dump()
    environment_fields = environment_settings.model_fields_set
    for name in environment_fields:
        merged[name] = getattr(environment_settings, name)
    merged.update({key: value for key, value in cli_values.items() if value is not None})
    selected = merged.get("sleeper_league_id") or merged.get("active_league_id")
    if selected:
        merged["active_league_id"] = selected
        context = merged.get("league_contexts", {}).get(selected)
        if (
            context is not None
            and "season" not in environment_fields
            and cli_values.get("season") is None
        ):
            merged["season"] = (
                context.season if isinstance(context, LeagueContext) else context["season"]
            )
    merged["sleeper_league_id"] = selected
    return Settings(**merged)


def save_settings(settings: Settings) -> Path:
    path = config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    persisted = {
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "sleeper_username": settings.sleeper_username,
        "sleeper_user_id": settings.sleeper_user_id,
        "active_league_id": settings.active_league_id,
        "league_contexts": {
            key: context.model_dump(mode="json")
            for key, context in sorted(settings.league_contexts.items())
        },
        "season": settings.season,
        "db_path": str(settings.db_path),
        "poll_seconds": settings.poll_seconds,
    }
    path.write_text(json.dumps(persisted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def with_league_context(
    settings: Settings,
    *,
    league_id: str,
    season: str,
    ranking_source: str | None = None,
    recommendation_model: RecommendationModelSelection | None = None,
    strategy: str | None = None,
    preserve_preferences: bool = True,
) -> Settings:
    contexts = dict(settings.league_contexts)
    previous = contexts.get(league_id) if preserve_preferences else None
    contexts[league_id] = LeagueContext(
        season=season,
        league_id=league_id,
        ranking_source=ranking_source
        if ranking_source is not None
        else (previous.ranking_source if previous else None),
        recommendation_model=(
            recommendation_model
            if recommendation_model is not None
            else (previous.recommendation_model if previous else None)
        ),
        strategy=strategy if strategy is not None else (previous.strategy if previous else None),
    )
    return settings.model_copy(
        update={
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "active_league_id": league_id,
            "sleeper_league_id": league_id,
            "season": season,
            "league_contexts": contexts,
            "strategy": None,
        }
    )


def for_resolved_sleeper_user(
    settings: Settings,
    *,
    user_id: str,
    username: str | None,
) -> Settings:
    """Apply the one-account boundary after Sleeper resolves authoritative identity."""
    changed = settings.sleeper_user_id is not None and settings.sleeper_user_id != user_id
    updates: dict[str, Any] = {
        "sleeper_user_id": user_id,
        "sleeper_username": username or settings.sleeper_username,
    }
    if changed:
        updates.update(
            {
                "active_league_id": None,
                "sleeper_league_id": None,
                "league_contexts": {},
                "strategy": None,
            }
        )
    return settings.model_copy(update=updates)


def ensure_directories(settings: Settings) -> None:
    Path(app_dirs().user_config_dir).mkdir(parents=True, exist_ok=True)
    Path(app_dirs().user_data_dir).mkdir(parents=True, exist_ok=True)
    Path(app_dirs().user_cache_dir).mkdir(parents=True, exist_ok=True)
    settings.db_path.expanduser().parent.mkdir(parents=True, exist_ok=True)
