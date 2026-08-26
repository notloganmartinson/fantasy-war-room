from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import httpx
import pytest
import respx
from typer.testing import CliRunner

from fantasy_war_room.bootstrap import ResolvedMcpLaunchSpec, resolve_mcp_launch_spec
from fantasy_war_room.cli import _agent_status, _validated_agent_state, app
from fantasy_war_room.config import (
    ActiveDraftSession,
    LeagueContext,
    Settings,
    load_settings,
    save_settings,
)
from fantasy_war_room.errors import ConfigurationError
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
    configured = load_settings()
    assert session is not None
    assert session.draft_id == "mock-77"
    assert session.context_type == "standalone"
    assert session.source_league_id is None
    assert session.scoring_context_league_id == "l1"
    assert session.draft_slot == 4
    assert configured.active_league_id is None
    assert configured.draft_configuration_context is not None
    assert configured.draft_configuration_context.league_id == "l1"
    onboard = runner.invoke(app, ["onboard", "--json"])
    assert onboard.exit_code == 0, onboard.stdout
    assert _body(onboard)["question"]["id"] == "intelligence_mode"
    assert load_settings().active_draft_session == session


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


def test_league_switch_invalidates_standalone_session_and_blocks_watch_and_mcp(
    runner: CliRunner, xdg: Path, tmp_path: Path
) -> None:
    save_settings(
        Settings(
            sleeper_username="alice",
            sleeper_user_id="u1",
            active_league_id="a",
            sleeper_league_id="a",
            league_contexts={
                "a": LeagueContext(league_id="a", season="2026"),
                "b": LeagueContext(league_id="b", season="2026"),
            },
            active_draft_session=ActiveDraftSession(
                draft_id="mock-m",
                context_type="standalone",
                season="2026",
                scoring_context_league_id="a",
            ),
        )
    )

    switched = runner.invoke(app, ["leagues", "use", "b", "--json"])
    assert switched.exit_code == 0
    settings = load_settings()
    assert settings.active_league_id == "b"
    assert settings.active_draft_session is None
    assert settings.active_draft_session_invalidated is True
    watched = runner.invoke(app, ["watch"])
    assert watched.exit_code != 0
    assert "invalidated" in watched.output
    with pytest.raises(ConfigurationError) as raised:
        resolve_mcp_launch_spec(settings, repository_root=tmp_path)
    assert raised.value.code == "codex_context_incomplete"
    status = _body(runner.invoke(app, ["onboard", "--json"]))
    assert status["state"] == "needs_action"
    assert status["next_actions"][0]["id"] == "sync_draft"


def test_league_switch_invalidates_other_league_draft(runner: CliRunner, xdg: Path) -> None:
    save_settings(
        Settings(
            sleeper_username="alice",
            sleeper_user_id="u1",
            active_league_id="a",
            sleeper_league_id="a",
            league_contexts={
                "a": LeagueContext(league_id="a", season="2026"),
                "b": LeagueContext(league_id="b", season="2026"),
            },
            active_draft_session=ActiveDraftSession(
                draft_id="draft-a",
                context_type="league",
                season="2026",
                source_league_id="a",
                scoring_context_league_id="a",
            ),
        )
    )
    assert runner.invoke(app, ["leagues", "use", "b", "--json"]).exit_code == 0
    assert load_settings().active_draft_session is None


def _advanced_settings(tmp_path: Path) -> Settings:
    return Settings(
        sleeper_username="alice",
        sleeper_user_id="u1",
        db_path=tmp_path / "advanced.duckdb",
        intelligence_mode="advanced",
        league_contexts={
            "l1": LeagueContext(league_id="l1", season="2026", recommendation_model="baseline-1.0")
        },
        active_draft_session=ActiveDraftSession(
            draft_id="d1",
            context_type="league",
            season="2026",
            source_league_id="l1",
            scoring_context_league_id="l1",
            draft_slot=1,
        ),
    )


def _readiness_result(*, ranking: str, projection: str, ready: bool) -> dict[str, Any]:
    statuses = {
        "player_directory": "pass",
        "compatible_ranking": ranking,
        "compatible_projection": projection,
        "codex_mcp_configuration": "missing",
    }
    return {
        "ready": ready,
        "checks": [
            {"name": name, "required": name != "codex_mcp_configuration", "status": status}
            for name, status in statuses.items()
        ],
    }


@pytest.mark.parametrize(
    ("ranking", "projection", "question_id"),
    [
        ("fail", "fail", "advanced_rankings"),
        ("pass", "fail", "advanced_projections"),
        ("fail", "pass", "advanced_rankings"),
    ],
)
def test_advanced_mode_requests_one_missing_human_input_at_a_time(
    monkeypatch: Any,
    tmp_path: Path,
    ranking: str,
    projection: str,
    question_id: str,
) -> None:
    monkeypatch.setattr(
        "fantasy_war_room.cli.readiness",
        lambda *_args, **_kwargs: _readiness_result(
            ranking=ranking, projection=projection, ready=False
        ),
    )
    result = _agent_status(_advanced_settings(tmp_path))
    assert result["state"] == "needs_input"
    assert result["question"]["id"] == question_id
    assert {choice["id"] for choice in result["question"]["choices"]} == {
        "quick",
        "personalized",
    }


def test_advanced_mode_with_compatible_inputs_is_ready(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "fantasy_war_room.cli.readiness",
        lambda *_args, **_kwargs: _readiness_result(ranking="pass", projection="pass", ready=True),
    )
    result = _agent_status(_advanced_settings(tmp_path))
    assert result["state"] == "ready"


def test_onboarding_state_boundary_rejects_actionless_needs_action() -> None:
    with pytest.raises(RuntimeError, match="at least one next action"):
        _validated_agent_state({"state": "needs_action", "question": None, "next_actions": []})


def test_claude_registration_uses_resolved_repository_cwd(
    runner: CliRunner, xdg: Path, tmp_path: Path, monkeypatch: Any
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    spec = ResolvedMcpLaunchSpec(
        schema_version="1.0",
        executable="uv",
        working_directory=str(repository_root),
        database=str(tmp_path / "fwr.duckdb"),
        draft_id="mock-1",
        draft_slot=2,
        ranking_source="source",
        recommendation_model="baseline-1.0",
        strategy=None,
        adp_source="local-adp",
        schedule_source="local-schedule",
        arguments=("run", "fwr-mcp", "--draft-id", "mock-1"),
    )
    called: dict[str, Any] = {}
    monkeypatch.setattr("fantasy_war_room.cli.resolve_mcp_launch_spec", lambda *_a, **_k: spec)

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        called.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fantasy_war_room.cli.subprocess.run", fake_run)
    result = runner.invoke(app, ["mcp", "configure", "--client", "claude", "--json"])
    assert result.exit_code == 0, result.stdout
    assert called["cwd"] == str(repository_root)
