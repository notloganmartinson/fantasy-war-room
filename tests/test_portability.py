from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from fantasy_war_room.bootstrap import (
    MANAGED_END,
    MANAGED_START,
    _update_codex_toml,
    choose_league_id,
    draft_slot,
    generate_codex_config,
    readiness,
    resolve_effective_draft_configuration,
)
from fantasy_war_room.config import (
    LeagueContext,
    RecommendationModelSelection,
    Settings,
    config_file_path,
    load_settings,
    save_settings,
    with_league_context,
)
from fantasy_war_room.models import Snapshot


def _strategy_settings(
    *, recommendation_model: RecommendationModelSelection, ranking_source: str
) -> Settings:
    return Settings(
        active_league_id="l1",
        sleeper_league_id="l1",
        league_contexts={
            "l1": LeagueContext(
                league_id="l1",
                season="2026",
                recommendation_model=recommendation_model,
                ranking_source=ranking_source,
                strategy="logan-ppr-2flex-1.0",
            )
        },
    )


def _save_alice_contexts(xdg: Path) -> None:
    save_settings(
        Settings(
            sleeper_username="alice",
            sleeper_user_id="alice-id",
            active_league_id="alice-1",
            sleeper_league_id="alice-1",
            db_path=xdg.parent / "portable.duckdb",
            poll_seconds=4.5,
            league_contexts={
                "alice-1": LeagueContext(
                    league_id="alice-1",
                    season="2026",
                    ranking_source="alice-source",
                    recommendation_model="trusted-board-1.1",
                    strategy="logan-ppr-2flex-1.0",
                ),
                "alice-2": LeagueContext(league_id="alice-2", season="2026"),
            },
        )
    )


def _register_account(
    api: Any,
    *,
    username: str,
    user_id: str,
    league_ids: list[str],
    with_sync: bool = False,
    scoring_settings: dict[str, float] | None = None,
    roster_positions: list[str] | None = None,
    team_count: int = 10,
    draft_slot_value: int = 3,
) -> None:
    base = "https://api.sleeper.app/v1"
    leagues = [
        {
            "league_id": league_id,
            "name": league_id,
            "status": "pre_draft",
            "draft_id": f"draft-{league_id}",
            "total_rosters": team_count,
            "season": "2026",
            "settings": {"type": 0},
            "scoring_settings": scoring_settings or {"rec": 1},
            "roster_positions": roster_positions or ["QB", "RB", "WR", "TE", "FLEX", "BN"],
        }
        for league_id in league_ids
    ]
    api.get(f"{base}/user/{username}").mock(
        return_value=httpx.Response(200, json={"user_id": user_id, "username": username})
    )
    api.get(f"{base}/user/{user_id}/leagues/nfl/2026").mock(
        return_value=httpx.Response(200, json=leagues)
    )
    if not with_sync:
        return
    for league in leagues:
        league_id = str(league["league_id"])
        draft = {
            "draft_id": f"draft-{league_id}",
            "league_id": league_id,
            "created": 1,
            "type": "snake",
            "season": "2026",
            "status": "pre_draft",
            "settings": {"teams": team_count, "rounds": 6},
            "draft_order": {user_id: draft_slot_value},
        }
        api.get(f"{base}/league/{league_id}").mock(return_value=httpx.Response(200, json=league))
        api.get(f"{base}/league/{league_id}/drafts").mock(
            return_value=httpx.Response(200, json=[draft])
        )
        api.get(f"{base}/draft/draft-{league_id}").mock(
            return_value=httpx.Response(200, json=draft)
        )
        api.get(f"{base}/draft/draft-{league_id}/picks").mock(
            return_value=httpx.Response(200, json=[])
        )
    api.get(f"{base}/players/nfl").mock(
        return_value=httpx.Response(
            200,
            json={
                "p1": {
                    "first_name": "Portable",
                    "last_name": "Player",
                    "position": "RB",
                    "team": "MIN",
                    "fantasy_positions": ["RB"],
                }
            },
        )
    )


def test_legacy_config_migrates_without_losing_preferences(xdg: Path) -> None:
    path = config_file_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "sleeper_username": "logan",
                "sleeper_user_id": "u7",
                "sleeper_league_id": "league-7",
                "season": "2025",
                "db_path": str(xdg.parent / "custom.duckdb"),
                "poll_seconds": 3.5,
                "strategy": "logan-ppr-2flex-1.0",
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.active_league_id == "league-7"
    assert settings.active_context == LeagueContext(
        league_id="league-7",
        season="2025",
        strategy="logan-ppr-2flex-1.0",
    )
    assert settings.poll_seconds == 3.5
    save_settings(settings)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["config_schema_version"] == "2.0"
    assert "sleeper_league_id" not in persisted
    assert persisted["league_contexts"]["league-7"]["strategy"] == "logan-ppr-2flex-1.0"


def test_migrated_strategy_supplies_effective_model_and_source(xdg: Path) -> None:
    path = config_file_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "sleeper_user_id": "u7",
                "sleeper_league_id": "league-7",
                "season": "2026",
                "strategy": "logan-ppr-2flex-1.0",
            }
        ),
        encoding="utf-8",
    )

    effective = resolve_effective_draft_configuration(load_settings())

    assert effective.recommendation_model == "trusted-board-1.1"
    assert effective.ranking_source == "parlay-play-hybrid"
    assert effective.strategy_profile is not None


def test_effective_strategy_configuration_accepts_compatible_explicit_values() -> None:
    settings = _strategy_settings(
        recommendation_model="trusted-board-1.1",
        ranking_source="parlay-play-hybrid",
    )

    effective = resolve_effective_draft_configuration(settings)

    assert effective.recommendation_model == "trusted-board-1.1"
    assert effective.ranking_source == "parlay-play-hybrid"


@pytest.mark.parametrize(
    ("model", "source", "field"),
    [
        ("baseline-1.0", "parlay-play-hybrid", "recommendation_model"),
        ("trusted-board-1.1", "another-source", "ranking_source"),
    ],
)
def test_effective_strategy_configuration_rejects_explicit_conflicts(
    model: RecommendationModelSelection, source: str, field: str
) -> None:
    settings = _strategy_settings(recommendation_model=model, ranking_source=source)

    with pytest.raises(Exception) as raised:
        resolve_effective_draft_configuration(settings)

    assert raised.value.code == "strategy_configuration_conflict"
    assert field in raised.value.details["conflicts"]


def test_no_strategy_uses_portable_model_fallback() -> None:
    settings = Settings(
        active_league_id="l1",
        sleeper_league_id="l1",
        league_contexts={"l1": LeagueContext(league_id="l1", season="2026")},
    )

    effective = resolve_effective_draft_configuration(settings)

    assert effective.recommendation_model == "baseline-1.0"
    assert effective.ranking_source is None
    assert effective.strategy is None


def test_strategy_required_intelligence_blocks_readiness_and_codex(tmp_path: Path) -> None:
    from test_recommend_integration import _fixture

    repository = _fixture(tmp_path)
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        sleeper_league_id="league-1",
        league_contexts={
            "league-1": LeagueContext(
                league_id="league-1",
                season="2026",
                strategy="logan-ppr-2flex-1.0",
            )
        },
    )

    state = readiness(settings, repository_root=tmp_path / "project")

    checks = {item["name"]: item for item in state["checks"]}
    assert state["ready"] is False
    assert state["recommendation_model"] == "trusted-board-1.1"
    assert state["ranking_source"] == "parlay-play-hybrid"
    assert checks["compatible_ranking"]["status"] == "fail"
    assert checks["strategy"]["required"] is True
    assert checks["strategy"]["status"] == "fail"
    with pytest.raises(Exception) as raised:
        generate_codex_config(settings, repository_root=tmp_path / "project")
    assert raised.value.code == "codex_context_incomplete"


def test_codex_generation_rejects_explicit_strategy_conflict(tmp_path: Path) -> None:
    settings = _strategy_settings(
        recommendation_model="baseline-1.0",
        ranking_source="parlay-play-hybrid",
    )

    with pytest.raises(Exception) as raised:
        generate_codex_config(settings, repository_root=tmp_path)

    assert raised.value.code == "strategy_configuration_conflict"


def test_multiple_leagues_switch_without_preference_leakage(xdg: Path) -> None:
    first = with_league_context(
        Settings(sleeper_username="alice", sleeper_user_id="u1"),
        league_id="l1",
        season="2026",
        ranking_source="source-one",
        recommendation_model="baseline-1.0",
        strategy="logan-ppr-2flex-1.0",
    )
    second = with_league_context(first, league_id="l2", season="2026")
    save_settings(second)

    loaded = load_settings()
    assert loaded.active_league_id == "l2"
    assert loaded.active_strategy is None
    assert loaded.league_contexts["l1"].strategy == "logan-ppr-2flex-1.0"
    assert loaded.league_contexts["l2"].ranking_source is None


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_configure_new_user_selects_only_league_and_clears_old_contexts(
    api: Any, runner: Any, xdg: Path
) -> None:
    from conftest import parse_output

    from fantasy_war_room.cli import app

    _save_alice_contexts(xdg)
    _register_account(api, username="bob", user_id="bob-id", league_ids=["bob-1"])

    result = runner.invoke(
        app,
        ["configure", "--username", "bob", "--non-interactive", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    assert parse_output(result)["data"]["league_id"] == "bob-1"
    configured = load_settings()
    assert configured.sleeper_user_id == "bob-id"
    assert set(configured.league_contexts) == {"bob-1"}
    assert configured.db_path == xdg.parent / "portable.duckdb"
    assert configured.poll_seconds == 4.5
    unavailable = runner.invoke(app, ["leagues", "use", "alice-1", "--json"])
    assert unavailable.exit_code == 3
    assert parse_output(unavailable)["error"]["code"] == "league_context_not_found"


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_setup_new_user_with_explicit_league_clears_old_contexts(
    api: Any, runner: Any, xdg: Path
) -> None:
    _save_alice_contexts(xdg)
    _register_account(
        api,
        username="bob",
        user_id="bob-id",
        league_ids=["bob-1"],
        with_sync=True,
    )
    from fantasy_war_room.cli import app

    result = runner.invoke(
        app,
        [
            "setup",
            "--username",
            "bob",
            "--league-id",
            "bob-1",
            "--non-interactive",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    configured = load_settings()
    assert configured.sleeper_user_id == "bob-id"
    assert set(configured.league_contexts) == {"bob-1"}
    assert configured.league_contexts["bob-1"].strategy is None


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_configure_same_resolved_user_preserves_all_contexts(
    api: Any, runner: Any, xdg: Path
) -> None:
    _save_alice_contexts(xdg)
    _register_account(
        api,
        username="renamed-alice",
        user_id="alice-id",
        league_ids=["alice-1", "alice-2"],
    )
    from fantasy_war_room.cli import app

    result = runner.invoke(
        app,
        ["configure", "--username", "renamed-alice", "--non-interactive", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    configured = load_settings()
    assert set(configured.league_contexts) == {"alice-1", "alice-2"}
    assert configured.league_contexts["alice-1"].ranking_source == "alice-source"
    assert configured.league_contexts["alice-1"].strategy == "logan-ppr-2flex-1.0"


def test_league_selection_policy_covers_no_multiple_and_not_owned() -> None:
    assert choose_league_id(["l1"], None, non_interactive=True) == "l1"
    assert choose_league_id(["l1", "l2"], None, non_interactive=False) is None

    import pytest

    with pytest.raises(Exception) as multiple:
        choose_league_id(["l1", "l2"], None, non_interactive=True)
    assert multiple.value.code == "league_selection_required"
    with pytest.raises(Exception) as unowned:
        choose_league_id(["l1"], "other", non_interactive=True)
    assert unowned.value.code == "league_not_available"


def test_draft_slot_can_be_pending_then_resolve_on_rerun() -> None:
    pending = Snapshot(
        snapshot_id="one",
        league_id="l1",
        draft_id="d1",
        observed_at="2026-08-01T00:00:00Z",
        source_updated_at=None,
        payload_hash="one",
        pick_count=0,
        league={},
        draft={"draft_id": "d1", "draft_order": None},
        picks=[],
    )
    published = pending.model_copy(
        update={
            "snapshot_id": "two",
            "payload_hash": "two",
            "draft": {"draft_id": "d1", "draft_order": {"u1": 4}},
        }
    )

    assert draft_slot(pending, "u1") is None
    assert draft_slot(published, "u1") == 4


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_noninteractive_setup_clean_clone_is_explicitly_not_ready(
    api: Any,
    runner: Any,
    xdg: Path,
    sleeper_payloads: dict[str, Any],
) -> None:
    from conftest import parse_output, register_sleeper

    from fantasy_war_room.cli import app

    payloads = dict(sleeper_payloads)
    payloads["picks"] = [{**sleeper_payloads["picks"][0], "picked_by": "someone-else"}]
    register_sleeper(api, payloads, picks=payloads["picks"])

    result = runner.invoke(
        app,
        ["setup", "--username", "alice", "--non-interactive", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    data = parse_output(result)["data"]
    assert data["league_id"] == "l1"
    assert data["draft_slot_status"] == "pending"
    assert data["draft_ready"] is False
    assert load_settings().active_strategy is None
    names = {check["name"]: check for check in data["readiness_checks"]}
    assert names["compatible_ranking"]["status"] == "skipped"
    assert names["compatible_projection"]["status"] == "skipped"
    assert names["compatible_market_board"]["status"] == "fail"
    with pytest.raises(Exception) as raised:
        generate_codex_config(load_settings(), repository_root=xdg.parent / "project")
    assert raised.value.code == "codex_context_incomplete"


def test_complete_readiness_and_codex_config_are_context_exact(xdg: Path, tmp_path: Path) -> None:
    from test_recommend_integration import _fixture

    repository = _fixture(tmp_path)
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        sleeper_league_id="league-1",
        league_contexts={
            "league-1": LeagueContext(
                league_id="league-1",
                season="2026",
                ranking_source="rotoworld",
                recommendation_model="baseline-1.0",
            ),
            "other": LeagueContext(
                league_id="other",
                season="2026",
                ranking_source="do-not-leak",
                recommendation_model="trusted-board-1.1",
                strategy="logan-ppr-2flex-1.0",
            ),
        },
    )
    root = tmp_path / "project"
    (root / ".codex").mkdir(parents=True)
    (root / ".codex" / "config.toml").write_text(
        '[mcp_servers.unrelated]\ncommand = "keep-me"\n',
        encoding="utf-8",
    )
    state = readiness(settings, repository_root=root)

    assert state["ready"] is True
    generated = generate_codex_config(settings, repository_root=root)
    block = generated["toml_block"]
    assert '"draft-1"' in block
    assert '"1"' in block
    assert '"rotoworld"' in block
    assert '"baseline-1.0"' in block
    assert "do-not-leak" not in block
    assert "logan-ppr-2flex-1.0" not in block
    persisted = (root / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[mcp_servers.unrelated]\ncommand = "keep-me"' in persisted


def _managed_test_block(command: str = "fwr-mcp") -> str:
    return (
        f'{MANAGED_START}\n[mcp_servers.fantasy-war-room]\ncommand = "{command}"\n{MANAGED_END}\n'
    )


@pytest.mark.parametrize(
    "existing",
    [
        "",
        '[mcp_servers.unrelated]\ncommand = "keep"\n',
    ],
)
def test_codex_toml_writer_adds_managed_block_without_duplicates(existing: str) -> None:
    updated = _update_codex_toml(existing, _managed_test_block())

    parsed = tomllib.loads(updated)
    assert parsed["mcp_servers"]["fantasy-war-room"]["command"] == "fwr-mcp"
    assert updated.count(MANAGED_START) == updated.count(MANAGED_END) == 1


@pytest.mark.parametrize(
    "heading",
    [
        "[mcp_servers.fantasy-war-room]",
        '[mcp_servers."fantasy-war-room"]',
        "[mcp_servers.'fantasy-war-room']",
    ],
)
def test_codex_toml_writer_refuses_unmanaged_equivalent_fwr_tables(heading: str) -> None:
    existing = f'{heading}\ncommand = "custom"\n'

    with pytest.raises(Exception) as raised:
        _update_codex_toml(existing, _managed_test_block())

    assert raised.value.code == "unmanaged_codex_mcp_config"


def test_codex_toml_writer_replaces_managed_middle_block_idempotently() -> None:
    existing = (
        'title = "before"\n\n'
        + _managed_test_block("old")
        + '\n[mcp_servers.after]\ncommand = "after"\n'
    )

    once = _update_codex_toml(existing, _managed_test_block("new"))
    twice = _update_codex_toml(once, _managed_test_block("new"))

    assert once == twice
    assert once.startswith('title = "before"\n\n')
    assert once.endswith('[mcp_servers.after]\ncommand = "after"\n')
    parsed = tomllib.loads(once)
    assert parsed["mcp_servers"]["fantasy-war-room"]["command"] == "new"
    assert list(parsed["mcp_servers"]).count("fantasy-war-room") == 1


def test_codex_toml_writer_rejects_malformed_toml_without_rewriting() -> None:
    with pytest.raises(Exception) as raised:
        _update_codex_toml("[broken", _managed_test_block())

    assert raised.value.code == "invalid_codex_config"


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_clean_bootstrap_flow_generates_exact_parseable_codex_context(
    api: Any,
    runner: Any,
    xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import parse_output
    from test_recommend_integration import ROSTER, SCORING, _fixture

    from fantasy_war_room import cli

    repository = _fixture(tmp_path)
    project = tmp_path / "clean-project"
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", project)
    _register_account(
        api,
        username="alice",
        user_id="user-1",
        league_ids=["league-1"],
        with_sync=True,
        scoring_settings=SCORING,
        roster_positions=ROSTER,
        team_count=2,
        draft_slot_value=1,
    )

    setup = runner.invoke(
        cli.app,
        [
            "setup",
            "--username",
            "alice",
            "--league-id",
            "league-1",
            "--ranking-source",
            "rotoworld",
            "--recommendation-model",
            "baseline-1.0",
            "--db-path",
            str(repository.path),
            "--non-interactive",
            "--json",
        ],
    )
    assert setup.exit_code == 0, setup.stdout
    context = runner.invoke(cli.app, ["context", "--json"])
    ready = runner.invoke(cli.app, ["draft-ready", "--json"])
    configured = runner.invoke(cli.app, ["codex", "configure", "--json"])

    assert context.exit_code == ready.exit_code == configured.exit_code == 0
    assert parse_output(context)["data"]["active_league_id"] == "league-1"
    assert parse_output(ready)["data"]["ready"] is True
    generated = parse_output(configured)["data"]
    parsed = tomllib.loads(Path(generated["path"]).read_text(encoding="utf-8"))
    server = parsed["mcp_servers"]["fantasy-war-room"]
    args = server["args"]
    assert args[args.index("--draft-id") + 1] == "draft-league-1"
    assert args[args.index("--draft-slot") + 1] == "1"
    assert args[args.index("--model") + 1] == "baseline-1.0"
    assert args[args.index("--source") + 1] == "rotoworld"
    assert "--strategy" not in args
    assert server["cwd"] == str(project)
