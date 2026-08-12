from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fantasy_war_room.config import app_dirs
from fantasy_war_room.errors import ConfigurationError, NotFoundError
from fantasy_war_room.strategy.models import StrategyProfile

DEFAULT_PROFILE_NAME = "logan-ppr-2flex-1.0"


def default_strategy_profile() -> StrategyProfile:
    return _read_profile(Path(__file__).parent / "profiles" / f"{DEFAULT_PROFILE_NAME}.json")


def strategy_directory() -> Path:
    return Path(app_dirs().user_config_dir) / "strategies"


def load_strategy_profile(selector: str | Path) -> StrategyProfile:
    value = str(selector)
    path = Path(value).expanduser()
    if path.is_file():
        return _read_profile(path)
    configured = strategy_directory() / f"{value}.json"
    if configured.is_file():
        return _read_profile(configured)
    if value == DEFAULT_PROFILE_NAME:
        return default_strategy_profile()
    raise NotFoundError(
        "Strategy profile was not found",
        {"strategy": value, "searched_path": str(configured)},
        code="strategy_not_found",
    )


def profile_hash(profile: StrategyProfile) -> str:
    encoded = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _read_profile(path: Path) -> StrategyProfile:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return StrategyProfile.model_validate(payload)
    except Exception as error:
        raise ConfigurationError(
            "invalid_strategy_profile",
            "Strategy profile is not valid",
            {"path": str(path), "error": str(error)},
        ) from error
