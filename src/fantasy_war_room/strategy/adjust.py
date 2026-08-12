from __future__ import annotations

from typing import Any, Literal

from fantasy_war_room.decision.models import RecommendationInputs, RecommendationResult
from fantasy_war_room.errors import InputError
from fantasy_war_room.identity import normalize_name
from fantasy_war_room.strategy.load import profile_hash
from fantasy_war_room.strategy.models import (
    ReservedPositionTargetState,
    RosterCompletionDirective,
    StrategyCandidate,
    StrategyProfile,
    StrategyProvenance,
    StrategyRecommendationResult,
    StrategyValueSummary,
    StrategyValueSummaryItem,
    TargetEvaluation,
    TargetProfile,
    TargetState,
)

ROSTER_COMPLETION_RULE_VERSION = "required-k-def-reservation-1.0"


def apply_strategy(
    raw: RecommendationResult,
    inputs: RecommendationInputs,
    profile: StrategyProfile,
    *,
    market_context: dict[str, Any] | None = None,
) -> StrategyRecommendationResult:
    _validate_compatibility(raw, inputs, profile)
    raw_candidates = list(raw.candidates)
    by_name = {normalize_name(row.player_name): row for row in raw_candidates}
    player_by_name = {normalize_name(row.player_name): row for row in inputs.projected_players}
    player_by_sleeper = {
        row.sleeper_player_id: row
        for row in inputs.projected_players
        if row.sleeper_player_id is not None
    }
    drafted_by_user = {
        pick.canonical_player_id
        for pick in inputs.completed_picks
        if pick.draft_slot == inputs.draft_slot and pick.canonical_player_id
    }
    drafted_by_others = {
        pick.canonical_player_id
        for pick in inputs.completed_picks
        if pick.draft_slot != inputs.draft_slot and pick.canonical_player_id
    }
    position_by_id = {row.canonical_player_id: row.position for row in inputs.projected_players}
    user_position_counts: dict[str, int] = {}
    for pick in inputs.completed_picks:
        if pick.draft_slot != inputs.draft_slot:
            continue
        position = pick.position or position_by_id.get(pick.canonical_player_id or "")
        if position:
            normalized = "DEF" if position.upper() in {"DEF", "DST"} else position.upper()
            user_position_counts[normalized] = user_position_counts.get(normalized, 0) + 1

    target_evaluations: list[TargetEvaluation] = []
    target_by_id: dict[str, tuple[TargetProfile, TargetEvaluation]] = {}
    primary_states: dict[str, str] = {}
    leader_score = raw_candidates[0].recommendation_score if raw_candidates else 0.0
    for target in profile.targets:
        player = (
            player_by_sleeper.get(target.sleeper_player_id)
            if target.sleeper_player_id is not None
            else player_by_name.get(normalize_name(target.player_name))
        )
        candidate = by_name.get(normalize_name(target.player_name))
        if player is not None:
            candidate = next(
                (
                    row
                    for row in raw_candidates
                    if row.canonical_player_id == player.canonical_player_id
                ),
                None,
            )
        canonical_id = player.canonical_player_id if player else None
        state, reason = _target_state(
            target,
            raw.turn_context.current_round,
            canonical_id,
            drafted_by_user,
            drafted_by_others,
            primary_states,
            market_context,
        )
        raw_rank = candidate.recommendation_rank if candidate else None
        deficit = round(leader_score - candidate.recommendation_score, 6) if candidate else None
        displacement = raw_rank - 1 if raw_rank is not None else None
        within_cost = bool(
            candidate
            and deficit is not None
            and displacement is not None
            and deficit <= target.max_raw_score_deficit
            and displacement <= target.max_raw_rank_displacement
        )
        evaluation = TargetEvaluation(
            player_name=target.player_name,
            canonical_player_id=canonical_id,
            state=state,
            window_mode=target.window_mode,
            raw_rank=raw_rank,
            raw_score_deficit=deficit,
            raw_rank_displacement=displacement,
            within_cost_ceiling=within_cost,
            reason=reason,
        )
        target_evaluations.append(evaluation)
        primary_states[target.player_name] = state
        if canonical_id:
            target_by_id[canonical_id] = (target, evaluation)

    user_pick_count = sum(pick.draft_slot == inputs.draft_slot for pick in inputs.completed_picks)
    remaining = max(0, inputs.draft_rounds - user_pick_count)
    unfilled = tuple(
        position
        for position in profile.roster_completion_guard.required_positions
        if user_position_counts.get(position, 0) == 0
    )
    completion_required = bool(unfilled) and remaining <= len(unfilled)
    evaluations_by_name = {evaluation.player_name: evaluation for evaluation in target_evaluations}
    reserved_states: list[ReservedPositionTargetState] = []
    active_reserved_by_position: dict[str, str] = {}
    for reserved in profile.reserved_position_targets:
        evaluation = evaluations_by_name[reserved.target_player_name]
        active = bool(
            evaluation.canonical_player_id
            and evaluation.state in {"too_early", "in_window", "deferred_pending_market_context"}
        )
        suppression = active and reserved.suppress_other_candidates_while_active
        if suppression:
            active_reserved_by_position[reserved.position] = evaluation.canonical_player_id or ""
        reserved_states.append(
            ReservedPositionTargetState(
                position=reserved.position,
                target_player_name=reserved.target_player_name,
                canonical_player_id=evaluation.canonical_player_id,
                target_state=evaluation.state,
                active=active,
                suppression_applied=suppression,
                reason=(
                    "Configured target remains reserved; other candidates at the position "
                    "are suppressed"
                    if suppression
                    else "Reserved target is no longer active"
                ),
            )
        )
    decorated: list[tuple[tuple[Any, ...], StrategyCandidate]] = []
    prohibited: list[StrategyCandidate] = []
    for raw_candidate in raw_candidates:
        target_pair = target_by_id.get(raw_candidate.canonical_player_id)
        eligible = True
        promotion: Literal["eligible_target_within_cost", "no_promotion"] = "no_promotion"
        reasons: list[str] = []
        if target_pair:
            target, evaluation = target_pair
            if target.window_mode == "hard_gate" and evaluation.state in {
                "too_early",
                "window_expired",
                "fallback_inactive",
                "deferred_pending_market_context",
            }:
                eligible = False
                reasons.append("target_hard_gate")
            elif evaluation.state == "in_window" and evaluation.within_cost_ceiling:
                promotion = "eligible_target_within_cost"
                reasons.append("target_promoted_within_raw_cost")
            elif evaluation.state == "in_window":
                reasons.append("target_raw_cost_exceeded")

        position_count = user_position_counts.get(raw_candidate.position, 0)
        utility: Literal[
            "normal_depth",
            "te2_starter_or_flex",
            "te2_bench_value",
            "redundant_qb_depth",
            "redundant_te_depth",
            "reserved_position_suppressed",
        ] = "normal_depth"
        reserved_id = active_reserved_by_position.get(raw_candidate.position)
        if reserved_id and raw_candidate.canonical_player_id != reserved_id:
            eligible = False
            utility = "reserved_position_suppressed"
            reasons.append("reserved_position_target_active")
        elif raw_candidate.position == "QB" and position_count >= 1:
            exception = profile.qb2_policy.late_round_exception_start_round
            if exception is None or raw.turn_context.current_round < exception:
                utility = "redundant_qb_depth"
                reasons.append("qb2_redundant_depth")
        if raw_candidate.position == "TE" and position_count >= 1:
            if profile.te3_prohibited and position_count >= 2:
                eligible = False
                reasons.append("te3_prohibited")
            else:
                effect = raw_candidate.roster_effect
                if effect.category != "bench_depth" and effect.candidate_assigned_slot is not None:
                    utility = profile.te2_policy.starter_or_flex_class
                    reasons.append("te2_improves_starting_lineup")
                elif (
                    leader_score - raw_candidate.recommendation_score
                    <= profile.te2_policy.max_bench_value_raw_score_deficit
                    and raw_candidate.recommendation_rank - 1
                    <= profile.te2_policy.max_bench_value_raw_rank_displacement
                ):
                    utility = profile.te2_policy.bench_value_class
                    reasons.append("te2_bench_value_within_raw_cost")
                else:
                    utility = profile.te2_policy.ordinary_depth_class
                    reasons.append("te2_redundant_depth")
        if completion_required:
            reasons.append("roster_completion_required")
        strategy_candidate = StrategyCandidate(
            strategy_rank=0,
            canonical_player_id=raw_candidate.canonical_player_id,
            raw_rank=raw_candidate.recommendation_rank,
            raw_score=raw_candidate.recommendation_score,
            eligible=eligible,
            target_promotion_class=promotion,
            positional_utility_class=utility,
            reason_codes=tuple(reasons),
            raw_candidate=raw_candidate,
        )
        key = (
            0 if eligible else 1,
            0 if promotion == "eligible_target_within_cost" else 1,
            _utility_order(utility),
            raw_candidate.recommendation_rank,
            raw_candidate.canonical_player_id,
        )
        if eligible:
            decorated.append((key, strategy_candidate))
        else:
            prohibited.append(strategy_candidate)
    decorated.sort(key=lambda item: item[0])
    ordered = tuple(
        candidate.model_copy(update={"strategy_rank": rank})
        for rank, (_, candidate) in enumerate(decorated, start=1)
    )
    prohibited_result = tuple(
        candidate.model_copy(update={"strategy_rank": 0})
        for candidate in sorted(prohibited, key=lambda row: (row.raw_rank, row.canonical_player_id))
    )
    actionable_choice = ordered[0] if ordered and not completion_required else None
    value_summary = _value_summary(
        raw_candidates,
        ordered,
        prohibited_result,
        actionable_choice,
    )
    return StrategyRecommendationResult(
        raw_recommendation=raw,
        actionable=not completion_required,
        actionable_choice=actionable_choice,
        directive=(
            RosterCompletionDirective(
                boundary_status=(
                    "exact_boundary" if remaining == len(unfilled) else "already_impossible"
                ),
                remaining_user_selections=remaining,
                unfilled_required_positions=unfilled,
                required_position_count=len(unfilled),
            )
            if completion_required
            else None
        ),
        candidates=() if completion_required else ordered,
        evaluated_candidates=ordered,
        prohibited_candidates=prohibited_result,
        targets=tuple(target_evaluations),
        reserved_position_targets=tuple(reserved_states),
        value_summary=value_summary,
        roster_completion_required=completion_required,
        remaining_user_selections=remaining,
        unfilled_required_positions=unfilled,
        strategy_provenance=StrategyProvenance(
            profile_name=profile.profile_name,
            profile_hash=profile_hash(profile),
            required_raw_model=profile.required_raw_model,
            required_ranking_source=profile.required_ranking_source,
        ),
    )


def _utility_order(value: str) -> int:
    return {
        "normal_depth": 0,
        "te2_starter_or_flex": 0,
        "te2_bench_value": 1,
        "redundant_qb_depth": 2,
        "redundant_te_depth": 2,
        "reserved_position_suppressed": 3,
    }[value]


def _summary_item(candidate: StrategyCandidate | None) -> StrategyValueSummaryItem | None:
    if candidate is None:
        return None
    raw = candidate.raw_candidate
    return StrategyValueSummaryItem(
        canonical_player_id=candidate.canonical_player_id,
        player_name=raw.player_name,
        position=raw.position,
        raw_rank=candidate.raw_rank,
        raw_score=candidate.raw_score,
        strategy_rank=candidate.strategy_rank or None,
        eligible=candidate.eligible,
        positional_utility_class=candidate.positional_utility_class,
        reason_codes=candidate.reason_codes,
    )


def _value_summary(
    raw_candidates: list[Any],
    ordered: tuple[StrategyCandidate, ...],
    prohibited: tuple[StrategyCandidate, ...],
    actionable_choice: StrategyCandidate | None,
) -> StrategyValueSummary:
    evaluated = sorted(
        (*ordered, *prohibited), key=lambda row: (row.raw_rank, row.canonical_player_id)
    )
    by_position = {
        position: next((row for row in evaluated if row.raw_candidate.position == position), None)
        for position in ("QB", "RB", "WR", "TE")
    }
    suppressed = next(
        (
            row
            for row in evaluated
            if not row.eligible or _utility_order(row.positional_utility_class) > 0
        ),
        None,
    )
    redundancy = next(
        (
            row
            for row in evaluated
            if row.positional_utility_class
            in {
                "te2_starter_or_flex",
                "te2_bench_value",
                "redundant_qb_depth",
                "redundant_te_depth",
            }
        ),
        None,
    )
    raw_leader = (
        next(
            (
                row
                for row in evaluated
                if row.canonical_player_id == raw_candidates[0].canonical_player_id
            ),
            None,
        )
        if raw_candidates
        else None
    )
    return StrategyValueSummary(
        best_raw_candidate=_summary_item(raw_leader),
        actionable_choice=_summary_item(actionable_choice),
        best_available_by_position={
            position: _summary_item(candidate) for position, candidate in by_position.items()
        },
        highest_raw_ranked_suppressed_candidate=_summary_item(suppressed),
        highest_raw_ranked_redundancy_affected_candidate=_summary_item(redundancy),
    )


def validate_strategy_compatibility(
    inputs: RecommendationInputs,
    profile: StrategyProfile,
    *,
    raw_model: str,
) -> None:
    problems: dict[str, Any] = {}
    if raw_model != profile.required_raw_model:
        problems["raw_model"] = {"expected": profile.required_raw_model, "actual": raw_model}
    if inputs.provenance.ranking_source != profile.required_ranking_source:
        problems["ranking_source"] = {
            "expected": profile.required_ranking_source,
            "actual": inputs.provenance.ranking_source,
        }
    checks = {
        "sport": (profile.sport, inputs.sport),
        "draft_type": (profile.draft_type, inputs.draft_type.casefold()),
        "league_type": (profile.league_type, inputs.league_type),
        "keeper_status": (profile.keeper_status, inputs.keeper_status),
        "scoring_format": (profile.scoring_format, inputs.scoring_format),
    }
    for name, (expected, actual) in checks.items():
        if actual != expected:
            problems[name] = {"expected": expected, "actual": actual}
    if inputs.team_count != profile.team_count:
        problems["team_count"] = {"expected": profile.team_count, "actual": inputs.team_count}
    if inputs.draft_slot not in profile.compatible_draft_slots:
        problems["draft_slot"] = {
            "expected": list(profile.compatible_draft_slots),
            "actual": inputs.draft_slot,
        }
    if inputs.roster.qb != profile.qb or inputs.roster.te != profile.te:
        problems["fixed_roster"] = {
            "expected": {"qb": profile.qb, "te": profile.te},
            "actual": {"qb": inputs.roster.qb, "te": inputs.roster.te},
        }
    if inputs.roster.flex != profile.flex:
        problems["flex"] = {"expected": profile.flex, "actual": inputs.roster.flex}
    if inputs.roster.k != profile.k or inputs.roster.defense != profile.defense:
        problems["required_completion_slots"] = {
            "expected": {"k": profile.k, "defense": profile.defense},
            "actual": {"k": inputs.roster.k, "defense": inputs.roster.defense},
        }
    if problems:
        raise InputError(
            "strategy_profile_incompatible",
            "Strategy profile is incompatible with the recommendation context",
            {
                "profile_name": profile.profile_name,
                "profile_schema_version": profile.schema_version,
                "failed_constraints": problems,
                "provenance": {
                    "draft_snapshot_id": inputs.provenance.draft_snapshot_id,
                    "scoring_context_league_id": inputs.provenance.scoring_context_league_id,
                    "scoring_settings_hash": inputs.provenance.scoring_settings_hash,
                },
            },
        )


def _validate_compatibility(
    raw: RecommendationResult, inputs: RecommendationInputs, profile: StrategyProfile
) -> None:
    validate_strategy_compatibility(
        inputs,
        profile,
        raw_model=raw.model_specification.recommendation_model_version,
    )


def _target_state(
    target: TargetProfile,
    round_no: int,
    canonical_id: str | None,
    drafted_by_user: set[str],
    drafted_by_others: set[str],
    primary_states: dict[str, str],
    market_context: dict[str, Any] | None,
) -> tuple[TargetState, str]:
    if canonical_id in drafted_by_user:
        return "acquired_by_user", "Target was selected by the user"
    if canonical_id in drafted_by_others:
        return "selected_by_opponent", "Target was selected by another team"
    if (
        target.role == "fallback"
        and target.fallback_for
        and primary_states.get(target.fallback_for)
        not in {"selected_by_opponent", "window_expired"}
    ):
        return "fallback_inactive", "Primary target has not been missed"
    if target.deferred_until_market_context and market_context is None:
        return "deferred_pending_market_context", "Compatible market context is required"
    if target.earliest_round is not None and round_no < target.earliest_round:
        return "too_early", "Current round is before the configured earliest round"
    if target.latest_round is not None and round_no > target.latest_round:
        return "window_expired", "Current round is after the configured latest round"
    return "in_window", "Current round is inside the effective target window"
