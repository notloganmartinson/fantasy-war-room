from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("FWR_SLEEPER_USERNAME", raising=False)
    monkeypatch.delenv("FWR_SLEEPER_LEAGUE_ID", raising=False)
    monkeypatch.delenv("FWR_SEASON", raising=False)
    monkeypatch.delenv("FWR_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    return config


@pytest.fixture
def api(respx_mock: Any) -> Any:
    return respx_mock


@pytest.fixture
def sleeper_payloads() -> dict[str, Any]:
    return {
        "user": {"user_id": "u1", "username": "alice"},
        "league": {
            "league_id": "l1",
            "name": "Friends",
            "status": "drafting",
            "draft_id": "d1",
            "total_rosters": 10,
        },
        "draft": {
            "draft_id": "d1",
            "league_id": "l1",
            "created": 1000,
            "status_updated": 1_700_000_000_000,
            "type": "snake",
            "season": "2026",
            "settings": {"teams": 10, "rounds": 15},
        },
        "mock_draft": {
            "draft_id": "mock1",
            "league_id": None,
            "created": 1001,
            "status": "drafting",
            "type": "snake",
            "season": "2026",
            "settings": {"teams": 10, "rounds": 15},
            "metadata": {"name": "Standalone mock"},
        },
        "picks": [
            {
                "pick_no": 1,
                "round": 1,
                "draft_slot": 1,
                "roster_id": "1",
                "picked_by": "u1",
                "player_id": "p1",
                "metadata": {
                    "first_name": "A",
                    "last_name": "Player",
                    "position": "QB",
                    "team": "BUF",
                },
            }
        ],
    }


def register_sleeper(
    api: Any, payloads: dict[str, Any], picks: list[dict[str, Any]] | None = None
) -> None:
    base = "https://api.sleeper.app/v1"
    api.get(f"{base}/user/alice").mock(return_value=_response(200, payloads["user"]))
    api.get(f"{base}/user/u1/leagues/nfl/2026").mock(
        return_value=_response(200, [payloads["league"]])
    )
    api.get(f"{base}/user/u1/drafts/nfl/2026").mock(
        return_value=_response(200, [payloads["draft"], payloads["mock_draft"]])
    )
    api.get(f"{base}/league/l1").mock(return_value=_response(200, payloads["league"]))
    api.get(f"{base}/league/l1/drafts").mock(return_value=_response(200, [payloads["draft"]]))
    api.get(f"{base}/draft/d1").mock(return_value=_response(200, payloads["draft"]))
    api.get(f"{base}/draft/d1/picks").mock(return_value=_response(200, picks or payloads["picks"]))
    api.get(f"{base}/draft/mock1").mock(return_value=_response(200, payloads["mock_draft"]))
    api.get(f"{base}/draft/mock1/picks").mock(return_value=_response(200, []))
    api.get(f"{base}/players/nfl").mock(
        return_value=_response(
            200,
            {
                "p1": {
                    "first_name": "A",
                    "last_name": "Player",
                    "position": "QB",
                    "team": "BUF",
                    "fantasy_positions": ["QB"],
                }
            },
        )
    )


def _response(status: int, payload: Any) -> Any:
    import httpx

    return httpx.Response(status, json=payload)


def parse_output(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)
