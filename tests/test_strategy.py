from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from mcp import Client
from pydantic import ValidationError
from rich.console import Console
from typer.testing import CliRunner

from fantasy_war_room.cli import app
from fantasy_war_room.decision.models import (
    CompletedDraftPick,
    ExpertRankingInput,
    RecommendationInputs,
    RecommendationPlayerInput,
    RecommendationProvenance,
    RosterConfiguration,
)
from fantasy_war_room.decision.recommend import recommend
from fantasy_war_room.errors import InputError
from fantasy_war_room.market import build_market_context, build_opponent_demand
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.server import create_server
from fantasy_war_room.mcp.service import DraftCopilotService
from fantasy_war_room.strategy.adjust import apply_strategy, validate_strategy_compatibility
from fantasy_war_room.strategy.load import default_strategy_profile, profile_hash
from fantasy_war_room.strategy.presentation import limit_strategy_result


def test_brown_hard_gate_and_provisional_promotion_ceiling() -> None:
    profile = _profile()
    round_one = _inputs()
    raw_one = recommend(round_one, "trusted-board-1.1")
    gated = apply_strategy(raw_one, round_one, profile)

    brown = next(target for target in gated.targets if target.player_name == "Chase Brown")
    assert brown.state == "too_early"
    assert {row.raw_candidate.player_name for row in gated.prohibited_candidates} >= {"Chase Brown"}

    round_two = _inputs(
        completed=(
            CompletedDraftPick(pick_no=1, draft_slot=1),
            CompletedDraftPick(pick_no=2, draft_slot=2),
        )
    )
    raw_two = recommend(round_two, "trusted-board-1.1")
    raw_brown = next(row for row in raw_two.candidates if row.player_name == "Chase Brown")
    raw_leader = raw_two.candidates[0]
    profile = profile.model_copy(
        update={
            "targets": tuple(
                target.model_copy(
                    update={
                        "max_raw_score_deficit": 5.0,
                        "max_raw_rank_displacement": 2,
                    }
                )
                for target in profile.targets
            )
        }
    )
    assert raw_leader.recommendation_score - raw_brown.recommendation_score > 5.0
    rejected = apply_strategy(raw_two, round_two, profile)
    rejected_brown = next(
        row for row in rejected.candidates if row.raw_candidate.player_name == "Chase Brown"
    )
    assert rejected_brown.target_promotion_class == "no_promotion"

    profile = profile.model_copy(
        update={
            "targets": tuple(
                target.model_copy(update={"max_raw_score_deficit": 7.0})
                if target.player_name == "Chase Brown"
                else target
                for target in profile.targets
            )
        }
    )
    adjusted = apply_strategy(raw_two, round_two, profile)
    brown_candidate = next(
        row for row in adjusted.candidates if row.raw_candidate.player_name == "Chase Brown"
    )
    brown_target = next(row for row in adjusted.targets if row.player_name == "Chase Brown")

    assert brown_target.state == "in_window"
    assert brown_target.raw_score_deficit is not None
    assert brown_target.raw_score_deficit <= 7.0
    assert brown_target.raw_rank_displacement is not None
    assert brown_target.raw_rank_displacement <= 2
    assert brown_candidate.target_promotion_class == "eligible_target_within_cost"
    assert brown_candidate.strategy_rank == 1
    assert brown_candidate.raw_score == next(
        row.recommendation_score for row in raw_two.candidates if row.player_name == "Chase Brown"
    )


def test_market_context_is_descriptive_deterministic_and_limit_independent() -> None:
    inputs = _inputs().model_copy(update={"draft_slot": 2})
    raw = recommend(inputs, "trusted-board-1.1")
    kyler = next(
        player for player in inputs.projected_players if player.player_name == "Kyler Murray"
    )
    adp = {
        "snapshot": {"adp_snapshot_id": "adp-1", "source": "local-adp"},
        "entries": {kyler.canonical_player_id: {"overall_adp": 65.0}},
    }
    schedule = {"snapshot": {"schedule_snapshot_id": "schedule-1"}, "entries": {"ARI": 8}}
    first = build_market_context(
        raw,
        inputs,
        draft_snapshot_id="draft-1",
        adp=adp,
        schedule=schedule,
        manual_windows={kyler.canonical_player_id: (None, None)},
    )
    second = build_market_context(
        raw.model_copy(update={"candidates": tuple(reversed(raw.candidates))}),
        inputs,
        draft_snapshot_id="draft-1",
        adp=adp,
        schedule=schedule,
        manual_windows={kyler.canonical_player_id: (None, None)},
    )
    by_id = {row.canonical_player_id: row for row in first.players}
    assert by_id[kyler.canonical_player_id].classification in {"too_early", "market_reach"}
    assert by_id[kyler.canonical_player_id].overall_adp == 65.0
    assert "probability" not in first.model_dump_json()
    assert {row.canonical_player_id: row for row in first.players} == {
        row.canonical_player_id: row for row in second.players
    }


def test_opponent_demand_handles_zero_intervening_and_is_not_probability() -> None:
    inputs = _inputs().model_copy(update={"draft_slot": 2})
    raw = recommend(inputs, "trusted-board-1.1")
    demand = build_opponent_demand(raw, inputs, draft_snapshot_id="draft-1", adp=None)
    assert demand.intervening_picks == 1
    assert sum(demand.position_pressure.values()) == demand.intervening_picks
    assert "probability" not in demand.model_dump_json()
    on_clock = inputs.model_copy(update={"draft_slot": 1})
    on_clock_raw = recommend(on_clock, "trusted-board-1.1")
    zero = build_opponent_demand(on_clock_raw, on_clock, draft_snapshot_id="draft-1", adp=None)
    assert zero.intervening_picks == 0
    assert zero.opponent_details == ()


def test_mcbride_round_gate_loveland_fallback_and_kyler_deferred() -> None:
    profile = _profile()
    inputs = _inputs(completed=(CompletedDraftPick(pick_no=1, draft_slot=1),))
    result = apply_strategy(recommend(inputs, "trusted-board-1.1"), inputs, profile)
    states = {target.player_name: target.state for target in result.targets}

    assert states["Trey McBride"] == "too_early"
    assert states["Colston Loveland"] == "fallback_inactive"
    assert states["Kyler Murray"] == "deferred_pending_market_context"

    mcbride_id = next(
        row.canonical_player_id
        for row in inputs.projected_players
        if row.player_name == "Trey McBride"
    )
    missed_inputs = _inputs(
        completed=(
            CompletedDraftPick(pick_no=1, draft_slot=2, canonical_player_id=mcbride_id),
            CompletedDraftPick(pick_no=2, draft_slot=1),
            CompletedDraftPick(pick_no=3, draft_slot=2),
            CompletedDraftPick(pick_no=4, draft_slot=1),
            CompletedDraftPick(pick_no=5, draft_slot=2),
            CompletedDraftPick(pick_no=6, draft_slot=1),
            CompletedDraftPick(pick_no=7, draft_slot=2),
            CompletedDraftPick(pick_no=8, draft_slot=1),
        )
    )
    missed = apply_strategy(recommend(missed_inputs, "trusted-board-1.1"), missed_inputs, profile)
    missed_states = {target.player_name: target.state for target in missed.targets}
    assert missed_states["Trey McBride"] == "selected_by_opponent"
    assert missed_states["Colston Loveland"] == "in_window"


def test_qb2_te2_demotion_te3_prohibition_and_completion_guard() -> None:
    profile = _profile()
    players = _players()
    ids = {row.player_name: row.canonical_player_id for row in players}
    inputs = _inputs(
        players=players,
        draft_rounds=4,
        completed=(
            CompletedDraftPick(
                pick_no=1, draft_slot=1, canonical_player_id=ids["QB One"], position="QB"
            ),
            CompletedDraftPick(
                pick_no=2, draft_slot=1, canonical_player_id=ids["TE One"], position="TE"
            ),
            CompletedDraftPick(
                pick_no=3, draft_slot=1, canonical_player_id=ids["TE Two"], position="TE"
            ),
        ),
    )
    result = apply_strategy(recommend(inputs, "trusted-board-1.1"), inputs, profile)
    by_name = {row.raw_candidate.player_name: row for row in result.evaluated_candidates}

    suppressed_qb = next(
        row for row in result.prohibited_candidates if row.raw_candidate.player_name == "QB Two"
    )
    assert suppressed_qb.positional_utility_class == "reserved_position_suppressed"
    assert "TE Two" not in by_name
    assert "TE Three" in {row.raw_candidate.player_name for row in result.prohibited_candidates}
    assert by_name["RB One"].eligible is True
    assert result.roster_completion_required is True
    assert result.actionable is False
    assert result.actionable_choice is None
    assert result.candidates == ()
    assert result.evaluated_candidates
    assert result.directive is not None
    assert result.directive.code == "roster_completion_required"
    assert result.directive.boundary_status == "already_impossible"
    assert result.remaining_user_selections == 1
    assert result.unfilled_required_positions == ("K", "DEF")


def test_reserved_kyler_target_suppresses_other_qbs_without_promoting_kyler() -> None:
    inputs = _inputs(completed=(CompletedDraftPick(pick_no=1, draft_slot=2),))
    raw = _raw_with_order(inputs, ("QB One", "WR One", "Kyler Murray"))
    result = apply_strategy(raw, inputs, _profile())
    by_name = {
        row.raw_candidate.player_name: row
        for row in (*result.evaluated_candidates, *result.prohibited_candidates)
    }

    assert raw.candidates[0].player_name == "QB One"
    assert result.actionable_choice is not None
    assert result.actionable_choice.raw_candidate.position != "QB"
    assert by_name["QB One"].positional_utility_class == "reserved_position_suppressed"
    assert "reserved_position_target_active" in by_name["QB One"].reason_codes
    assert by_name["Kyler Murray"].target_promotion_class == "no_promotion"
    assert next(
        target for target in result.targets if target.player_name == "Kyler Murray"
    ).state == ("deferred_pending_market_context")
    reserved = result.reserved_position_targets[0]
    assert reserved.active is True and reserved.suppression_applied is True


def test_reserved_qb_target_ends_when_missed_or_acquired_and_can_be_abandoned() -> None:
    players = _players()
    ids = {row.player_name: row.canonical_player_id for row in players}
    opponent = _inputs(
        players=players,
        completed=(
            CompletedDraftPick(
                pick_no=1,
                draft_slot=2,
                canonical_player_id=ids["Kyler Murray"],
                position="QB",
            ),
        ),
    )
    opponent_result = apply_strategy(
        _raw_with_order(opponent, ("QB One", "WR One")), opponent, _profile()
    )
    assert opponent_result.actionable_choice is not None
    assert opponent_result.actionable_choice.raw_candidate.player_name == "QB One"
    assert opponent_result.reserved_position_targets[0].active is False

    acquired = _inputs(
        players=players,
        completed=(
            CompletedDraftPick(
                pick_no=1,
                draft_slot=1,
                canonical_player_id=ids["Kyler Murray"],
                position="QB",
            ),
        ),
    )
    acquired_result = apply_strategy(
        _raw_with_order(acquired, ("QB One", "WR One")), acquired, _profile()
    )
    acquired_qb = next(
        row
        for row in acquired_result.evaluated_candidates
        if row.raw_candidate.player_name == "QB One"
    )
    assert acquired_qb.positional_utility_class == "redundant_qb_depth"
    assert acquired_result.reserved_position_targets[0].active is False

    abandoned = _profile().model_copy(update={"reserved_position_targets": ()})
    available = _inputs(completed=(CompletedDraftPick(pick_no=1, draft_slot=2),))
    abandoned_result = apply_strategy(
        _raw_with_order(available, ("QB One", "WR One")), available, abandoned
    )
    assert abandoned_result.actionable_choice is not None
    assert abandoned_result.actionable_choice.raw_candidate.player_name == "QB One"


def test_reserved_target_market_window_is_capped_by_last_feasible_acquisition_pick() -> None:
    inputs = _slot_seven_inputs_before(114)
    result = apply_strategy(
        recommend(inputs, "trusted-board-1.1"),
        inputs,
        _slot_seven_profile(),
        market_context=_kyler_market(inputs, classification="too_early"),
    )
    kyler = next(target for target in result.targets if target.player_name == "Kyler Murray")

    assert kyler.latest_feasible_acquisition_pick == 127
    assert kyler.state == "too_early"
    assert result.reserved_position_targets[0].latest_feasible_acquisition_pick == 127


def test_reserved_target_becomes_actionable_at_position_deadline() -> None:
    inputs = _slot_seven_inputs_before(127)
    raw = _raw_with_order(inputs, ("WR One", "Kyler Murray", "QB One"))
    result = apply_strategy(
        raw,
        inputs,
        _slot_seven_profile(),
        market_context=_kyler_market(inputs, classification="too_early"),
    )
    kyler = next(target for target in result.targets if target.player_name == "Kyler Murray")

    assert kyler.state == "last_reasonable_chance"
    assert "Roster feasibility moved the action point earlier" in kyler.reason
    assert "probability" not in kyler.reason.casefold()
    assert result.actionable_choice is not None
    assert result.actionable_choice.raw_candidate.player_name == "Kyler Murray"
    assert result.actionable_choice.target_promotion_class == "reserved_position_deadline"


def test_reserved_target_uses_normal_market_window_before_deadline() -> None:
    inputs = _slot_seven_inputs_before(107)
    result = apply_strategy(
        recommend(inputs, "trusted-board-1.1"),
        inputs,
        _slot_seven_profile(),
        market_context=_kyler_market(inputs, classification="in_effective_window"),
    )
    kyler = next(target for target in result.targets if target.player_name == "Kyler Murray")

    assert kyler.latest_feasible_acquisition_pick == 127
    assert kyler.state == "in_window"
    assert kyler.reason == "Current round is inside the effective target window"


def test_opponent_drafting_reserved_target_before_deadline_releases_position() -> None:
    players = _players()
    kyler_id = next(row.canonical_player_id for row in players if row.player_name == "Kyler Murray")
    inputs = _slot_seven_inputs_before(107, opponent_target_id=kyler_id)
    raw = _raw_with_order(inputs, ("QB One", "WR One"))
    result = apply_strategy(
        raw,
        inputs,
        _slot_seven_profile(),
        market_context=_kyler_market(inputs, classification="too_early"),
    )

    assert result.reserved_position_targets[0].target_state == "selected_by_opponent"
    assert result.reserved_position_targets[0].active is False
    assert result.actionable_choice is not None
    assert result.actionable_choice.raw_candidate.player_name == "QB One"


def test_reserved_target_deadline_preserves_exact_k_def_completion_boundary() -> None:
    inputs = _slot_seven_inputs_before(134)
    result = apply_strategy(
        recommend(inputs, "trusted-board-1.1"),
        inputs,
        _slot_seven_profile(),
        market_context=_kyler_market(inputs, classification="too_early"),
    )

    assert result.roster_completion_required is True
    assert result.actionable is False
    assert result.directive is not None
    assert result.directive.boundary_status == "exact_boundary"
    assert result.remaining_user_selections == 2
    assert result.unfilled_required_positions == ("K", "DEF")


def test_reserved_target_deadline_is_deterministic_and_decision_time_independent() -> None:
    inputs = _slot_seven_inputs_before(127)
    later_as_of = inputs.model_copy(update={"decision_at": inputs.decision_at + timedelta(hours=1)})
    first = apply_strategy(
        recommend(inputs, "trusted-board-1.1"),
        inputs,
        _slot_seven_profile(),
        market_context=_kyler_market(inputs, classification="too_early"),
    )
    second = apply_strategy(
        recommend(later_as_of, "trusted-board-1.1"),
        later_as_of,
        _slot_seven_profile(),
        market_context=_kyler_market(later_as_of, classification="too_early"),
    )

    assert first.targets == second.targets
    assert first.reserved_position_targets == second.reserved_position_targets


def test_te2_uses_lineup_flex_value_then_bench_value_then_redundancy() -> None:
    players = _players()
    ids = {row.player_name: row.canonical_player_id for row in players}
    te1_pick = CompletedDraftPick(
        pick_no=1,
        draft_slot=1,
        canonical_player_id=ids["TE One"],
        position="TE",
    )
    lineup_inputs = _inputs(players=players, completed=(te1_pick,))
    lineup = apply_strategy(
        recommend(lineup_inputs, "trusted-board-1.1"), lineup_inputs, _profile()
    )
    lineup_by_name = {row.raw_candidate.player_name: row for row in lineup.evaluated_candidates}
    assert lineup_by_name["Colston Loveland"].positional_utility_class == "te2_starter_or_flex"
    assert "te2_improves_starting_lineup" in lineup_by_name["Colston Loveland"].reason_codes
    assert next(
        target for target in lineup.targets if target.player_name == "Colston Loveland"
    ).state == ("fallback_inactive")

    full_roster = (
        CompletedDraftPick(
            pick_no=1,
            draft_slot=1,
            canonical_player_id=ids["Trey McBride"],
            position="TE",
        ),
        CompletedDraftPick(
            pick_no=2,
            draft_slot=1,
            canonical_player_id=ids["Amon-Ra St. Brown"],
            position="WR",
        ),
        CompletedDraftPick(
            pick_no=3,
            draft_slot=1,
            canonical_player_id=ids["Chase Brown"],
            position="RB",
        ),
        CompletedDraftPick(
            pick_no=4,
            draft_slot=1,
            canonical_player_id=ids["WR One"],
            position="WR",
        ),
    )
    bench_inputs = _inputs(players=players, completed=full_roster)
    bench_raw = _raw_with_order(bench_inputs, ("Colston Loveland", "RB One", "TE Two"))
    bench = apply_strategy(bench_raw, bench_inputs, _profile())
    bench_by_name = {row.raw_candidate.player_name: row for row in bench.evaluated_candidates}
    assert bench_by_name["Colston Loveland"].raw_candidate.roster_effect.category == "bench_depth"
    assert bench_by_name["Colston Loveland"].positional_utility_class == "te2_bench_value"
    assert bench_by_name["TE Two"].positional_utility_class == "redundant_te_depth"
    assert bench_by_name["Colston Loveland"].strategy_rank < bench_by_name["TE Two"].strategy_rank


def test_te3_remains_prohibited_and_value_summary_is_limit_invariant() -> None:
    players = _players()
    ids = {row.player_name: row.canonical_player_id for row in players}
    inputs = _inputs(
        players=players,
        completed=(
            CompletedDraftPick(
                pick_no=1, draft_slot=1, canonical_player_id=ids["TE One"], position="TE"
            ),
            CompletedDraftPick(
                pick_no=2, draft_slot=1, canonical_player_id=ids["TE Two"], position="TE"
            ),
        ),
    )
    raw = _raw_with_order(inputs, ("TE Three", "QB One", "RB One", "WR One"))
    complete = apply_strategy(raw, inputs, _profile())
    limited = limit_strategy_result(complete, 1)

    assert "TE Three" in {row.raw_candidate.player_name for row in complete.prohibited_candidates}
    assert complete.value_summary == limited.value_summary
    assert complete.raw_recommendation == limited.raw_recommendation == raw
    summary = complete.value_summary
    assert summary.best_available_by_position["TE"] is not None
    assert summary.best_available_by_position["TE"].player_name == "TE Three"
    assert summary.highest_raw_ranked_suppressed_candidate is not None
    assert summary.highest_raw_ranked_suppressed_candidate.player_name == "TE Three"
    assert "adp" not in json.dumps(summary.model_dump(mode="json")).casefold()


def test_value_summary_is_deterministic_for_input_order_and_surfaces_te2_value() -> None:
    players = tuple(
        player.model_copy(
            update={
                "player_name": "Mark Andrews",
                "league_projected_points": 195.0,
                "league_known_component_points": 195.0,
                "cbs_projected_points": 195.0,
            }
        )
        if player.player_name == "TE Two"
        else player
        for player in _players()
    )
    ids = {row.player_name: row.canonical_player_id for row in players}
    completed = (
        CompletedDraftPick(
            pick_no=1,
            draft_slot=1,
            canonical_player_id=ids["Trey McBride"],
            position="TE",
        ),
        CompletedDraftPick(
            pick_no=2,
            draft_slot=1,
            canonical_player_id=ids["Amon-Ra St. Brown"],
            position="WR",
        ),
        CompletedDraftPick(
            pick_no=3, draft_slot=1, canonical_player_id=ids["Chase Brown"], position="RB"
        ),
        CompletedDraftPick(
            pick_no=4, draft_slot=1, canonical_player_id=ids["WR One"], position="WR"
        ),
    )
    inputs = _inputs(players=players, completed=completed)
    raw = _raw_with_order(inputs, ("Mark Andrews", "RB One", "WR Two"))
    first = apply_strategy(raw, inputs, _profile())
    reordered_inputs = inputs.model_copy(update={"projected_players": tuple(reversed(players))})
    repeated = apply_strategy(raw, reordered_inputs, _profile())

    assert first.value_summary == repeated.value_summary
    affected = first.value_summary.highest_raw_ranked_redundancy_affected_candidate
    assert affected is not None
    assert affected.player_name == "Mark Andrews"
    assert affected.positional_utility_class == "te2_bench_value"


@pytest.mark.parametrize(
    ("draft_rounds", "positions", "expected_status", "expected_missing"),
    [
        (4, ("QB", "TE"), "exact_boundary", ("K", "DEF")),
        (3, ("QB", "K"), "exact_boundary", ("DEF",)),
        (5, ("QB", "TE"), None, ("K", "DEF")),
        (3, ("QB", "TE"), "already_impossible", ("K", "DEF")),
    ],
)
def test_roster_completion_boundaries(
    draft_rounds: int,
    positions: tuple[str, ...],
    expected_status: str | None,
    expected_missing: tuple[str, ...],
) -> None:
    inputs = _inputs(
        draft_rounds=draft_rounds,
        completed=tuple(
            CompletedDraftPick(pick_no=index, draft_slot=1, position=position)
            for index, position in enumerate(positions, start=1)
        ),
    )
    raw = recommend(inputs, "trusted-board-1.1")
    result = apply_strategy(raw, inputs, _profile())

    assert result.raw_recommendation == raw
    assert result.unfilled_required_positions == expected_missing
    assert result.actionable is (expected_status is None)
    if expected_status is None:
        assert result.directive is None
        assert result.actionable_choice == result.candidates[0]
    else:
        assert result.actionable_choice is None
        assert result.candidates == ()
        assert result.evaluated_candidates
        assert result.directive is not None
        assert result.directive.boundary_status == expected_status
        assert result.directive.required_position_count == len(expected_missing)


@pytest.mark.parametrize("limit", [1, 5, 10, 100])
def test_strategy_limit_is_presentation_only(limit: int) -> None:
    inputs = _inputs(
        completed=(
            CompletedDraftPick(pick_no=1, draft_slot=1),
            CompletedDraftPick(pick_no=2, draft_slot=2),
        )
    )
    complete = apply_strategy(recommend(inputs, "trusted-board-1.1"), inputs, _profile())
    limited = limit_strategy_result(complete, limit)

    assert len(limited.candidates) == min(limit, len(complete.candidates))
    assert limited.candidates == complete.candidates[:limit]
    assert limited.evaluated_candidates == complete.evaluated_candidates
    assert limited.raw_recommendation == complete.raw_recommendation
    assert limited.targets == complete.targets
    assert limited.actionable == complete.actionable


def test_strategy_requires_raw_model_source_slot_and_roster_compatibility() -> None:
    inputs = _inputs()
    profile = _profile().model_copy(update={"compatible_draft_slots": (7,)})
    with pytest.raises(InputError) as raised:
        apply_strategy(recommend(inputs, "trusted-board-1.1"), inputs, profile)
    assert raised.value.code == "strategy_profile_incompatible"
    assert "draft_slot" in (raised.value.details or {})["failed_constraints"]


@pytest.mark.parametrize(
    ("updates", "constraint"),
    [
        ({"draft_type": "auction"}, "draft_type"),
        ({"draft_type": "linear"}, "draft_type"),
        ({"scoring_format": "half_ppr"}, "scoring_format"),
        ({"scoring_format": "standard"}, "scoring_format"),
        ({"league_type": "keeper", "keeper_status": "keeper"}, "league_type"),
        ({"league_type": "dynasty", "keeper_status": "keeper"}, "league_type"),
        ({"team_count": 12}, "team_count"),
        ({"draft_slot": 2}, "draft_slot"),
    ],
)
def test_strategy_rejects_incompatible_format(updates: dict[str, object], constraint: str) -> None:
    inputs = _inputs().model_copy(update=updates)
    with pytest.raises(InputError) as raised:
        validate_strategy_compatibility(inputs, _profile(), raw_model="trusted-board-1.1")
    assert raised.value.code == "strategy_profile_incompatible"
    assert constraint in (raised.value.details or {})["failed_constraints"]
    assert (raised.value.details or {})["profile_name"] == "logan-ppr-2flex-1.0"


@pytest.mark.parametrize(
    "roster",
    [
        RosterConfiguration(qb=2, rb=1, wr=1, te=1, flex=1),
        RosterConfiguration(qb=1, rb=1, wr=1, te=2, flex=1),
        RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=2),
    ],
)
def test_strategy_rejects_wrong_roster_structure(roster: RosterConfiguration) -> None:
    inputs = _inputs().model_copy(update={"roster": roster})
    with pytest.raises(InputError) as raised:
        validate_strategy_compatibility(inputs, _profile(), raw_model="trusted-board-1.1")
    assert raised.value.code == "strategy_profile_incompatible"


def test_committed_profile_declares_every_compatibility_field() -> None:
    path = (
        Path(__file__).parents[1]
        / "src/fantasy_war_room/strategy/profiles/logan-ppr-2flex-1.0.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert {
        "sport",
        "team_count",
        "compatible_draft_slots",
        "draft_type",
        "league_type",
        "keeper_status",
        "scoring_format",
        "qb",
        "te",
        "flex",
        "k",
        "defense",
    } <= payload.keys()
    assert payload["schema_version"] == "1.1"
    assert payload["reserved_position_targets"][0]["target_player_name"] == "Kyler Murray"
    profile = default_strategy_profile()
    assert profile.schema_version == "1.1"
    assert profile.reserved_position_targets[0].target_player_name == "Kyler Murray"
    assert profile_hash(profile) == profile_hash(default_strategy_profile())


def test_reserved_target_and_te2_value_policy_are_validated() -> None:
    payload = default_strategy_profile().model_dump(mode="json")
    payload["reserved_position_targets"][0]["target_player_name"] = "Unknown Target"
    with pytest.raises(ValidationError, match="is not a configured target"):
        type(default_strategy_profile()).model_validate(payload)

    payload = default_strategy_profile().model_dump(mode="json")
    payload["te2_policy"]["max_bench_value_raw_score_deficit"] = -1
    with pytest.raises(ValidationError):
        type(default_strategy_profile()).model_validate(payload)


def test_completion_human_rendering_hides_offensive_leader(monkeypatch) -> None:
    from fantasy_war_room import rendering

    inputs = _inputs(
        draft_rounds=2,
        completed=(),
    )
    result = apply_strategy(recommend(inputs, "trusted-board-1.1"), inputs, _profile())
    output = StringIO()
    monkeypatch.setattr(rendering, "stdout", Console(file=output, force_terminal=False))
    rendering.render_recommendation(result)
    text = output.getvalue()
    assert "Roster completion required" in text
    assert "No offensive strategy recommendation is actionable" in text
    assert result.raw_recommendation.candidates[0].player_name not in text


def test_strategy_mcp_adds_dynamic_tool_and_preserves_complete_raw_result(tmp_path) -> None:
    from test_recommend_integration import BASE, _fixture

    repository = _fixture(tmp_path)
    profile = _profile(team_count=2, flex=1)
    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
        strategy_profile=profile,
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            assert client.instructions is not None
            assert profile.profile_name in client.instructions
            listing = await client.list_tools()
            assert "get_draft_strategy" in {tool.name for tool in listing.tools}
            strategy = await client.call_tool(
                "get_draft_strategy", {"as_of": (BASE.replace(tzinfo=UTC)).isoformat()}
            )
            assert strategy.is_error is True  # intelligence imports are not visible at BASE
            recommendation = await client.call_tool(
                "recommend_pick",
                {
                    "source": "rotoworld",
                    "as_of": (BASE + timedelta(hours=4)).isoformat(),
                },
            )
            assert recommendation.is_error is False
            data = recommendation.structured_content["data"]
            assert data["raw_recommendation"]["schema_version"] == "1.1"
            assert len(data["raw_recommendation"]["candidates"]) >= len(data["candidates"])
            assert data["value_summary"]["best_available_by_position"]
            assert data["reserved_position_targets"]
            assert "exclusive planned QB target" in client.instructions
            assert "TE2 is value-sensitive" in client.instructions

    asyncio.run(exercise())


def test_completion_cli_json_human_and_mcp_are_non_actionable(tmp_path) -> None:
    from test_recommend_integration import BASE, ROSTER, SCORING, _draft, _fixture, _pick

    repository = _fixture(tmp_path / "fixture")
    context = {
        "league_id": "league-1",
        "season": "2026",
        "settings": {"type": 0},
        "scoring_settings": SCORING,
        "roster_positions": ROSTER,
    }
    picks = [
        _pick(number, 1 if number % 2 else 2, f"unknown-{number}", f"user-{number % 2}")
        for number in range(1, 13)
    ]
    repository.insert(_draft("completion", "draft-1", BASE + timedelta(hours=5), context, picks))
    profile = _profile(team_count=2, flex=1)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    common = [
        "recommend",
        "--draft-id",
        "draft-1",
        "--draft-slot",
        "1",
        "--strategy",
        str(profile_path),
        "--as-of",
        (BASE + timedelta(hours=5)).isoformat(),
        "--db-path",
        str(repository.path),
    ]
    runner = CliRunner()
    json_result = runner.invoke(app, [*common, "--json"])
    assert json_result.exit_code == 0, json_result.output
    data = json.loads(json_result.stdout)["data"]
    assert data["actionable"] is False
    assert data["actionable_choice"] is None
    assert data["candidates"] == []
    assert data["directive"]["code"] == "roster_completion_required"
    assert data["raw_recommendation"]["candidates"]

    human = runner.invoke(app, common)
    assert human.exit_code == 0, human.output
    assert "Roster completion required" in human.stdout
    assert "No offensive strategy recommendation is actionable" in human.stdout

    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
        strategy_profile=profile,
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "recommend_pick",
                {
                    "source": "rotoworld",
                    "as_of": (BASE + timedelta(hours=5)).isoformat(),
                    "limit": 1,
                },
            )
            assert result.is_error is False
            body = result.structured_content["data"]
            assert body["actionable"] is False
            assert body["actionable_choice"] is None
            assert body["candidates"] == []
            assert body["raw_recommendation"]["candidates"]

    asyncio.run(exercise())


def test_cli_and_mcp_apply_equivalent_strategy_limit(tmp_path) -> None:
    from test_recommend_integration import BASE, _fixture

    repository = _fixture(tmp_path / "fixture")
    profile = _profile(team_count=2, flex=1)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    runner = CliRunner()
    cli_result = runner.invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "draft-1",
            "--draft-slot",
            "1",
            "--strategy",
            str(profile_path),
            "--limit",
            "1",
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    cli_data = json.loads(cli_result.stdout)["data"]
    assert len(cli_data["candidates"]) == 1

    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
        strategy_profile=profile,
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "recommend_pick",
                {
                    "source": "rotoworld",
                    "limit": 1,
                    "as_of": (BASE + timedelta(hours=4)).isoformat(),
                },
            )
            assert result.is_error is False
            mcp_data = result.structured_content["data"]
            assert mcp_data["candidates"] == cli_data["candidates"]
            assert mcp_data["evaluated_candidates"] == cli_data["evaluated_candidates"]
            assert mcp_data["raw_recommendation"] == cli_data["raw_recommendation"]

    asyncio.run(exercise())


def test_cli_and_mcp_return_structured_profile_incompatibility(tmp_path) -> None:
    from test_recommend_integration import BASE, _fixture

    repository = _fixture(tmp_path / "fixture")
    incompatible = default_strategy_profile().model_copy(
        update={"required_ranking_source": "rotoworld"}
    )
    profile_path = tmp_path / "incompatible.json"
    profile_path.write_text(incompatible.model_dump_json(indent=2), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "draft-1",
            "--draft-slot",
            "1",
            "--strategy",
            str(profile_path),
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert result.exit_code != 0
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "strategy_profile_incompatible"
    assert "team_count" in error["details"]["failed_constraints"]

    raw = runner.invoke(
        app,
        [
            "recommend",
            "--draft-id",
            "draft-1",
            "--draft-slot",
            "1",
            "--source",
            "rotoworld",
            "--model",
            "trusted-board-1.1",
            "--as-of",
            (BASE + timedelta(hours=4)).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert raw.exit_code == 0

    service = DraftCopilotService(
        McpReadRepository(repository.path),
        draft_id="draft-1",
        sleeper_user_id="user-1",
        draft_slot=1,
        default_source="rotoworld",
        strategy_profile=incompatible,
    )

    async def exercise() -> None:
        async with Client(create_server(service)) as client:
            response = await client.call_tool(
                "recommend_pick",
                {"source": "rotoworld", "as_of": (BASE + timedelta(hours=4)).isoformat()},
            )
            assert response.is_error is True
            assert response.structured_content["error"]["code"] == ("strategy_profile_incompatible")

    asyncio.run(exercise())


def _profile(*, team_count: int = 2, flex: int = 1):
    return default_strategy_profile().model_copy(
        update={
            "required_ranking_source": "rotoworld",
            "team_count": team_count,
            "compatible_draft_slots": (1,),
            "flex": flex,
        }
    )


def _raw_with_order(inputs: RecommendationInputs, names: tuple[str, ...]):
    raw = recommend(inputs, "trusted-board-1.1")
    by_name = {candidate.player_name: candidate for candidate in raw.candidates}
    selected = [by_name[name] for name in names]
    selected_ids = {candidate.canonical_player_id for candidate in selected}
    ordered = selected + [
        candidate
        for candidate in raw.candidates
        if candidate.canonical_player_id not in selected_ids
    ]
    return raw.model_copy(
        update={
            "candidates": tuple(
                candidate.model_copy(update={"recommendation_rank": rank})
                for rank, candidate in enumerate(ordered, start=1)
            )
        }
    )


def _slot_seven_profile():
    return _profile(team_count=10, flex=2).model_copy(update={"compatible_draft_slots": (7,)})


def _slot_seven_inputs_before(
    next_pick: int, *, opponent_target_id: str | None = None
) -> RecommendationInputs:
    base_players = _players()
    filler_players = tuple(
        RecommendationPlayerInput(
            canonical_player_id=f"filler-{position.casefold()}-{index}",
            player_name=f"Filler {position} {index}",
            position=position,
            team="FA",
            league_projected_points=150.0 - index,
            cbs_projected_points=150.0 - index,
            scoring_completeness="complete",
        )
        for position in ("QB", "RB", "WR", "TE")
        for index in range(1, 31)
    )
    inputs = _inputs(players=(*base_players, *filler_players), draft_rounds=15).model_copy(
        update={
            "team_count": 10,
            "draft_slot": 7,
            "roster": RosterConfiguration(qb=1, rb=2, wr=2, te=1, flex=2, bench=5, k=1, defense=1),
        }
    )
    completed = []
    for pick_no in range(1, next_pick):
        round_no = (pick_no - 1) // 10 + 1
        within_round = (pick_no - 1) % 10 + 1
        slot = within_round if round_no % 2 else 11 - within_round
        completed.append(
            CompletedDraftPick(
                pick_no=pick_no,
                draft_slot=slot,
                canonical_player_id=opponent_target_id
                if opponent_target_id is not None and pick_no == 100
                else None,
                position="RB" if slot == 7 else None,
            )
        )
    return inputs.model_copy(update={"completed_picks": tuple(completed)})


def _kyler_market(inputs: RecommendationInputs, *, classification: str) -> dict[str, object]:
    kyler_id = next(
        row.canonical_player_id
        for row in inputs.projected_players
        if row.player_name == "Kyler Murray"
    )
    return {
        "players": (
            {
                "canonical_player_id": kyler_id,
                "overall_adp": 146.6,
                "classification": classification,
                "market_derived_window": {
                    "earliest_pick": 136,
                    "latest_pick": 156,
                },
            },
        )
    }


def _inputs(
    *,
    completed: tuple[CompletedDraftPick, ...] = (),
    players: tuple[RecommendationPlayerInput, ...] | None = None,
    draft_rounds: int = 12,
) -> RecommendationInputs:
    selected = players or _players()
    rankings = tuple(
        ExpertRankingInput(
            canonical_player_id=row.canonical_player_id,
            overall_rank=index,
            tier="A" if index <= 4 else "B",
        )
        for index, row in enumerate(selected, start=1)
    )
    return RecommendationInputs(
        decision_at=datetime(2026, 8, 20, 19, tzinfo=UTC),
        team_count=2,
        draft_type="snake",
        draft_rounds=draft_rounds,
        draft_slot=1,
        roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1, k=1, defense=1),
        completed_picks=completed,
        projected_players=selected,
        expert_rankings=rankings,
        provenance=RecommendationProvenance(
            draft_snapshot_id="draft",
            player_snapshot_id="players",
            ranking_snapshot_id="ranking",
            projection_snapshot_id="projection",
            ranking_source="rotoworld",
            projection_source="cbs",
            projection_source_version="2026",
            ranking_resolver_version="2.0",
            scoring_calculator_version="1.1",
            scoring_settings_hash="hash",
        ),
        sport="nfl",
        league_type="redraft",
        keeper_status="non_keeper",
        scoring_format="full_ppr",
    )


def _players() -> tuple[RecommendationPlayerInput, ...]:
    values = (
        ("Amon-Ra St. Brown", "WR", 220.0),
        ("Chase Brown", "RB", 219.0),
        ("Trey McBride", "TE", 210.0),
        ("Colston Loveland", "TE", 190.0),
        ("Kyler Murray", "QB", 250.0),
        ("QB One", "QB", 240.0),
        ("QB Two", "QB", 230.0),
        ("RB One", "RB", 205.0),
        ("RB Two", "RB", 185.0),
        ("WR One", "WR", 200.0),
        ("WR Two", "WR", 180.0),
        ("TE One", "TE", 175.0),
        ("TE Two", "TE", 165.0),
        ("TE Three", "TE", 155.0),
    )
    return tuple(
        RecommendationPlayerInput(
            canonical_player_id=f"player-{index}",
            sleeper_player_id=f"s-{index}",
            player_name=name,
            position=position,  # type: ignore[arg-type]
            league_projected_points=projection,
            league_known_component_points=projection,
            cbs_projected_points=projection,
            scoring_completeness="complete",
        )
        for index, (name, position, projection) in enumerate(values, start=1)
    )
