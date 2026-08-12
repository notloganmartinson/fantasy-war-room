from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
from mcp import Client
from typer.testing import CliRunner

from fantasy_war_room.cli import app
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.server import create_server
from fantasy_war_room.mcp.service import DraftCopilotService
from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.services import canonical_hash

BASE = datetime(2026, 8, 1, tzinfo=UTC)
SCORING = {"rec": 1, "rush_yd": 0.1, "rec_yd": 0.1, "pass_yd": 0.04}
ROSTER = ["QB", "RB", "WR", "TE", "FLEX", "BN", "K", "DEF"]


def test_repository_builds_inputs_and_replays_as_of_without_future_knowledge(
    tmp_path: Path,
) -> None:
    repository = _fixture(tmp_path)
    early = repository.recommendation_inputs(
        BASE + timedelta(hours=4),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        ranking_source="rotoworld",
    )
    later = repository.recommendation_inputs(
        BASE + timedelta(hours=7),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        ranking_source="rotoworld",
    )

    assert early.provenance.draft_snapshot_id == "draft-base"
    assert early.provenance.player_snapshot_id == "players-base"
    assert early.provenance.ranking_snapshot_id == "ranking-base"
    assert early.provenance.projection_snapshot_id == "projection-base"
    assert len(early.completed_picks) == 2
    assert later.provenance.draft_snapshot_id == "draft-future"
    assert later.provenance.player_snapshot_id == "players-future"
    assert later.provenance.ranking_snapshot_id == "ranking-future"
    assert later.provenance.projection_snapshot_id == "projection-future"

    configured = repository.recommendation_inputs(
        BASE + timedelta(hours=4),
        draft_id=None,
        league_id="league-1",
        sleeper_user_id="user-1",
        draft_slot=None,
        ranking_source="rotoworld",
    )
    assert configured.provenance.draft_snapshot_id == "draft-base"
    assert configured.draft_slot == 1


def test_recommend_cli_json_limit_invariance_drafted_exclusion_and_determinism(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repository = _fixture(tmp_path)
    runner = CliRunner()
    common = [
        "recommend",
        "--draft-id",
        "draft-1",
        "--draft-slot",
        "1",
        "--source",
        "rotoworld",
        "--as-of",
        (BASE + timedelta(hours=4)).isoformat(),
        "--db-path",
        str(repository.path),
        "--json",
    ]
    one = runner.invoke(app, [*common, "--limit", "1"])
    many = runner.invoke(app, [*common, "--limit", "20"])
    repeated = runner.invoke(app, [*common, "--limit", "20"])

    assert one.exit_code == many.exit_code == repeated.exit_code == 0
    one_data = json.loads(one.stdout)["data"]
    many_data = json.loads(many.stdout)["data"]
    assert many.stdout == repeated.stdout
    assert (
        one_data["candidates"][0]["recommendation_score"]
        == many_data["candidates"][0]["recommendation_score"]
    )
    assert one_data["baselines"] == many_data["baselines"]
    candidate_ids = {candidate["canonical_player_id"] for candidate in many_data["candidates"]}
    assert "c-qb-1" not in candidate_ids
    assert "c-rb-1" not in candidate_ids  # picked through its retained old Sleeper ID
    assert "c-wr-3" in candidate_ids  # projection match without a ranking entry
    partial = next(
        candidate
        for candidate in many_data["candidates"]
        if candidate["canonical_player_id"] == "c-wr-3"
    )
    assert partial["projection_value_kind"] == "known_component"
    assert partial["scoring_completeness"] == "partial"

    monkeypatch.setenv("FWR_SLEEPER_LEAGUE_ID", "league-1")
    monkeypatch.setenv("FWR_SLEEPER_USER_ID", "user-1")
    configured = runner.invoke(
        app,
        [
            "recommend",
            "--source",
            "rotoworld",
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert configured.exit_code == 0
    assert json.loads(configured.stdout)["data"]["provenance"]["draft_snapshot_id"] == "draft-base"

    trusted_common = [*common, "--model", "trusted-board-1.0"]
    trusted_one = runner.invoke(app, [*trusted_common, "--limit", "1"])
    trusted_many = runner.invoke(app, [*trusted_common, "--limit", "20"])
    trusted_repeated = runner.invoke(app, [*trusted_common, "--limit", "20"])
    assert trusted_one.exit_code == trusted_many.exit_code == trusted_repeated.exit_code == 0
    trusted_one_data = json.loads(trusted_one.stdout)["data"]
    trusted_many_data = json.loads(trusted_many.stdout)["data"]
    assert trusted_many.stdout == trusted_repeated.stdout
    assert trusted_many_data["model_specification"]["recommendation_model_version"] == (
        "trusted-board-1.0"
    )
    assert trusted_many_data["model_specification"]["weights"] == {
        "vorp": 30.0,
        "expert_rank": 50.0,
        "scarcity": 15.0,
        "roster_fit": 5.0,
        "next_pick_availability": 0.0,
    }
    assert (
        trusted_one_data["candidates"][0]["recommendation_score"]
        == trusted_many_data["candidates"][0]["recommendation_score"]
    )
    assert trusted_one_data["baselines"] == trusted_many_data["baselines"]

    version_1_1 = runner.invoke(app, [*common, "--model", "trusted-board-1.1", "--limit", "20"])
    assert version_1_1.exit_code == 0
    version_1_1_data = json.loads(version_1_1.stdout)["data"]
    assert version_1_1_data["schema_version"] == "1.1"
    assert version_1_1_data["model_specification"]["recommendation_model_version"] == (
        "trusted-board-1.1"
    )
    assert version_1_1_data["model_specification"]["trusted_rank_transform_version"] == (
        "exponential-half-life-20-1.0"
    )
    ranked_candidate = next(
        candidate
        for candidate in version_1_1_data["candidates"]
        if candidate["canonical_player_id"] == "c-rb-2"
    )
    assert ranked_candidate["trusted_tier"] == "A"
    assert ranked_candidate["trusted_tier_component"]["weight"] == 15.0


def test_recommend_human_output_and_standalone_mock_outside_repository_cwd(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repository = _fixture(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = CliRunner().invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "mock-1",
            "--draft-slot",
            "2",
            "--source",
            "rotoworld",
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--limit",
            "20",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Model: baseline-1.0" in result.stdout
    assert "expert_rank" in result.stdout
    assert "Round 2, pick 3" in result.stdout
    assert "Draft recommendations" in result.stdout
    assert "VORP" in result.stdout
    assert "unavailable" in result.stdout
    assert "known" in result.stdout


def test_recommend_stable_snapshot_and_context_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    empty = IntelligenceRepository(tmp_path / "empty.duckdb")
    no_draft = runner.invoke(app, ["recommend", "--db-path", str(empty.path), "--json"])
    assert json.loads(no_draft.stdout)["error"]["code"] == "no_draft_snapshot"

    repository = _fixture(tmp_path / "populated")
    missing_source = runner.invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "draft-1",
            "--draft-slot",
            "1",
            "--source",
            "missing",
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    missing_draft = runner.invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "absent",
            "--draft-slot",
            "1",
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert json.loads(missing_source.stdout)["error"]["code"] == "missing_ranking_snapshot"
    assert json.loads(missing_draft.stdout)["error"]["code"] == "draft_not_found"


def test_unattributed_pick_is_not_assigned_to_slot_one_and_mock_context_is_required(
    tmp_path: Path,
) -> None:
    repository = _fixture(tmp_path)
    context = {
        "league_id": "league-1",
        "season": "2026",
        "settings": {"type": 0},
        "scoring_settings": SCORING,
        "roster_positions": ROSTER,
    }
    unattributed = _pick(2, 2, "s-rb-2", "user-2")
    unattributed.pop("draft_slot")
    unattributed.pop("roster_id")
    repository.insert(
        _draft(
            "draft-unattributed",
            "draft-unattributed",
            BASE + timedelta(hours=2),
            context,
            [_pick(1, 1, "s-qb-1", "user-1"), unattributed],
        )
    )
    inputs = repository.recommendation_inputs(
        BASE + timedelta(hours=4),
        draft_id="draft-unattributed",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        ranking_source="rotoworld",
    )
    assert inputs.completed_picks[1].draft_slot is None

    no_context = _draft(
        "mock-no-context",
        "mock-no-context",
        BASE + timedelta(hours=2),
        context,
        [],
        standalone=True,
    ).model_copy(update={"scoring_context": None, "scoring_context_league_id": None})
    repository.insert(no_context)
    result = CliRunner().invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "mock-no-context",
            "--draft-slot",
            "1",
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert json.loads(result.stdout)["error"]["code"] == "mock_scoring_context_required"


def test_mcp_read_repository_and_service_are_explicit_as_of_deterministic(
    tmp_path: Path,
) -> None:
    repository = _fixture(tmp_path)
    at = BASE + timedelta(hours=4)
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
    )

    first, first_provenance = service.recommend_pick(
        model=None, source=None, limit=20, as_of=at.isoformat()
    )
    repeated, repeated_provenance = service.recommend_pick(
        model=None, source=None, limit=20, as_of=at.isoformat()
    )

    assert first == repeated
    assert first_provenance == repeated_provenance
    assert first["provenance"]["draft_snapshot_id"] == "draft-base"
    assert first["model_specification"]["recommendation_model_version"] == ("trusted-board-1.1")
    candidate_ids = {row["canonical_player_id"] for row in first["candidates"]}
    assert "c-qb-1" not in candidate_ids
    assert "c-rb-1" not in candidate_ids


def test_mcp_tools_are_read_only_structured_and_domain_errors_set_is_error(
    tmp_path: Path,
) -> None:
    repository = _fixture(tmp_path)
    at = (BASE + timedelta(hours=4)).isoformat()
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "get_draft_state",
                "get_my_roster",
                "get_available_players",
                "recommend_pick",
                "compare_players",
                "get_position_outlook",
                "get_market_context",
                "get_opponent_demand",
            }
            assert all(
                tool.annotations and tool.annotations.read_only_hint for tool in listing.tools
            )
            result = await client.call_tool(
                "recommend_pick",
                {"model": "trusted-board-1.1", "source": "rotoworld", "as_of": at},
            )
            assert result.is_error is False
            assert result.structured_content["status"] == "ok"
            assert result.structured_content["data"]["turn_context"]["current_round"] == 2

        missing = DraftCopilotService(
            McpReadRepository(repository.path),
            draft_id="missing",
            sleeper_user_id="user-1",
            draft_slot=1,
            default_source="rotoworld",
        )
        async with Client(create_server(missing)) as client:
            result = await client.call_tool("get_draft_state", {"as_of": at})
            assert result.is_error is True
            assert result.structured_content["status"] == "error"
            assert result.structured_content["error"]["code"] == "draft_not_found"

    asyncio.run(exercise())


def test_mcp_roster_comparison_and_position_outlook(tmp_path: Path) -> None:
    repository = _fixture(tmp_path)
    at = (BASE + timedelta(hours=4)).isoformat()
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
    )

    roster, provenance = service.get_my_roster(as_of=at)
    available, available_provenance = service.get_available_players(
        position="RB", limit=20, as_of=at
    )
    comparison, comparison_provenance = service.compare_players(
        players=["c-qb-1", "c-rb-2"], as_of=at
    )
    outlook, outlook_provenance = service.get_position_outlook(position="RB", as_of=at)

    assert roster["starters"][0]["canonical_player_id"] == "c-qb-1"
    assert all(row["availability"] == "available" for row in available["players"])
    assert "c-rb-1" not in {row["canonical_player_id"] for row in available["players"]}
    assert comparison["players"][0]["availability"] == "drafted"
    assert comparison["players"][0]["recommendation_score"] is None
    assert outlook["outlooks"][0]["position"] == "RB"
    snapshot_ids = {
        value["draft_snapshot_id"]
        for value in (provenance, available_provenance, comparison_provenance, outlook_provenance)
    }
    assert snapshot_ids == {"draft-base"}


def test_mcp_entrypoint_works_outside_repository_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    executable = Path(sys.executable).parent / "fwr-mcp"
    result = subprocess.run(
        [str(executable), "--help"],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: fwr-mcp" in result.stdout


def _fixture(tmp_path: Path) -> IntelligenceRepository:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = IntelligenceRepository(tmp_path / "recommend.duckdb")
    repository.initialize()
    context = {
        "league_id": "league-1",
        "season": "2026",
        "settings": {"type": 0},
        "scoring_settings": SCORING,
        "roster_positions": ROSTER,
    }
    base_picks = [
        _pick(1, 1, "s-qb-1", "user-1"),
        _pick(2, 2, "s-old-rb-1", "user-2"),
    ]
    repository.insert(
        _draft("draft-base", "draft-1", BASE + timedelta(hours=2), context, base_picks)
    )
    repository.insert(
        _draft(
            "draft-future",
            "draft-1",
            BASE + timedelta(hours=6),
            context,
            [*base_picks, _pick(3, 2, "s-wr-1", "user-2")],
        )
    )
    repository.insert(
        _draft(
            "mock-base",
            "mock-1",
            BASE + timedelta(hours=2),
            context,
            base_picks,
            standalone=True,
        )
    )
    _players(repository, "players-base", BASE + timedelta(hours=1))
    _players(repository, "players-future", BASE + timedelta(hours=6))
    _ranking(repository, "ranking-base", BASE, BASE + timedelta(hours=3), "base")
    _ranking(
        repository,
        "ranking-future",
        BASE,
        BASE + timedelta(hours=6),
        "future",
        reprocessed_from="ranking-base",
    )
    _projection(repository, "projection-base", BASE, BASE + timedelta(hours=3), "1.0")
    _projection(repository, "projection-future", BASE, BASE + timedelta(hours=6), "1.1")
    return repository


def _draft(
    snapshot_id: str,
    draft_id: str,
    observed_at: datetime,
    context: dict[str, Any],
    picks: list[dict[str, Any]],
    *,
    standalone: bool = False,
) -> Snapshot:
    draft = {
        "draft_id": draft_id,
        "league_id": None if standalone else "league-1",
        "season": "2026",
        "type": "snake",
        "status": "drafting",
        "settings": {"teams": 2, "rounds": 8},
        "draft_order": {"user-1": 1, "user-2": 2},
        "slot_to_roster_id": {"1": "r1", "2": "r2"},
    }
    return Snapshot(
        snapshot_id=snapshot_id,
        league_id=None if standalone else "league-1",
        draft_id=draft_id,
        observed_at=observed_at,
        source_updated_at=None,
        payload_hash=snapshot_id,
        pick_count=len(picks),
        league={} if standalone else context,
        draft=draft,
        picks=picks,
        source_league_id=None if standalone else "league-1",
        scoring_context_league_id="league-1",
        scoring_context=context,
        draft_context_type="standalone" if standalone else "league",
    )


def _pick(number: int, slot: int, player_id: str, user_id: str) -> dict[str, Any]:
    return {
        "pick_no": number,
        "round": (number - 1) // 2 + 1,
        "draft_slot": slot,
        "roster_id": f"r{slot}",
        "picked_by": user_id,
        "player_id": player_id,
        "metadata": {},
    }


def _players(repository: IntelligenceRepository, snapshot_id: str, at: datetime) -> None:
    players = [
        (f"c-{position.lower()}-{index}", f"s-{position.lower()}-{index}", position)
        for position in ("QB", "RB", "WR", "TE")
        for index in range(1, 5)
    ]
    with duckdb.connect(str(repository.path)) as connection:
        connection.execute(
            "INSERT INTO player_directory_snapshots VALUES "
            "(?, 'sleeper', 'nfl', ?, ?, ?, ?, ?, '1.0')",
            [snapshot_id, at, at, snapshot_id, len(players), "/synthetic/cache"],
        )
        for canonical_id, sleeper_id, position in players:
            connection.execute(
                "INSERT INTO canonical_players VALUES (?, ?) ON CONFLICT DO NOTHING",
                [canonical_id, at],
            )
            connection.execute(
                "INSERT INTO player_provider_ids VALUES (?, 'sleeper', ?, ?) "
                "ON CONFLICT DO NOTHING",
                [canonical_id, sleeper_id, at],
            )
            connection.execute(
                "INSERT INTO player_observations (snapshot_id, canonical_player_id, provider, "
                "provider_player_id, observed_at, first_name, last_name, normalized_full_name, "
                "position, fantasy_positions, team, active, status, injury_status, "
                "years_experience, provider_ids, raw_payload) VALUES "
                "(?, ?, 'sleeper', ?, ?, ?, '', ?, ?, ?, ?, true, NULL, NULL, NULL, ?, ?)",
                [
                    snapshot_id,
                    canonical_id,
                    sleeper_id,
                    at,
                    canonical_id,
                    canonical_id,
                    position,
                    json.dumps([position]),
                    "FA",
                    json.dumps({"sleeper": sleeper_id}),
                    json.dumps({}),
                ],
            )
        connection.execute(
            "INSERT INTO player_provider_ids VALUES "
            "('c-rb-1', 'sleeper', 's-old-rb-1', ?) ON CONFLICT DO NOTHING",
            [at],
        )


def _ranking(
    repository: IntelligenceRepository,
    snapshot_id: str,
    observed_at: datetime,
    imported_at: datetime,
    version: str,
    reprocessed_from: str | None = None,
) -> None:
    with duckdb.connect(str(repository.path)) as connection:
        connection.execute(
            "INSERT INTO ranking_snapshots VALUES "
            "(?, 'rotoworld', ?, '2026', 'ppr', 2, ?, ?, ?, 'synthetic.csv', "
            "3, 3, 0, 0, '1.0', '2.0', ?)",
            [snapshot_id, version, observed_at, imported_at, snapshot_id, reprocessed_from],
        )
        ranked = (("c-rb-2", "A"), ("c-wr-2", "B"), ("c-qb-2", "S"))
        for row_number, (canonical_id, tier) in enumerate(ranked, 1):
            connection.execute(
                "INSERT INTO ranking_entries VALUES "
                "(?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, NULL, "
                "'matched', ?, 'strict_identity')",
                [
                    snapshot_id,
                    row_number,
                    canonical_id,
                    canonical_id,
                    row_number,
                    json.dumps({"tier": tier}),
                ],
            )


def _projection(
    repository: IntelligenceRepository,
    snapshot_id: str,
    observed_at: datetime,
    imported_at: datetime,
    calculator_version: str,
) -> None:
    scoring_hash = canonical_hash({key: float(value) for key, value in SCORING.items()})
    with duckdb.connect(str(repository.path)) as connection:
        connection.execute(
            "INSERT INTO projection_snapshots VALUES "
            "(?, 'cbs', '2026-full', '2026', 'full_season', 'ppr', ?, ?, ?, "
            "16, 16, 0, 0, 'players-base', 'draft-base', ?, ?, ?, '1.0')",
            [
                snapshot_id,
                observed_at,
                imported_at,
                snapshot_id,
                scoring_hash,
                json.dumps(SCORING),
                calculator_version,
            ],
        )
        row_number = 0
        for position_index, position in enumerate(("QB", "RB", "WR", "TE")):
            for index in range(1, 5):
                row_number += 1
                exact = 300 - position_index * 20 - index * 5
                partial = position == "WR" and index == 3
                connection.execute(
                    "INSERT INTO projection_entries (projection_snapshot_id, source_position, "
                    "source_row_number, canonical_player_id, source_player_name, position, team, "
                    "cbs_projected_points, league_known_component_points, "
                    "league_projected_points, scoring_completeness, unprojected_scoring_keys, "
                    "match_status, match_method, raw_payload, schema_version) VALUES "
                    "(?, ?, ?, ?, ?, ?, 'FA', ?, ?, ?, ?, ?, 'matched', "
                    "'strict_identity', '{}', '1.0')",
                    [
                        snapshot_id,
                        position,
                        row_number,
                        f"c-{position.lower()}-{index}",
                        f"Synthetic {position} {index}",
                        position,
                        exact + 10,
                        exact,
                        None if partial else exact,
                        "partial" if partial else "complete",
                        json.dumps(["bonus_rec_yd_100"] if partial else []),
                    ],
                )
