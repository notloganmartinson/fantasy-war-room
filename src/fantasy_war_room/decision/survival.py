from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from fantasy_war_room.decision.models import DraftTurnContext, RosterConfiguration
from fantasy_war_room.decision.survival_models import (
    CandidateSurvivalResult,
    ModeledPoolCoverage,
    NextPickSurvivalInputs,
    NextPickSurvivalResult,
    OpponentIntervalResult,
    OpponentRosterState,
    PassNowInterval,
    PickMassFit,
    SimulatedSelection,
    SurvivalCompletedPick,
    SurvivalModelSpecification,
    SurvivalModelVersion,
    SurvivalPlayerInput,
)
from fantasy_war_room.errors import InputError

MASK_64 = (1 << 64) - 1
SUPPORTED_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
FLEX_POSITIONS = frozenset({"RB", "WR", "TE"})
DISPERSION_FIT_VERSION = "discrete-laplace-dispersion-1.0"


@dataclass(frozen=True)
class _PreparedPlayer:
    player: SurvivalPlayerInput
    pick_hazards: tuple[float, ...] | None
    fit_status: str


class SplitMix64:
    """Small versioned generator whose bit stream is controlled by FWR."""

    def __init__(self, seed: int) -> None:
        self._state = seed & MASK_64

    def next_uint64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & MASK_64
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK_64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK_64
        return (value ^ (value >> 31)) & MASK_64

    def unit_interval(self) -> float:
        return self.next_uint64() / float(1 << 64)


def survival_model_specification(model_version: SurvivalModelVersion) -> SurvivalModelSpecification:
    common: dict[str, int | float | str | bool] = {
        "adp_distance_scale_league_rounds": 1.0,
        "pool_coverage_warning_threshold": 0.8,
        "shallow_pool_horizon_multiple": 3,
    }
    if model_version == "adp-only-1.0":
        return SurvivalModelSpecification(
            model_version=model_version,
            pick_weight_model="logistic-adp-distance-1.0",
            dispersion_model="none",
            roster_adjustment_model="none",
            parameters=common,
        )
    roster_model = (
        "bounded-vacancy-multiplier-1.0" if model_version == "adp-dispersion-roster-1.0" else "none"
    )
    return SurvivalModelSpecification(
        model_version=model_version,
        pick_weight_model="conditional-discrete-pick-hazard-1.0",
        dispersion_model=DISPERSION_FIT_VERSION,
        roster_adjustment_model=roster_model,
        parameters={
            **common,
            "missing_dispersion_fallback_picks": "one_league_round",
            "invalid_fit_fallback": "adp-only-weight",
            "fixed_vacancy_multiplier": 1.10,
            "flex_vacancy_multiplier": 1.05,
        },
    )


def owner_slot_for_pick(pick_no: int, team_count: int) -> int:
    if pick_no <= 0 or team_count <= 0:
        raise InputError(
            "invalid_snake_pick",
            "Snake pick number and team count must be greater than zero",
            {"pick_no": pick_no, "team_count": team_count},
        )
    round_number = (pick_no - 1) // team_count + 1
    within_round = (pick_no - 1) % team_count + 1
    return within_round if round_number % 2 else team_count - within_round + 1


def scheduled_pick_for_slot(round_number: int, draft_slot: int, team_count: int) -> int:
    if round_number <= 0 or not 1 <= draft_slot <= team_count:
        raise InputError(
            "invalid_snake_slot",
            "Round and draft slot must be valid for the configured league",
            {
                "round_number": round_number,
                "draft_slot": draft_slot,
                "team_count": team_count,
            },
        )
    within_round = draft_slot if round_number % 2 else team_count - draft_slot + 1
    return (round_number - 1) * team_count + within_round


def next_scheduled_pick_for_slot(
    at_or_after: int, draft_slot: int, team_count: int, round_count: int
) -> int | None:
    starting_round = max(1, (at_or_after - 1) // team_count + 1)
    for round_number in range(starting_round, round_count + 1):
        pick_no = scheduled_pick_for_slot(round_number, draft_slot, team_count)
        if pick_no >= at_or_after:
            return pick_no
    return None


def derive_pass_now_interval(
    turn: DraftTurnContext,
    *,
    team_count: int,
    round_count: int,
) -> PassNowInterval:
    final_pick = team_count * round_count
    current = turn.next_overall_pick
    if current > final_pick:
        raise InputError("draft_complete", "The selected draft has no remaining picks")
    if turn.on_the_clock:
        target = turn.user_following_scheduled_pick
        if target is None:
            raise InputError(
                "no_following_user_pick",
                "Pass-now survival requires a following scheduled user selection",
            )
        start = current + 1
    else:
        target = turn.user_next_scheduled_pick
        start = current
    if target > final_pick or start > target:
        raise InputError(
            "invalid_pass_now_interval",
            "Pass-now interval is outside the configured draft",
            {"simulation_start_pick": start, "target_user_pick": target},
        )
    for pick_no in range(start, target):
        if owner_slot_for_pick(pick_no, team_count) == turn.draft_slot:
            raise InputError(
                "invalid_pass_now_interval",
                "Pass-now interval contains a user-owned selection",
                {"pick_no": pick_no, "draft_slot": turn.draft_slot},
            )
    return PassNowInterval(
        current_overall_pick=current,
        user_is_on_the_clock=turn.on_the_clock,
        simulation_start_pick=start,
        target_user_pick=target,
        intervening_opponent_pick_count=target - start,
    )


def validate_completed_picks(
    picks: tuple[SurvivalCompletedPick, ...],
    *,
    current_overall_pick: int,
    team_count: int,
) -> None:
    numbers = [pick.pick_no for pick in picks]
    if len(numbers) != len(set(numbers)):
        raise InputError(
            "duplicate_completed_pick", "Completed picks contain duplicate pick numbers"
        )
    expected = list(range(1, current_overall_pick))
    if sorted(numbers) != expected:
        raise InputError(
            "noncontiguous_completed_picks",
            "Completed picks must form the exact prefix before the current overall pick",
            {"expected_pick_count": len(expected), "actual_pick_numbers": sorted(numbers)},
        )
    for pick in picks:
        if pick.canonical_player_id is None:
            raise InputError(
                "unresolved_completed_draft_pick",
                "Every completed pick must have an exact canonical player identity",
                {"pick_no": pick.pick_no},
            )
        expected_slot = owner_slot_for_pick(pick.pick_no, team_count)
        if pick.draft_slot is not None and pick.draft_slot != expected_slot:
            raise InputError(
                "completed_pick_slot_mismatch",
                "Completed pick slot conflicts with snake draft arithmetic",
                {
                    "pick_no": pick.pick_no,
                    "draft_slot": pick.draft_slot,
                    "expected_draft_slot": expected_slot,
                },
            )


def adp_only_pick_weight(overall_adp: float, simulated_pick: int, league_size: int) -> float:
    if (
        not math.isfinite(overall_adp)
        or overall_adp <= 0
        or simulated_pick <= 0
        or league_size <= 0
    ):
        raise InputError(
            "invalid_adp_weight_input", "ADP weight inputs must be finite and positive"
        )
    distance = (overall_adp - simulated_pick) / league_size
    if distance >= 40:
        return math.exp(-distance)
    if distance <= -40:
        return 1.0
    return 1.0 / (1.0 + math.exp(distance))


def fit_pick_mass(
    *,
    mean_pick: float,
    pick_sd: float,
    first_legal_pick: int,
    final_legal_pick: int,
    fit_model_version: str = DISPERSION_FIT_VERSION,
) -> PickMassFit:
    """Fit a bounded discrete Laplace mass; invalid inputs never produce mass."""
    if fit_model_version != DISPERSION_FIT_VERSION:
        return PickMassFit(
            status="unavailable",
            mass=None,
            fit_model_version=fit_model_version,
            fallback_reason="unsupported_fit_model",
        )
    if (
        not math.isfinite(mean_pick)
        or not math.isfinite(pick_sd)
        or first_legal_pick <= 0
        or final_legal_pick < first_legal_pick
        or not first_legal_pick <= mean_pick <= final_legal_pick
        or pick_sd < 0
    ):
        return PickMassFit(
            status="unavailable",
            mass=None,
            fit_model_version=fit_model_version,
            fallback_reason="invalid_distribution_inputs",
        )
    support_size = final_legal_pick - first_legal_pick + 1
    maximum_variance = (mean_pick - first_legal_pick) * (final_legal_pick - mean_pick)
    if (support_size < 2 and pick_sd > 0) or pick_sd**2 > maximum_variance + 1e-12:
        return PickMassFit(
            status="unavailable",
            mass=None,
            fit_model_version=fit_model_version,
            fallback_reason="infeasible_bounded_variance",
        )
    scale = max(pick_sd / math.sqrt(2.0), 0.25)
    support = range(first_legal_pick, final_legal_pick + 1)
    log_weights = tuple(-abs(pick - mean_pick) / scale for pick in support)
    maximum = max(log_weights)
    weights = tuple(math.exp(value - maximum) for value in log_weights)
    total = sum(weights)
    if not math.isfinite(total) or total <= 0:
        return PickMassFit(
            status="unavailable",
            mass=None,
            fit_model_version=fit_model_version,
            fallback_reason="distribution_normalization_failed",
        )
    mass = tuple(value / total for value in weights)
    if any(not math.isfinite(value) or value < 0 for value in mass) or abs(sum(mass) - 1) > 1e-12:
        return PickMassFit(
            status="unavailable",
            mass=None,
            fit_model_version=fit_model_version,
            fallback_reason="distribution_validation_failed",
        )
    fitted_mean = sum(pick * probability for pick, probability in zip(support, mass, strict=True))
    fitted_variance = sum(
        ((pick - fitted_mean) ** 2) * probability
        for pick, probability in zip(support, mass, strict=True)
    )
    return PickMassFit(
        status="fitted",
        mass=mass,
        fit_model_version=fit_model_version,
        mean_error=abs(fitted_mean - mean_pick),
        variance_error=abs(fitted_variance - pick_sd**2),
        iterations=0,
    )


def roster_need_multiplier(
    *,
    position: str,
    opponent_roster: OpponentRosterState,
    roster_configuration: RosterConfiguration,
    model_version: str = "bounded-vacancy-multiplier-1.0",
) -> float:
    if model_version != "bounded-vacancy-multiplier-1.0":
        raise InputError("unsupported_roster_adjustment_model", "Unknown roster adjustment model")
    normalized = position.upper()
    current = _position_count(opponent_roster, normalized)
    fixed = _required_count(roster_configuration, normalized)
    if current < fixed:
        return 1.10
    if normalized in FLEX_POSITIONS:
        flex_used = max(
            0,
            opponent_roster.rb
            + opponent_roster.wr
            + opponent_roster.te
            - roster_configuration.rb
            - roster_configuration.wr
            - roster_configuration.te,
        )
        if flex_used < roster_configuration.flex:
            return 1.05
    return 1.0


def input_fingerprint(inputs: NextPickSurvivalInputs) -> str:
    """Hash normalized state while excluding request order, count, candidates, and seed."""
    payload = inputs.model_dump(mode="json", exclude={"candidates", "simulation_count", "seed"})
    if inputs.model_specification.dispersion_model == "none":
        for player in payload["available_players"]:
            player.pop("adp_sd", None)
            player.pop("sample_size", None)
    if inputs.model_specification.roster_adjustment_model == "none":
        payload.pop("opponent_rosters", None)
        payload.pop("roster_configuration", None)
    payload["available_players"] = sorted(
        payload["available_players"], key=lambda row: row["canonical_player_id"]
    )
    payload["completed_picks"] = sorted(payload["completed_picks"], key=lambda row: row["pick_no"])
    if "opponent_rosters" in payload:
        payload["opponent_rosters"] = sorted(
            payload["opponent_rosters"], key=lambda row: row["draft_slot"]
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def simulate_next_pick_survival(inputs: NextPickSurvivalInputs) -> NextPickSurvivalResult:
    _validate_inputs(inputs)
    fingerprint = input_fingerprint(inputs)
    prepared, failed_fit_count, fallback_count = _prepare_players(inputs)
    coverage = _pool_coverage(inputs, prepared, failed_fit_count, fallback_count)
    available_by_id = {item.player.canonical_player_id: item for item in prepared}
    drafted = {cast(str, pick.canonical_player_id) for pick in inputs.completed_picks}
    all_available_ids = {player.canonical_player_id for player in inputs.available_players}
    results: list[CandidateSurvivalResult] = []
    for candidate in inputs.candidates:
        player_id = candidate.canonical_player_id
        if player_id in drafted:
            results.append(_unmodeled_candidate(inputs, player_id, "already_drafted"))
            continue
        if player_id not in all_available_ids:
            results.append(_unmodeled_candidate(inputs, player_id, "invalid_candidate"))
            continue
        if player_id not in available_by_id:
            results.append(_unmodeled_candidate(inputs, player_id, "missing_compatible_adp"))
            continue
        if not coverage.hard_minimum_satisfied:
            results.append(_unmodeled_candidate(inputs, player_id, "insufficient_modeled_pool"))
            continue
        outcomes = _candidate_survival_outcomes(inputs, prepared, fingerprint, player_id)
        survived = sum(outcomes)
        rate = survived / inputs.simulation_count
        results.append(
            CandidateSurvivalResult(
                canonical_player_id=player_id,
                status="modeled",
                target_user_pick=inputs.draft.target_user_pick,
                survived_simulation_count=survived,
                simulation_count=inputs.simulation_count,
                simulated_availability_rate=rate,
                monte_carlo_standard_error=math.sqrt(rate * (1 - rate) / inputs.simulation_count),
            )
        )
    limitations = [
        "Simulated availability rates are model outputs, not ground-truth probabilities.",
    ]
    if inputs.draft.user_is_on_the_clock:
        limitations.append(
            "The current user selection is conditioned only as not this candidate; the "
            "unspecified alternative player is not removed from the modeled pool."
        )
    return NextPickSurvivalResult(
        model_version=inputs.model_specification.model_version,
        decision_at=inputs.decision_at,
        seed=inputs.seed,
        simulation_count=inputs.simulation_count,
        current_overall_pick=inputs.draft.current_overall_pick,
        simulation_start_pick=inputs.draft.simulation_start_pick,
        target_user_pick=inputs.draft.target_user_pick,
        intervening_opponent_pick_count=inputs.draft.intervening_opponent_pick_count,
        user_is_on_the_clock=inputs.draft.user_is_on_the_clock,
        draft_snapshot_id=inputs.draft.draft_snapshot_id,
        adp_snapshot_id=inputs.adp.adp_snapshot_id,
        input_fingerprint=fingerprint,
        model_specification=inputs.model_specification,
        pool_coverage=coverage,
        candidates=tuple(results),
        limitations=tuple(limitations),
    )


def _candidate_survival_outcomes(
    inputs: NextPickSurvivalInputs,
    prepared: tuple[_PreparedPlayer, ...],
    fingerprint: str,
    player_id: str,
) -> tuple[bool, ...]:
    if inputs.model_specification.roster_adjustment_model == "none":
        return _static_candidate_survival_outcomes(inputs, prepared, fingerprint, player_id)
    outcomes: list[bool] = []
    for simulation_index in range(inputs.simulation_count):
        stream = _candidate_stream(
            fingerprint,
            inputs.model_specification.model_version,
            player_id,
            inputs.seed,
            simulation_index,
        )
        interval = _simulate_prepared_interval(
            prepared,
            opponent_rosters=inputs.opponent_rosters,
            roster_configuration=inputs.roster_configuration,
            team_count=inputs.draft.team_count,
            simulation_start_pick=inputs.draft.simulation_start_pick,
            target_user_pick=inputs.draft.target_user_pick,
            model_specification=inputs.model_specification,
            stream=stream,
        )
        outcomes.append(player_id in interval.remaining_player_ids)
    return tuple(outcomes)


def _static_candidate_survival_outcomes(
    inputs: NextPickSurvivalInputs,
    prepared: tuple[_PreparedPlayer, ...],
    fingerprint: str,
    player_id: str,
) -> tuple[bool, ...]:
    pick_numbers = tuple(range(inputs.draft.simulation_start_pick, inputs.draft.target_user_pick))
    weight_rows = tuple(
        tuple(
            _pick_weight(
                item,
                pick_no,
                inputs.draft.team_count,
                OpponentRosterState(draft_slot=1),
                inputs.roster_configuration,
                inputs.model_specification,
            )
            for item in prepared
        )
        for pick_no in pick_numbers
    )
    row_totals = tuple(sum(row) for row in weight_rows)
    candidate_index = next(
        index for index, item in enumerate(prepared) if item.player.canonical_player_id == player_id
    )
    outcomes: list[bool] = []
    for simulation_index in range(inputs.simulation_count):
        stream = _candidate_stream(
            fingerprint,
            inputs.model_specification.model_version,
            player_id,
            inputs.seed,
            simulation_index,
        )
        selected_indices: list[int] = []
        candidate_survived = True
        for row, base_total in zip(weight_rows, row_totals, strict=True):
            total = base_total - sum(row[index] for index in selected_indices)
            threshold = stream.unit_interval() * total
            cumulative = 0.0
            selected: int | None = None
            last_available: int | None = None
            for index, weight in enumerate(row):
                if index in selected_indices:
                    continue
                last_available = index
                cumulative += weight
                if threshold < cumulative:
                    selected = index
                    break
            if selected is None:
                if last_available is None:
                    raise InputError(
                        "insufficient_modeled_pool",
                        "No available player remains for the simulated opponent pick",
                    )
                selected = last_available
            selected_indices.append(selected)
            if selected == candidate_index:
                candidate_survived = False
        outcomes.append(candidate_survived)
    return tuple(outcomes)


def simulate_opponent_interval(
    players: tuple[SurvivalPlayerInput, ...],
    *,
    opponent_rosters: tuple[OpponentRosterState, ...],
    roster_configuration: RosterConfiguration,
    team_count: int,
    round_count: int,
    simulation_start_pick: int,
    target_user_pick: int,
    model_specification: SurvivalModelSpecification,
    stream: SplitMix64,
) -> OpponentIntervalResult:
    """Simulate one pure opponent interval from normalized available players."""
    prepared: list[_PreparedPlayer] = []
    for player in sorted(players, key=lambda row: row.canonical_player_id):
        if player.overall_adp is None:
            continue
        hazards = None
        if model_specification.dispersion_model != "none":
            fit = fit_pick_mass(
                mean_pick=player.overall_adp,
                pick_sd=player.adp_sd if player.adp_sd is not None else float(team_count),
                first_legal_pick=1,
                final_legal_pick=team_count * round_count,
                fit_model_version=model_specification.dispersion_model,
            )
            hazards = _conditional_hazards(fit.mass) if fit.mass is not None else None
        prepared.append(_PreparedPlayer(player, hazards, "direct_interval"))
    horizon = target_user_pick - simulation_start_pick
    if horizon < 0 or len(prepared) < horizon:
        raise InputError(
            "insufficient_modeled_pool",
            "Modeled pool cannot supply every opponent selection in the interval",
        )
    return _simulate_prepared_interval(
        tuple(prepared),
        opponent_rosters=opponent_rosters,
        roster_configuration=roster_configuration,
        team_count=team_count,
        simulation_start_pick=simulation_start_pick,
        target_user_pick=target_user_pick,
        model_specification=model_specification,
        stream=stream,
    )


def _simulate_prepared_interval(
    players: tuple[_PreparedPlayer, ...],
    *,
    opponent_rosters: tuple[OpponentRosterState, ...],
    roster_configuration: RosterConfiguration,
    team_count: int,
    simulation_start_pick: int,
    target_user_pick: int,
    model_specification: SurvivalModelSpecification,
    stream: SplitMix64,
) -> OpponentIntervalResult:
    remaining = list(players)
    rosters = {roster.draft_slot: roster for roster in opponent_rosters}
    selections: list[SimulatedSelection] = []
    for pick_no in range(simulation_start_pick, target_user_pick):
        slot = owner_slot_for_pick(pick_no, team_count)
        roster = rosters.get(slot, OpponentRosterState(draft_slot=slot))
        weights = [
            _pick_weight(
                item, pick_no, team_count, roster, roster_configuration, model_specification
            )
            for item in remaining
        ]
        index = _weighted_index(weights, stream)
        selected = remaining.pop(index).player
        selections.append(
            SimulatedSelection(
                pick_no=pick_no,
                draft_slot=slot,
                canonical_player_id=selected.canonical_player_id,
                position=selected.position,
            )
        )
        if model_specification.roster_adjustment_model != "none":
            rosters[slot] = _add_to_roster(roster, selected.position)
    return OpponentIntervalResult(
        selections=tuple(selections),
        remaining_player_ids=tuple(sorted(item.player.canonical_player_id for item in remaining)),
        opponent_rosters=tuple(rosters[slot] for slot in sorted(rosters)),
    )


def _validate_inputs(inputs: NextPickSurvivalInputs) -> None:
    draft = inputs.draft
    if inputs.model_specification != survival_model_specification(
        inputs.model_specification.model_version
    ):
        raise InputError(
            "invalid_survival_model_specification",
            "Survival model specification does not match its versioned contract",
        )
    if draft.user_draft_slot > draft.team_count:
        raise InputError("invalid_draft_slot", "User draft slot exceeds team count")
    if inputs.adp.league_size != draft.team_count:
        raise InputError("incompatible_adp_snapshot", "ADP league size does not match draft")
    current_owner = owner_slot_for_pick(draft.current_overall_pick, draft.team_count)
    expected_on_clock = current_owner == draft.user_draft_slot
    if draft.user_is_on_the_clock != expected_on_clock:
        raise InputError(
            "invalid_pass_now_interval",
            "On-the-clock state conflicts with snake draft arithmetic",
        )
    expected_start = (
        draft.current_overall_pick + 1 if draft.user_is_on_the_clock else draft.current_overall_pick
    )
    expected_target = next_scheduled_pick_for_slot(
        expected_start,
        draft.user_draft_slot,
        draft.team_count,
        draft.round_count,
    )
    if draft.simulation_start_pick != expected_start or draft.target_user_pick != expected_target:
        raise InputError(
            "invalid_pass_now_interval",
            "Supplied pass-now interval does not match the next user selection",
            {
                "expected_simulation_start_pick": expected_start,
                "expected_target_user_pick": expected_target,
            },
        )
    if (
        draft.intervening_opponent_pick_count
        != draft.target_user_pick - draft.simulation_start_pick
    ):
        raise InputError("invalid_pass_now_interval", "Intervening pick count is inconsistent")
    for pick_no in range(draft.simulation_start_pick, draft.target_user_pick):
        if owner_slot_for_pick(pick_no, draft.team_count) == draft.user_draft_slot:
            raise InputError(
                "invalid_pass_now_interval", "Simulation interval contains a user-owned pick"
            )
    validate_completed_picks(
        inputs.completed_picks,
        current_overall_pick=draft.current_overall_pick,
        team_count=draft.team_count,
    )
    player_ids = [player.canonical_player_id for player in inputs.available_players]
    candidate_ids = [candidate.canonical_player_id for candidate in inputs.candidates]
    if len(player_ids) != len(set(player_ids)):
        raise InputError("duplicate_available_player", "Available player IDs must be unique")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise InputError("duplicate_survival_candidate", "Candidate IDs must be unique")
    roster_slots = [roster.draft_slot for roster in inputs.opponent_rosters]
    if len(roster_slots) != len(set(roster_slots)):
        raise InputError("duplicate_opponent_roster", "Opponent roster slots must be unique")
    if draft.user_draft_slot in roster_slots:
        raise InputError(
            "user_roster_not_allowed", "Opponent rosters must not contain the user slot"
        )
    if any(slot > draft.team_count for slot in roster_slots):
        raise InputError("invalid_opponent_roster_slot", "Opponent roster slot exceeds team count")
    drafted_ids = {cast(str, pick.canonical_player_id) for pick in inputs.completed_picks}
    overlap = sorted(drafted_ids.intersection(player_ids))
    if overlap:
        raise InputError(
            "drafted_player_marked_available",
            "Available-player universe contains completed draft selections",
            {"canonical_player_ids": overlap},
        )


def _prepare_players(
    inputs: NextPickSurvivalInputs,
) -> tuple[tuple[_PreparedPlayer, ...], int, int]:
    prepared: list[_PreparedPlayer] = []
    failed = 0
    fallback = 0
    final_pick = inputs.draft.team_count * inputs.draft.round_count
    uses_dispersion = inputs.model_specification.dispersion_model != "none"
    for player in sorted(inputs.available_players, key=lambda row: row.canonical_player_id):
        if player.overall_adp is None:
            continue
        if not uses_dispersion:
            prepared.append(_PreparedPlayer(player, None, "not_requested"))
            continue
        pick_sd = player.adp_sd
        fit_status = "fitted"
        if pick_sd is None:
            pick_sd = float(inputs.draft.team_count)
            fit_status = "missing_dispersion_fallback"
            fallback += 1
        fit = fit_pick_mass(
            mean_pick=player.overall_adp,
            pick_sd=pick_sd,
            first_legal_pick=1,
            final_legal_pick=final_pick,
            fit_model_version=inputs.model_specification.dispersion_model,
        )
        if fit.mass is None:
            failed += 1
            fallback += 1
            prepared.append(_PreparedPlayer(player, None, "invalid_fit_adp_fallback"))
        else:
            prepared.append(_PreparedPlayer(player, _conditional_hazards(fit.mass), fit_status))
    return tuple(prepared), failed, fallback


def _pool_coverage(
    inputs: NextPickSurvivalInputs,
    prepared: tuple[_PreparedPlayer, ...],
    failed_fit_count: int,
    fallback_count: int,
) -> ModeledPoolCoverage:
    total = len(inputs.available_players)
    compatible = sum(player.overall_adp is not None for player in inputs.available_players)
    modeled = len(prepared)
    missing_positions = Counter(
        player.position.upper() for player in inputs.available_players if player.overall_adp is None
    )
    modeled_positions = Counter(item.player.position.upper() for item in prepared)
    horizon = inputs.draft.intervening_opponent_pick_count
    coverage_rate = modeled / total if total else 0.0
    warnings: list[str] = []
    if coverage_rate < 0.8:
        warnings.append("low_modeled_pool_coverage")
    if modeled < 3 * horizon:
        warnings.append("shallow_modeled_pool")
    final_pick = inputs.draft.team_count * inputs.draft.round_count
    if inputs.draft.target_user_pick > (2 * final_pick) / 3 and (
        coverage_rate < 0.9 or modeled < max(3 * horizon, 2 * inputs.draft.team_count)
    ):
        warnings.append("late_round_pool_depletion")
    required_unmodeled = (inputs.roster_configuration.k > 0 and modeled_positions["K"] == 0) or (
        inputs.roster_configuration.defense > 0 and modeled_positions["DEF"] == 0
    )
    if required_unmodeled:
        warnings.append("required_k_def_unmodeled")
    if fallback_count:
        warnings.append("distribution_fit_fallbacks_present")
    if inputs.draft.user_is_on_the_clock:
        warnings.append("unspecified_current_selection_not_removed")
    hard_minimum = horizon + 1
    return ModeledPoolCoverage(
        total_available_relevant_players=total,
        compatible_adp_available_players=compatible,
        modeled_available_players=modeled,
        missing_compatible_adp_players=total - compatible,
        failed_distribution_fit_players=failed_fit_count,
        dispersion_fallback_players=fallback_count,
        coverage_rate=coverage_rate,
        modeled_pool_to_intervening_pick_ratio=(modeled / horizon if horizon else None),
        modeled_by_position=dict(sorted(modeled_positions.items())),
        missing_by_position=dict(sorted(missing_positions.items())),
        hard_minimum_required=hard_minimum,
        hard_minimum_satisfied=modeled >= hard_minimum,
        warning_codes=tuple(sorted(warnings)),
    )


def _pick_weight(
    item: _PreparedPlayer,
    pick_no: int,
    team_count: int,
    roster: OpponentRosterState,
    roster_configuration: RosterConfiguration,
    specification: SurvivalModelSpecification,
) -> float:
    adp = cast(float, item.player.overall_adp)
    if specification.dispersion_model == "none" or item.pick_hazards is None:
        weight = adp_only_pick_weight(adp, pick_no, team_count)
    else:
        index = pick_no - 1
        weight = 0.0 if index >= len(item.pick_hazards) else item.pick_hazards[index]
    if specification.roster_adjustment_model != "none":
        weight *= roster_need_multiplier(
            position=item.player.position,
            opponent_roster=roster,
            roster_configuration=roster_configuration,
            model_version=specification.roster_adjustment_model,
        )
    return weight


def _conditional_hazards(mass: tuple[float, ...]) -> tuple[float, ...]:
    hazards = [0.0] * len(mass)
    tail = 0.0
    for index in range(len(mass) - 1, -1, -1):
        tail += mass[index]
        hazards[index] = mass[index] / tail if tail > 0 else 0.0
    return tuple(hazards)


def _weighted_index(weights: Sequence[float], stream: SplitMix64) -> int:
    total = sum(weights)
    if not weights or not math.isfinite(total) or total <= 0:
        raise InputError("invalid_pick_weights", "Simulated pick weights must have positive mass")
    threshold = stream.unit_interval() * total
    cumulative = 0.0
    for index, weight in enumerate(weights):
        if not math.isfinite(weight) or weight < 0:
            raise InputError("invalid_pick_weights", "Simulated pick weights must be finite")
        cumulative += weight
        if threshold < cumulative:
            return index
    return len(weights) - 1


def _candidate_stream(
    fingerprint: str,
    model_version: str,
    candidate_id: str,
    seed: int,
    simulation_index: int,
) -> SplitMix64:
    encoded = "\x1f".join(
        (model_version, fingerprint, candidate_id, str(seed), str(simulation_index))
    ).encode()
    derived = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")
    return SplitMix64(derived)


def _unmodeled_candidate(
    inputs: NextPickSurvivalInputs, player_id: str, status: str
) -> CandidateSurvivalResult:
    return CandidateSurvivalResult(
        canonical_player_id=player_id,
        status=cast(Any, status),
        target_user_pick=inputs.draft.target_user_pick,
        simulation_count=inputs.simulation_count,
    )


def _required_count(configuration: RosterConfiguration, position: str) -> int:
    return {
        "QB": configuration.qb,
        "RB": configuration.rb,
        "WR": configuration.wr,
        "TE": configuration.te,
        "K": configuration.k,
        "DEF": configuration.defense,
    }.get(position, 0)


def _position_count(roster: OpponentRosterState, position: str) -> int:
    return {
        "QB": roster.qb,
        "RB": roster.rb,
        "WR": roster.wr,
        "TE": roster.te,
        "K": roster.k,
        "DEF": roster.defense,
    }.get(position, 0)


def _add_to_roster(roster: OpponentRosterState, position: str) -> OpponentRosterState:
    field = {"QB": "qb", "RB": "rb", "WR": "wr", "TE": "te", "K": "k", "DEF": "defense"}.get(
        position.upper()
    )
    if field is None:
        return roster
    return roster.model_copy(update={field: getattr(roster, field) + 1})
