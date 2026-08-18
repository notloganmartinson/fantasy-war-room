from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from mcp import Client
from test_recommend_integration import BASE, _draft, _fixture, _pick
from typer.testing import CliRunner

from fantasy_war_room.cli import app
from fantasy_war_room.errors import InputError, NotFoundError
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.server import create_server
from fantasy_war_room.mcp.service import DraftCopilotService
from fantasy_war_room.models import AdpSnapshot
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.survival import build_survival_response


def test_survival_input_builder_is_exact_as_of_and_preserves_pool_coverage(
    tmp_path: Path,
) -> None:
    repository = _survival_fixture(tmp_path)
    inputs, provenance = repository.survival_inputs(
        BASE + timedelta(hours=4),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        candidate_player_ids=("c-wr-2", "c-te-4"),
        simulation_count=100,
        seed=42,
        model_version="adp-only-1.0",
        adp_source="local-adp",
    )

    assert inputs.draft.draft_snapshot_id == "draft-base"
    assert inputs.adp.adp_snapshot_id == "adp-base"
    assert provenance["player_snapshot_id"] == "players-base"
    assert inputs.draft.user_draft_slot == 1
    assert inputs.draft.current_overall_pick == 3
    assert inputs.draft.user_is_on_the_clock is False
    assert inputs.draft.simulation_start_pick == 3
    assert inputs.draft.target_user_pick == 4
    available = {player.canonical_player_id: player for player in inputs.available_players}
    assert "c-qb-1" not in available
    assert "c-rb-1" not in available
    assert available["c-te-4"].overall_adp is None
    assert len(inputs.available_players) == 14
    rosters = {roster.draft_slot: roster for roster in inputs.opponent_rosters}
    assert set(rosters) == {2}
    assert rosters[2].rb == 1
    assert inputs.model_specification.model_version == "adp-only-1.0"


def test_survival_builder_blocks_future_draft_and_adp_observation_or_import_leakage(
    tmp_path: Path,
) -> None:
    repository = _survival_fixture(tmp_path)
    before_import = BASE + timedelta(hours=2, minutes=30)
    with pytest.raises(NotFoundError) as raised:
        repository.survival_inputs(
            before_import,
            draft_id="draft-1",
            league_id=None,
            sleeper_user_id="user-1",
            draft_slot=None,
            candidate_player_ids=("c-wr-2",),
            simulation_count=10,
            seed=1,
            model_version="adp-only-1.0",
            adp_source="local-adp",
        )
    assert raised.value.code == "missing_compatible_adp_snapshot"

    early, _ = repository.survival_inputs(
        BASE + timedelta(hours=4),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        candidate_player_ids=("c-wr-2",),
        simulation_count=10,
        seed=1,
        model_version="adp-only-1.0",
        adp_source="local-adp",
    )
    late, _ = repository.survival_inputs(
        BASE + timedelta(hours=7),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        candidate_player_ids=("c-wr-2",),
        simulation_count=10,
        seed=1,
        model_version="adp-only-1.0",
        adp_source="local-adp",
    )
    assert (early.draft.draft_snapshot_id, early.adp.adp_snapshot_id) == (
        "draft-base",
        "adp-base",
    )
    assert (late.draft.draft_snapshot_id, late.adp.adp_snapshot_id) == (
        "draft-future",
        "adp-future",
    )


@pytest.mark.parametrize(
    ("draft_id", "expected_code"),
    [
        ("draft-unresolved", "unresolved_completed_draft_pick"),
        ("draft-gap", "noncontiguous_completed_picks"),
    ],
)
def test_survival_builder_fails_closed_for_bad_completed_picks(
    tmp_path: Path, draft_id: str, expected_code: str
) -> None:
    repository = _survival_fixture(tmp_path)
    context = _context()
    if draft_id == "draft-unresolved":
        picks = [_pick(1, 1, "does-not-resolve", "user-1")]
    else:
        picks = [_pick(1, 1, "s-qb-1", "user-1"), _pick(3, 2, "s-wr-1", "user-2")]
    repository.insert(_draft(draft_id, draft_id, BASE + timedelta(hours=2), context, picks))
    with pytest.raises(InputError) as raised:
        repository.survival_inputs(
            BASE + timedelta(hours=4),
            draft_id=draft_id,
            league_id=None,
            sleeper_user_id="user-1",
            draft_slot=None,
            candidate_player_ids=("c-wr-2",),
            simulation_count=10,
            seed=1,
            model_version="adp-only-1.0",
            adp_source="local-adp",
        )
    assert raised.value.code == expected_code


def test_live_integration_on_clock_off_clock_and_consecutive_turns(tmp_path: Path) -> None:
    repository = _survival_fixture(tmp_path)
    off_clock, _ = repository.survival_inputs(
        BASE + timedelta(hours=4),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        candidate_player_ids=("c-wr-2",),
        simulation_count=20,
        seed=1,
        model_version="adp-only-1.0",
        adp_source="local-adp",
    )
    assert (off_clock.draft.simulation_start_pick, off_clock.draft.target_user_pick) == (3, 4)

    on_clock, _ = repository.survival_inputs(
        BASE + timedelta(hours=4),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-2",
        draft_slot=None,
        candidate_player_ids=("c-wr-2",),
        simulation_count=20,
        seed=1,
        model_version="adp-only-1.0",
        adp_source="local-adp",
    )
    assert on_clock.draft.user_is_on_the_clock is True
    assert (on_clock.draft.simulation_start_pick, on_clock.draft.target_user_pick) == (4, 6)

    consecutive = build_survival_response(
        repository,
        BASE + timedelta(hours=7),
        draft_id="draft-1",
        league_id=None,
        sleeper_user_id="user-1",
        draft_slot=None,
        candidate_player_ids=("c-wr-2",),
        simulation_count=20,
        seed=1,
        model_version="adp-only-1.0",
        adp_source="local-adp",
    )
    assert consecutive.simulation.intervening_opponent_pick_count == 0
    assert consecutive.simulation.candidates[0].simulated_availability_rate == 1.0


def test_candidate_statuses_and_explicit_model_variants(tmp_path: Path) -> None:
    repository = _survival_fixture(tmp_path)
    for model in (
        "adp-only-1.0",
        "adp-dispersion-1.0",
        "adp-dispersion-roster-1.0",
    ):
        result = build_survival_response(
            repository,
            BASE + timedelta(hours=4),
            draft_id="draft-1",
            league_id=None,
            sleeper_user_id="user-1",
            draft_slot=None,
            candidate_player_ids=("c-wr-2", "c-te-4", "c-qb-1", "unknown"),
            simulation_count=20,
            seed=7,
            model_version=model,
            adp_source="local-adp",
        )
        statuses = {row.canonical_player_id: row.status for row in result.simulation.candidates}
        assert statuses == {
            "c-wr-2": "modeled",
            "c-te-4": "missing_compatible_adp",
            "c-qb-1": "already_drafted",
            "unknown": "invalid_candidate",
        }


def test_cli_json_is_stable_defaults_to_adp_only_and_human_uses_simulated_rate(
    tmp_path: Path,
) -> None:
    repository = _survival_fixture(tmp_path)
    common = [
        "survival",
        "--draft-id",
        "draft-1",
        "--draft-slot",
        "1",
        "--player-id",
        "c-wr-2",
        "--simulations",
        "100",
        "--seed",
        "42",
        "--adp-source",
        "local-adp",
        "--as-of",
        (BASE + timedelta(hours=4)).isoformat(),
        "--db-path",
        str(repository.path),
    ]
    runner = CliRunner()
    first = runner.invoke(app, [*common, "--json"])
    repeated = runner.invoke(app, [*common, "--json"])
    human = runner.invoke(app, common)
    assert first.exit_code == repeated.exit_code == human.exit_code == 0
    assert first.stdout == repeated.stdout
    data = json.loads(first.stdout)["data"]
    assert data["simulation"]["model_version"] == "adp-only-1.0"
    assert data["simulation"]["simulation_count"] == 100
    assert data["provenance"]["draft"]["draft_snapshot_id"] == "draft-base"
    assert data["provenance"]["adp"]["adp_snapshot_id"] == "adp-base"
    assert "simulated availability rate" in human.stdout
    assert " probability" not in human.stdout.lower()

    default_count = runner.invoke(
        app,
        [argument for argument in common if argument not in {"--simulations", "100"}] + ["--json"],
    )
    assert default_count.exit_code == 0
    assert json.loads(default_count.stdout)["data"]["simulation"]["simulation_count"] == 5000


def test_mcp_survival_tool_is_read_only_network_free_and_matches_cli_canonical_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _survival_fixture(tmp_path)
    at = (BASE + timedelta(hours=4)).isoformat()
    cli = CliRunner().invoke(
        app,
        [
            "survival",
            "--draft-id",
            "draft-1",
            "--draft-slot",
            "1",
            "--player-id",
            "c-wr-2",
            "--simulations",
            "100",
            "--seed",
            "42",
            "--adp-source",
            "local-adp",
            "--as-of",
            at,
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    cli_data = json.loads(cli.stdout)["data"]
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=None,
        default_source="rotoworld",
        default_adp_source="local-adp",
    )
    before = _database_counts(repository.path)

    def forbid_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("MCP survival must not perform network I/O")

    monkeypatch.setattr(httpx.Client, "send", forbid_network)

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            tools = await client.list_tools()
            tool = next(item for item in tools.tools if item.name == "simulate_next_pick_survival")
            assert tool.annotations and tool.annotations.read_only_hint
            response = await client.call_tool(
                "simulate_next_pick_survival",
                {
                    "canonical_player_ids": ["c-wr-2"],
                    "simulation_count": 100,
                    "seed": 42,
                    "model": "adp-only-1.0",
                    "as_of": at,
                },
            )
            assert response.is_error is False
            assert response.structured_content["status"] == "ok"
            assert response.structured_content["data"] == cli_data

    asyncio.run(exercise())
    assert _database_counts(repository.path) == before


def test_missing_exact_adp_and_no_following_selection_are_explicit(tmp_path: Path) -> None:
    repository = _survival_fixture(tmp_path)
    with pytest.raises(NotFoundError) as missing:
        repository.survival_inputs(
            BASE + timedelta(hours=4),
            draft_id="draft-1",
            league_id=None,
            sleeper_user_id="user-1",
            draft_slot=None,
            candidate_player_ids=("c-wr-2",),
            simulation_count=10,
            seed=1,
            model_version="adp-only-1.0",
            adp_source="wrong-source",
        )
    assert missing.value.code == "missing_compatible_adp_snapshot"

    context = _context()
    player_ids = [
        f"s-{position.lower()}-{index}"
        for position in ("QB", "RB", "WR", "TE")
        for index in range(1, 5)
    ]
    before_last_user_pick = [
        _pick(
            number,
            1 if number in {1, 4, 5, 8, 9, 12, 13} else 2,
            player_ids[number - 1],
            "user-1" if number in {1, 4, 5, 8, 9, 12, 13} else "user-2",
        )
        for number in range(1, 15)
    ]
    repository.insert(
        _draft(
            "last-user-pick",
            "last-user-pick",
            BASE + timedelta(hours=2),
            context,
            before_last_user_pick,
        )
    )
    with pytest.raises(InputError) as no_following:
        repository.survival_inputs(
            BASE + timedelta(hours=4),
            draft_id="last-user-pick",
            league_id=None,
            sleeper_user_id="user-2",
            draft_slot=None,
            candidate_player_ids=("c-te-4",),
            simulation_count=10,
            seed=1,
            model_version="adp-only-1.0",
            adp_source="local-adp",
        )
    assert no_following.value.code == "no_following_user_pick"

    completed = [
        _pick(
            number,
            1 if number in {1, 4, 5, 8, 9, 12, 13, 16} else 2,
            f"s-qb-{(number - 1) % 4 + 1}",
            "user-1",
        )
        for number in range(1, 17)
    ]
    repository.insert(_draft("complete", "complete", BASE + timedelta(hours=2), context, completed))
    with pytest.raises(InputError) as complete:
        repository.survival_inputs(
            BASE + timedelta(hours=4),
            draft_id="complete",
            league_id=None,
            sleeper_user_id="user-1",
            draft_slot=1,
            candidate_player_ids=("c-wr-2",),
            simulation_count=10,
            seed=1,
            model_version="adp-only-1.0",
            adp_source="local-adp",
        )
    assert complete.value.code == "draft_complete"


def _survival_fixture(tmp_path: Path) -> IntelligenceRepository:
    repository = _fixture(tmp_path)
    _adp(repository, "adp-base", BASE + timedelta(hours=1), BASE + timedelta(hours=3), 0)
    _adp(repository, "adp-future", BASE + timedelta(hours=6), BASE + timedelta(hours=6), 10)
    return repository


def _adp(
    repository: IntelligenceRepository,
    snapshot_id: str,
    observed_at: Any,
    imported_at: Any,
    offset: int,
) -> None:
    player_ids = [
        f"c-{position.lower()}-{index}"
        for position in ("QB", "RB", "WR", "TE")
        for index in range(1, 5)
        if not (position == "TE" and index == 4)
    ]
    snapshot = AdpSnapshot(
        adp_snapshot_id=snapshot_id,
        source="local-adp",
        source_version=snapshot_id,
        season="2026",
        league_size=2,
        scoring_format="ppr",
        draft_type="snake",
        observed_at=observed_at,
        imported_at=imported_at,
        payload_hash=snapshot_id,
        identity_resolver_version="2.0",
        original_filename="synthetic.csv",
        total_row_count=len(player_ids),
        matched_row_count=len(player_ids),
        unresolved_row_count=0,
        ambiguous_row_count=0,
    )
    entries = [
        {
            "source_row_number": index,
            "canonical_player_id": player_id,
            "player_name": player_id,
            "position": player_id.split("-")[1].upper(),
            "team": "FA",
            "overall_adp": float(index + offset),
            "adp_sd": 2.0,
            "sample_size": 100,
            "match_status": "matched",
            "match_method": "strict_identity",
            "raw_payload": {},
        }
        for index, player_id in enumerate(player_ids, 1)
    ]
    repository.insert_adp_snapshot(snapshot, entries, [])


def _context() -> dict[str, Any]:
    return {
        "league_id": "league-1",
        "season": "2026",
        "settings": {"type": 0},
        "scoring_settings": {"rec": 1, "rush_yd": 0.1, "rec_yd": 0.1, "pass_yd": 0.04},
        "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "BN", "K", "DEF"],
    }


def _database_counts(path: Path) -> tuple[int, ...]:
    with duckdb.connect(str(path), read_only=True) as connection:
        return tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("draft_snapshots", "adp_snapshots", "player_directory_snapshots")
        )
