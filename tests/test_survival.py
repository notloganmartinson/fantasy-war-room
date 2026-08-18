from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fantasy_war_room.decision.models import DraftTurnContext, RosterConfiguration
from fantasy_war_room.decision.survival import (
    SplitMix64,
    _candidate_survival_outcomes,
    _prepare_players,
    derive_pass_now_interval,
    fit_pick_mass,
    input_fingerprint,
    owner_slot_for_pick,
    scheduled_pick_for_slot,
    simulate_next_pick_survival,
    simulate_opponent_interval,
    survival_model_specification,
    validate_completed_picks,
)
from fantasy_war_room.decision.survival_models import (
    NextPickSurvivalInputs,
    OpponentRosterState,
    SurvivalAdpSnapshotInput,
    SurvivalCandidateInput,
    SurvivalCompletedPick,
    SurvivalDraftContext,
    SurvivalModelVersion,
    SurvivalPlayerInput,
)
from fantasy_war_room.errors import InputError

NOW = datetime(2026, 8, 17, 20, tzinfo=UTC)


def test_pass_now_interval_on_clock_off_clock_and_consecutive_turn() -> None:
    on_clock = _turn(next_pick=37, slot=4, on_clock=True, next_user=37, following=44)
    interval = derive_pass_now_interval(on_clock, team_count=10, round_count=15)
    assert interval.simulation_start_pick == 38
    assert interval.target_user_pick == 44
    assert interval.intervening_opponent_pick_count == 6
    assert list(range(interval.simulation_start_pick, interval.target_user_pick)) == list(
        range(38, 44)
    )

    off_clock = _turn(next_pick=39, slot=4, on_clock=False, next_user=44, following=57)
    interval = derive_pass_now_interval(off_clock, team_count=10, round_count=15)
    assert interval.simulation_start_pick == 39
    assert interval.target_user_pick == 44
    assert interval.intervening_opponent_pick_count == 5

    consecutive = _turn(next_pick=10, slot=10, on_clock=True, next_user=10, following=11)
    interval = derive_pass_now_interval(consecutive, team_count=10, round_count=15)
    assert interval.simulation_start_pick == interval.target_user_pick == 11
    assert interval.intervening_opponent_pick_count == 0


def test_forward_reverse_snake_arithmetic_and_interval_contains_only_opponents() -> None:
    assert [owner_slot_for_pick(number, 4) for number in range(1, 9)] == [
        1,
        2,
        3,
        4,
        4,
        3,
        2,
        1,
    ]
    inputs = _inputs()
    assert all(
        owner_slot_for_pick(number, inputs.draft.team_count) != inputs.draft.user_draft_slot
        for number in range(inputs.draft.simulation_start_pick, inputs.draft.target_user_pick)
    )


@pytest.mark.parametrize("team_count", [2, 4, 10, 12, 14])
def test_snake_owner_and_scheduled_pick_are_inverse_properties(team_count: int) -> None:
    for round_number in range(1, 16):
        round_picks = {
            scheduled_pick_for_slot(round_number, slot, team_count)
            for slot in range(1, team_count + 1)
        }
        assert round_picks == set(
            range((round_number - 1) * team_count + 1, round_number * team_count + 1)
        )
        for slot in range(1, team_count + 1):
            pick_no = scheduled_pick_for_slot(round_number, slot, team_count)
            assert owner_slot_for_pick(pick_no, team_count) == slot


def test_interval_never_simulates_current_user_pick_or_mutates_rosters() -> None:
    inputs = _inputs(simulations=1)
    before = inputs.opponent_rosters
    interval = simulate_opponent_interval(
        tuple(player for player in inputs.available_players if player.overall_adp is not None),
        opponent_rosters=inputs.opponent_rosters,
        roster_configuration=inputs.roster_configuration,
        team_count=10,
        round_count=15,
        simulation_start_pick=38,
        target_user_pick=44,
        model_specification=inputs.model_specification,
        stream=SplitMix64(7),
    )
    assert [selection.pick_no for selection in interval.selections] == list(range(38, 44))
    assert len({selection.canonical_player_id for selection in interval.selections}) == 6
    assert all(selection.draft_slot != 4 for selection in interval.selections)
    assert inputs.opponent_rosters == before
    assert len(interval.remaining_player_ids) == 54


def test_zero_horizon_survival_is_exactly_one() -> None:
    inputs = _inputs(current=10, start=11, target=11, slot=10, simulations=25)
    result = simulate_next_pick_survival(inputs)
    candidate = result.candidates[0]
    assert candidate.survived_simulation_count == 25
    assert candidate.simulated_availability_rate == 1.0
    assert candidate.monte_carlo_standard_error == 0.0


@pytest.mark.parametrize(
    ("picks", "code"),
    [
        (
            (SurvivalCompletedPick(pick_no=1, canonical_player_id="a"),) * 2,
            "duplicate_completed_pick",
        ),
        (
            (
                SurvivalCompletedPick(pick_no=1, canonical_player_id="a"),
                SurvivalCompletedPick(pick_no=3, canonical_player_id="b"),
            ),
            "noncontiguous_completed_picks",
        ),
        ((SurvivalCompletedPick(pick_no=1),), "unresolved_completed_draft_pick"),
    ],
)
def test_completed_pick_integrity(picks: tuple[SurvivalCompletedPick, ...], code: str) -> None:
    current = 2 if code == "unresolved_completed_draft_pick" else 3
    with pytest.raises(InputError) as raised:
        validate_completed_picks(picks, current_overall_pick=current, team_count=10)
    assert raised.value.code == code


def test_adp_only_ignores_dispersion_and_opponent_rosters() -> None:
    original = _inputs(model="adp-only-1.0")
    changed = original.model_copy(
        update={
            "available_players": tuple(
                player.model_copy(update={"adp_sd": 99.0}) for player in original.available_players
            ),
            "opponent_rosters": tuple(
                roster.model_copy(update={"qb": 5, "rb": 5, "wr": 5, "te": 5})
                for roster in original.opponent_rosters
            ),
        }
    )
    assert _candidate_data(simulate_next_pick_survival(original)) == _candidate_data(
        simulate_next_pick_survival(changed)
    )


def test_dispersion_ignores_rosters_and_roster_variant_uses_same_dispersion_contract() -> None:
    original = _inputs(model="adp-dispersion-1.0")
    changed = original.model_copy(
        update={
            "opponent_rosters": tuple(
                roster.model_copy(update={"qb": 8, "rb": 8, "wr": 8, "te": 8})
                for roster in original.opponent_rosters
            )
        }
    )
    assert _candidate_data(simulate_next_pick_survival(original)) == _candidate_data(
        simulate_next_pick_survival(changed)
    )
    roster_spec = survival_model_specification("adp-dispersion-roster-1.0")
    assert roster_spec.dispersion_model == original.model_specification.dispersion_model
    assert roster_spec.pick_weight_model == original.model_specification.pick_weight_model
    assert roster_spec.roster_adjustment_model != "none"


def test_discrete_dispersion_fit_support_normalization_and_invalid_failure() -> None:
    fit = fit_pick_mass(
        mean_pick=44.0,
        pick_sd=7.0,
        first_legal_pick=1,
        final_legal_pick=150,
    )
    assert fit.status == "fitted"
    assert fit.mass is not None
    assert len(fit.mass) == 150
    assert sum(fit.mass) == pytest.approx(1.0, abs=1e-12)
    assert all(value >= 0 for value in fit.mass)
    assert fit.mean_error is not None and fit.mean_error < 0.01

    invalid = fit_pick_mass(
        mean_pick=0,
        pick_sd=-1,
        first_legal_pick=1,
        final_legal_pick=150,
    )
    assert invalid.status == "unavailable"
    assert invalid.mass is None
    assert invalid.fallback_reason == "invalid_distribution_inputs"

    unsupported = fit_pick_mass(
        mean_pick=44,
        pick_sd=7,
        first_legal_pick=1,
        final_legal_pick=150,
        fit_model_version="not-real",
    )
    assert unsupported.status == "unavailable"
    assert unsupported.mass is None

    infeasible = fit_pick_mass(
        mean_pick=1,
        pick_sd=1,
        first_legal_pick=1,
        final_legal_pick=1,
    )
    assert infeasible.status == "unavailable"
    assert infeasible.mass is None
    assert infeasible.fallback_reason == "infeasible_bounded_variance"


def test_missing_dispersion_fallback_is_visible_and_deterministic() -> None:
    inputs = _inputs(model="adp-dispersion-1.0")
    players = list(inputs.available_players)
    players[0] = players[0].model_copy(update={"adp_sd": None})
    inputs = inputs.model_copy(update={"available_players": tuple(players)})
    first = simulate_next_pick_survival(inputs)
    second = simulate_next_pick_survival(inputs)
    assert first == second
    assert first.pool_coverage.dispersion_fallback_players == 1
    assert "distribution_fit_fallbacks_present" in first.pool_coverage.warning_codes


def test_candidate_statuses_for_already_drafted_missing_adp_and_invalid() -> None:
    inputs = _inputs(current=2, start=2, target=4, slot=4, simulations=10)
    players = (
        *inputs.available_players,
        SurvivalPlayerInput(canonical_player_id="missing-adp", position="WR"),
    )
    candidates = (
        SurvivalCandidateInput(canonical_player_id="drafted-1"),
        SurvivalCandidateInput(canonical_player_id="missing-adp"),
        SurvivalCandidateInput(canonical_player_id="absent"),
    )
    changed = inputs.model_copy(update={"available_players": players, "candidates": candidates})
    statuses = {
        row.canonical_player_id: row.status
        for row in simulate_next_pick_survival(changed).candidates
    }
    assert statuses == {
        "drafted-1": "already_drafted",
        "missing-adp": "missing_compatible_adp",
        "absent": "invalid_candidate",
    }


def test_insufficient_pool_and_coverage_warnings() -> None:
    inputs = _inputs(current=37, start=38, target=44, simulations=10)
    players = (
        SurvivalPlayerInput(canonical_player_id="candidate", position="WR", overall_adp=42),
        SurvivalPlayerInput(canonical_player_id="other", position="RB", overall_adp=43),
        SurvivalPlayerInput(canonical_player_id="missing", position="TE"),
    )
    changed = inputs.model_copy(
        update={
            "available_players": players,
            "candidates": (SurvivalCandidateInput(canonical_player_id="candidate"),),
        }
    )
    result = simulate_next_pick_survival(changed)
    assert result.candidates[0].status == "insufficient_modeled_pool"
    assert result.pool_coverage.hard_minimum_required == 7
    assert result.pool_coverage.hard_minimum_satisfied is False
    assert result.pool_coverage.coverage_rate == pytest.approx(2 / 3)
    assert result.pool_coverage.missing_by_position == {"TE": 1}
    assert "low_modeled_pool_coverage" in result.pool_coverage.warning_codes
    assert "shallow_modeled_pool" in result.pool_coverage.warning_codes


def test_seed_repeatability_candidate_order_and_fingerprint_invariance() -> None:
    inputs = _inputs(simulations=100, candidate_ids=("p-10", "p-20"))
    first = simulate_next_pick_survival(inputs)
    repeated = simulate_next_pick_survival(inputs)
    reversed_inputs = inputs.model_copy(update={"candidates": tuple(reversed(inputs.candidates))})
    reversed_result = simulate_next_pick_survival(reversed_inputs)
    assert first == repeated
    assert first.input_fingerprint == reversed_result.input_fingerprint
    assert _candidate_map(first) == _candidate_map(reversed_result)


def test_simulation_count_prefix_stability() -> None:
    short = _inputs(simulations=100)
    long = short.model_copy(update={"simulation_count": 250})
    prepared_short, _, _ = _prepare_players(short)
    prepared_long, _, _ = _prepare_players(long)
    short_outcomes = _candidate_survival_outcomes(
        short, prepared_short, input_fingerprint(short), "p-10"
    )
    long_outcomes = _candidate_survival_outcomes(
        long, prepared_long, input_fingerprint(long), "p-10"
    )
    assert long_outcomes[:100] == short_outcomes


def test_input_models_are_frozen_and_simulation_does_not_mutate_input() -> None:
    inputs = _inputs(simulations=10)
    before = inputs.model_dump(mode="json")
    simulate_next_pick_survival(inputs)
    assert inputs.model_dump(mode="json") == before
    with pytest.raises(ValidationError):
        inputs.seed = 8  # type: ignore[misc]


def test_pure_inputs_have_no_quality_lane_and_json_is_stable() -> None:
    assert "ranking" not in NextPickSurvivalInputs.model_fields
    assert "projection" not in NextPickSurvivalInputs.model_fields
    assert "recommendation" not in NextPickSurvivalInputs.model_fields
    inputs = _inputs(simulations=50)
    first = simulate_next_pick_survival(inputs).model_dump_json()
    second = simulate_next_pick_survival(inputs).model_dump_json()
    assert first == second


def test_result_counts_and_rates_are_bounded() -> None:
    result = simulate_next_pick_survival(_inputs(simulations=200))
    row = result.candidates[0]
    assert row.status == "modeled"
    assert row.survived_simulation_count is not None
    assert 0 <= row.survived_simulation_count <= 200
    assert row.simulated_availability_rate is not None
    assert 0 <= row.simulated_availability_rate <= 1


def _turn(
    *, next_pick: int, slot: int, on_clock: bool, next_user: int, following: int | None
) -> DraftTurnContext:
    return DraftTurnContext(
        next_overall_pick=next_pick,
        current_round=(next_pick - 1) // 10 + 1,
        snake_direction="forward" if ((next_pick - 1) // 10 + 1) % 2 else "reverse",
        draft_slot=slot,
        on_the_clock=on_clock,
        user_next_scheduled_pick=next_user,
        opponent_picks_before_next_user_pick=max(0, next_user - next_pick),
        user_following_scheduled_pick=following,
        opponent_picks_between_user_selections=(following - next_user - 1)
        if following is not None
        else None,
    )


def _inputs(
    *,
    model: SurvivalModelVersion = "adp-only-1.0",
    current: int = 37,
    start: int = 38,
    target: int = 44,
    slot: int = 4,
    simulations: int = 100,
    candidate_ids: tuple[str, ...] = ("p-10",),
) -> NextPickSurvivalInputs:
    completed = tuple(
        SurvivalCompletedPick(
            pick_no=pick_no,
            draft_slot=owner_slot_for_pick(pick_no, 10),
            canonical_player_id=f"drafted-{pick_no}",
            position=("QB", "RB", "WR", "TE")[pick_no % 4],
        )
        for pick_no in range(1, current)
    )
    players = tuple(
        SurvivalPlayerInput(
            canonical_player_id=f"p-{index:02d}",
            sleeper_player_id=f"s-{index:02d}",
            position=("QB", "RB", "WR", "TE")[index % 4],
            overall_adp=25.0 + index,
            adp_sd=5.0 + index % 5,
            sample_size=100 + index,
        )
        for index in range(60)
    )
    rosters = tuple(
        OpponentRosterState(draft_slot=draft_slot)
        for draft_slot in range(1, 11)
        if draft_slot != slot
    )
    return NextPickSurvivalInputs(
        decision_at=NOW,
        draft=SurvivalDraftContext(
            draft_snapshot_id="draft-snapshot",
            draft_id="draft-1",
            observed_at=NOW,
            payload_hash="draft-hash",
            team_count=10,
            round_count=15,
            user_draft_slot=slot,
            current_overall_pick=current,
            user_is_on_the_clock=start == current + 1,
            simulation_start_pick=start,
            target_user_pick=target,
            intervening_opponent_pick_count=target - start,
        ),
        adp=SurvivalAdpSnapshotInput(
            adp_snapshot_id="adp-snapshot",
            source="fantasy-football-calculator",
            source_version="source-hash",
            observed_at=NOW,
            imported_at=NOW,
            payload_hash="adp-hash",
            season="2026",
            league_size=10,
            scoring_format="ppr",
        ),
        available_players=players,
        candidates=tuple(
            SurvivalCandidateInput(canonical_player_id=value) for value in candidate_ids
        ),
        completed_picks=completed,
        opponent_rosters=rosters,
        roster_configuration=RosterConfiguration(
            qb=1, rb=2, wr=2, te=1, flex=2, bench=6, k=0, defense=0
        ),
        simulation_count=simulations,
        seed=42,
        model_specification=survival_model_specification(model),
    )


def _candidate_data(result: object) -> object:
    return _candidate_map(result)  # type: ignore[arg-type]


def _candidate_map(result: object) -> dict[str, object]:
    return {
        candidate.canonical_player_id: candidate.model_dump(mode="json")
        for candidate in result.candidates  # type: ignore[attr-defined]
    }
