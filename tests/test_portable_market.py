from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import polars as pl
import pytest
from typer.testing import CliRunner

from fantasy_war_room.bootstrap import generate_codex_config, readiness
from fantasy_war_room.config import LeagueContext, Settings, with_league_context
from fantasy_war_room.data_bootstrap import data_status
from fantasy_war_room.errors import InputError
from fantasy_war_room.market_board import (
    MARKET_BOARD_SOURCE,
    MARKET_BOARD_TRANSFORMATION_VERSION,
    derive_market_board,
)
from fantasy_war_room.market_imports import import_adp_frame
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.service import DraftCopilotService


def _portable_fixture(tmp_path: Path):
    from test_recommend_integration import BASE, _fixture

    repository = _fixture(tmp_path)
    observed_at = BASE + timedelta(hours=2)
    frame = pl.DataFrame(
        [
            {
                "player_name": "c-rb-2",
                "position": "RB",
                "team": "FA",
                "overall_adp": 4.0,
                "adp_sd": 1.5,
                "sample_size": 100,
            },
            {
                "player_name": "c-wr-2",
                "position": "WR",
                "team": "FA",
                "overall_adp": 2.0,
                "adp_sd": 1.0,
                "sample_size": 200,
            },
            {
                "player_name": "Unknown Prospect",
                "position": "RB",
                "team": "FA",
                "overall_adp": 3.0,
                "adp_sd": None,
                "sample_size": 5,
            },
            {
                "player_name": "c-rb-1",
                "position": "RB",
                "team": "FA",
                "overall_adp": 1.0,
                "adp_sd": 0.5,
                "sample_size": 300,
            },
        ]
    )
    adp, _ = import_adp_frame(
        frame,
        repository,
        original_filename="ffc.json",
        source="fantasy-football-calculator",
        source_version="api-v1",
        season="2026",
        scoring_format="ppr",
        league_size=2,
        draft_type="snake",
        observed_at=observed_at,
        fetched_at=observed_at,
        source_uri="https://fantasyfootballcalculator.com/api/v1/adp/ppr",
        source_payload_hash="ffc-payload-hash",
        transformation_version="ffc-api-to-adp-1.0",
    )
    board, _ = derive_market_board(repository, adp.adp_snapshot_id)
    return repository, adp, board


def test_market_board_is_deterministic_idempotent_and_preserves_issues(tmp_path: Path) -> None:
    repository, adp, board = _portable_fixture(tmp_path)
    repeated, repeated_created = derive_market_board(repository, adp.adp_snapshot_id)

    assert repeated.market_board_snapshot_id == board.market_board_snapshot_id
    assert repeated_created is False
    assert board.source == MARKET_BOARD_SOURCE
    assert board.transformation_version == MARKET_BOARD_TRANSFORMATION_VERSION
    assert board.source_payload_hash == "ffc-payload-hash"
    assert board.unresolved_row_count == 1
    with duckdb.connect(str(repository.path)) as connection:
        entries = connection.execute(
            "SELECT canonical_player_id, overall_market_rank, overall_adp, match_status "
            "FROM market_board_entries WHERE market_board_snapshot_id=? "
            "ORDER BY source_row_number",
            [board.market_board_snapshot_id],
        ).fetchall()
        issue_count = connection.execute(
            "SELECT count(*) FROM market_board_match_issues WHERE market_board_snapshot_id=?",
            [board.market_board_snapshot_id],
        ).fetchone()
    assert entries == [
        ("c-rb-2", 3, 4.0, "matched"),
        ("c-wr-2", 2, 2.0, "matched"),
        (None, None, 3.0, "unresolved"),
        ("c-rb-1", 1, 1.0, "matched"),
    ]
    assert issue_count == (1,)


def test_portable_model_is_ready_and_recommends_without_projections(tmp_path: Path) -> None:
    repository, _, _ = _portable_fixture(tmp_path)
    with duckdb.connect(str(repository.path)) as connection:
        connection.execute("DELETE FROM projection_entries")
        connection.execute("DELETE FROM projection_snapshots")
        connection.execute("DELETE FROM ranking_entries")
        connection.execute("DELETE FROM ranking_snapshots")
        connection.execute(
            "INSERT INTO player_provider_ids VALUES ('c-wr-2', 'sleeper', 'a-future-alias', ?)",
            [datetime.now(UTC) + timedelta(days=1)],
        )
    with duckdb.connect(str(repository.path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM projection_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM ranking_snapshots").fetchone() == (0,)
    portable = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        league_contexts={
            "league-1": LeagueContext(
                league_id="league-1",
                season="2026",
                recommendation_model="portable-market-1.0",
            )
        },
    )
    state = readiness(portable, repository_root=tmp_path)
    checks = {check["name"]: check for check in state["checks"]}
    assert state["ready"] is True, state
    assert checks["compatible_projection"]["status"] == "skipped"
    assert checks["compatible_market_board"]["status"] == "pass"
    configured = generate_codex_config(portable, repository_root=tmp_path)
    assert configured["recommendation_model"] == "portable-market-1.0"
    assert configured["ranking_source"] == MARKET_BOARD_SOURCE
    assert (tmp_path / ".codex" / "config.toml").is_file()

    decision_at = datetime.now(UTC)
    result = repository.portable_market_inputs(
        decision_at,
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
    )
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=None,
        default_source=MARKET_BOARD_SOURCE,
        default_model="portable-market-1.0",
    )
    response, _ = service.recommend_pick(
        model=None,
        source=None,
        limit=10,
        as_of=decision_at.isoformat(),
    )
    input_by_id = {player.canonical_player_id: player for player in result.market_players}
    assert input_by_id["c-wr-2"].sleeper_player_id == "s-wr-2"
    assert response["projection_backed"] is False
    assert response["candidates"][0]["canonical_player_id"] == "c-wr-2"
    assert "c-rb-1" not in {
        candidate["canonical_player_id"] for candidate in response["candidates"]
    }
    assert {
        "projected_points",
        "vorp",
        "replacement_projection",
        "scarcity",
        "starter_projection_delta",
    }.isdisjoint(response["candidates"][0])
    assert response["provenance"]["market_board_source"] == MARKET_BOARD_SOURCE
    assert "market-order baseline" in " ".join(response["limitations"])

    portable_result, snapshot, inputs = service._portable_context(decision_at.isoformat())
    empty_result = portable_result.model_copy(update={"candidates": []})
    service._portable_context = lambda _: (empty_result, snapshot, inputs)  # type: ignore[method-assign]
    with pytest.raises(InputError) as raised:
        service.recommend_pick(model=None, source=None, limit=10, as_of=None)
    assert raised.value.code == "insufficient_market_depth"

    trusted = portable.model_copy(
        update={
            "league_contexts": {
                "league-1": portable.league_contexts["league-1"].model_copy(
                    update={
                        "ranking_source": "rotoworld",
                        "recommendation_model": "trusted-board-1.1",
                    }
                )
            }
        }
    )
    assert readiness(trusted, repository_root=tmp_path)["ready"] is False


def test_new_context_gets_portable_default_without_migrating_existing_context() -> None:
    settings = Settings(
        league_contexts={"existing": LeagueContext(league_id="existing", season="2026")}
    )
    existing = with_league_context(settings, league_id="existing", season="2026")
    added = with_league_context(settings, league_id="new", season="2026")

    assert existing.league_contexts["existing"].recommendation_model is None
    assert added.league_contexts["new"].recommendation_model == "portable-market-1.0"


def test_data_status_reports_sources_and_model_readiness(tmp_path: Path) -> None:
    repository, _, _ = _portable_fixture(tmp_path)
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        league_contexts={
            "league-1": LeagueContext(
                league_id="league-1",
                season="2026",
                recommendation_model="portable-market-1.0",
            )
        },
    )

    status = data_status(settings, repository_root=tmp_path)

    assert status["schema_version"] == "1.0"
    assert status["sources"]["player_directory"]["source_version"] == "1.0"
    assert status["sources"]["ffc_adp"]["compatible"] is True
    assert status["sources"]["portable_market_board"]["state"] == "available"
    assert status["sources"]["custom_rankings"]["state"] == "manual"
    assert status["model_readiness"]["portable-market-1.0"]["status"] == "READY"
    assert status["model_readiness"]["configured"]["model"] == "portable-market-1.0"


def test_data_status_cli_json_contract(tmp_path: Path, monkeypatch: Any) -> None:
    from fantasy_war_room import cli

    repository, _, _ = _portable_fixture(tmp_path)
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        league_contexts={
            "league-1": LeagueContext(
                league_id="league-1",
                season="2026",
                recommendation_model="portable-market-1.0",
            )
        },
    )
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)

    result = CliRunner().invoke(cli.app, ["data", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.stdout)
    assert payload["command"] == "data status"
    assert payload["data"]["sources"]["portable_market_board"]["compatible"] is True


def test_data_refresh_orchestrates_players_and_portable_sources(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from fantasy_war_room import cli

    settings = Settings(db_path=tmp_path / "refresh.duckdb")
    calls: dict[str, Any] = {}

    class Client:
        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    monkeypatch.setattr(cli, "ensure_directories", lambda _: None)
    monkeypatch.setattr(cli, "_client", lambda _: Client())

    def fake_sync(*args: Any, **kwargs: Any):
        calls["player_force"] = kwargs["force"]
        return SimpleNamespace(snapshot_id="players-1"), True, "network"

    def fake_bootstrap(*args: Any, **kwargs: Any):
        calls["bootstrap_force"] = kwargs["force"]
        return {
            "active_league_id": "league-1",
            "format": {},
            "sources": {},
            "recommendation_ready": True,
        }

    monkeypatch.setattr(cli, "sync_players", fake_sync)
    monkeypatch.setattr(cli, "bootstrap_data", fake_bootstrap)

    result = CliRunner().invoke(cli.app, ["data", "refresh", "--force", "--json"])

    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.stdout)
    assert payload["data"]["sources"]["player_directory"]["status"] == "acquired"
    assert calls == {"player_force": True, "closed": True, "bootstrap_force": True}
