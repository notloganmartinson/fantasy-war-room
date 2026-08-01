from __future__ import annotations

import json
from pathlib import Path

import pytest

from fantasy_war_room.config import config_file_path, load_settings


def test_precedence_cli_environment_file_defaults(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_file_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"sleeper_username": "file", "season": "2024", "sleeper_league_id": "from-file"})
    )
    monkeypatch.setenv("FWR_SLEEPER_USERNAME", "environment")
    monkeypatch.setenv("FWR_SEASON", "2025")

    settings = load_settings(sleeper_username="cli")

    assert settings.sleeper_username == "cli"
    assert settings.season == "2025"
    assert settings.sleeper_league_id == "from-file"
    assert settings.poll_seconds == 2.0


def test_default_paths_are_xdg_and_absolute(xdg: Path) -> None:
    settings = load_settings()
    assert str(settings.db_path).startswith(str(xdg.parent / "data"))
    assert settings.db_path.is_absolute()
