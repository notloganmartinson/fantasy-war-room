from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import httpx
import respx
from typer.testing import CliRunner

from fantasy_war_room.bootstrap import resolve_mcp_launch_spec
from fantasy_war_room.cli import app
from fantasy_war_room.config import ActiveDraftSession, Settings, load_settings, save_settings
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.service import DraftCopilotService
from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import SnapshotRepository


def _body(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)["data"]


def test_clean_user_onboard_requires_username(runner: CliRunner, xdg: Path) -> None:
    result = runner.invoke(app, ["onboard", "--json"])
    assert result.exit_code == 0
    data = _body(result)
    assert data["state"] == "needs_input"
    assert data["question"]["id"] == "sleeper_username"
    assert data["sleeper_account"] == {"username": None, "user_id": None}


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_onboard_multiple_leagues_returns_structured_question(
    api: Any, runner: CliRunner, xdg: Path
) -> None:
    base = "https://api.sleeper.app/v1"
    api.get(f"{base}/user/alice").mock(
        return_value=httpx.Response(200, json={"user_id": "u1", "username": "alice"})
    )
    api.get(f"{base}/user/u1/leagues/nfl/2026").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"league_id": "l1", "name": "One", "total_rosters": 10},
                {"league_id": "l2", "name": "Two", "total_rosters": 12},
            ],
        )
    )
    result = runner.invoke(app, ["onboard", "--username", "alice", "--json"])
    data = _body(result)
    assert data["state"] == "needs_input"
    assert data["question"]["id"] == "league"
    assert {choice["league_id"] for choice in data["question"]["choices"]} == {"l1", "l2"}


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_connect_standalone_url_preserves_separate_scoring_context(
    api: Any, runner: CliRunner, xdg: Path
) -> None:
    base = "https://api.sleeper.app/v1"
    draft = {
        "draft_id": "mock-77",
        "league_id": None,
        "season": "2026",
        "type": "snake",
        "settings": {"teams": 10, "rounds": 15},
        "draft_order": {"u1": 4},
    }
    league = {
        "league_id": "l1",
        "season": "2026",
        "scoring_settings": {"rec": 1},
        "roster_positions": ["QB", "RB", "WR", "TE", "FLEX"],
    }
    api.get(f"{base}/draft/mock-77").mock(return_value=httpx.Response(200, json=draft))
    api.get(f"{base}/draft/mock-77/picks").mock(return_value=httpx.Response(200, json=[]))
    api.get(f"{base}/league/l1").mock(return_value=httpx.Response(200, json=league))
    save_settings(Settings(sleeper_username="alice", sleeper_user_id="u1"))

    result = runner.invoke(
        app,
        [
            "drafts",
            "connect",
            "https://sleeper.com/draft/nfl/mock-77",
            "--scoring-context-league-id",
            "l1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    session = load_settings().active_draft_session
    assert session is not None
    assert session.draft_id == "mock-77"
    assert session.context_type == "standalone"
    assert session.source_league_id is None
    assert session.scoring_context_league_id == "l1"
    assert session.draft_slot == 4


def test_watch_defaults_to_active_mock(runner: CliRunner, xdg: Path, monkeypatch: Any) -> None:
    selected: dict[str, Any] = {}
    save_settings(
        Settings(
            active_draft_session=ActiveDraftSession(
                draft_id="mock-1",
                context_type="standalone",
                season="2026",
                scoring_context_league_id="l1",
            )
        )
    )

    def fake_watch(*args: Any, **kwargs: Any) -> None:
        selected.update(draft_id=args[2], scoring_context=args[4])

    monkeypatch.setattr("fantasy_war_room.cli.watch_by_draft_id", fake_watch)
    assert runner.invoke(app, ["watch"]).exit_code == 0
    assert selected == {"draft_id": "mock-1", "scoring_context": "l1"}


def test_basic_mcp_draft_state_does_not_require_intelligence(tmp_path: Path) -> None:
    repository = SnapshotRepository(tmp_path / "basic.duckdb")
    repository.insert(
        Snapshot(
            snapshot_id="s1",
            league_id="l1",
            draft_id="d1",
            observed_at="2026-08-01T00:00:00Z",
            source_updated_at=None,
            payload_hash="hash",
            pick_count=1,
            league={},
            draft={
                "draft_id": "d1",
                "type": "snake",
                "settings": {"teams": 10, "rounds": 15},
                "draft_order": {"u1": 3},
            },
            picks=[
                {
                    "pick_no": 1,
                    "round": 1,
                    "draft_slot": 1,
                    "player_id": "p1",
                    "metadata": {"first_name": "Test", "last_name": "Player"},
                }
            ],
        )
    )
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="d1",
        sleeper_user_id="u1",
        draft_slot=None,
        default_source="missing",
        default_model="portable-market-1.0",
    )
    state, provenance = service.get_draft_state(as_of="2026-08-01T01:00:00Z")
    assert state["draft_id"] == "d1"
    assert state["user_slot"] == 3
    assert state["completed_pick_count"] == 1
    assert provenance["intelligence_required"] is False
    try:
        service.recommend_pick(model=None, source=None, limit=10, as_of="2026-08-01T01:00:00Z")
    except Exception as error:
        assert error.code in {
            "incompatible_scoring_context",
            "missing_player_directory",
            "missing_compatible_market_board",
        }
    else:
        raise AssertionError("recommend_pick unexpectedly succeeded without intelligence")


def test_launch_spec_uses_active_mock_not_context_league_draft(tmp_path: Path) -> None:
    from test_recommend_integration import _fixture

    repository = _fixture(tmp_path)
    with duckdb.connect(str(repository.path)) as connection:
        row = connection.execute(
            "SELECT * FROM draft_snapshots WHERE draft_id='mock-1' LIMIT 1"
        ).fetchone()
    assert row is not None
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        sleeper_league_id="league-1",
        league_contexts={
            "league-1": {
                "league_id": "league-1",
                "season": "2026",
                "ranking_source": "rotoworld",
                "recommendation_model": "baseline-1.0",
            }
        },
        active_draft_session=ActiveDraftSession(
            draft_id="mock-1",
            context_type="standalone",
            season="2026",
            scoring_context_league_id="league-1",
            draft_slot=2,
        ),
    )
    spec = resolve_mcp_launch_spec(settings, repository_root=tmp_path / "current")
    assert spec.draft_id == "mock-1"
    assert spec.working_directory == str((tmp_path / "current").resolve())
    assert spec.arguments[spec.arguments.index("--draft-id") + 1] == "mock-1"
