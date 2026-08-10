from __future__ import annotations

from typing import Any, Literal

from fantasy_war_room.decision.models import (
    BaselineSelection,
    CandidateExplanation,
    DraftTurnContext,
    ExpertRankingInput,
    LineupAssignment,
    ModelSpecification,
    NextPickAvailability,
    OffensivePosition,
    PlayerReassignment,
    PositionalScarcity,
    ProjectionValueKind,
    RecommendationBaselines,
    RecommendationInputs,
    RecommendationPlayerInput,
    RecommendationResult,
    ReplacementLevel,
    RosterAllocation,
    RosterEffect,
    ScoreComponent,
)
from fantasy_war_room.errors import InputError

POSITIONS: tuple[OffensivePosition, ...] = ("QB", "RB", "WR", "TE")
FLEX_POSITIONS: tuple[OffensivePosition, ...] = ("RB", "WR", "TE")
VORP_WEIGHT = 50.0
EXPERT_RANK_WEIGHT = 20.0
SCARCITY_WEIGHT = 20.0
ROSTER_FIT_WEIGHT = 10.0
NEXT_PICK_AVAILABILITY_WEIGHT = 0.0


def recommend(inputs: RecommendationInputs) -> RecommendationResult:
    """Produce baseline-1.0 solely from the supplied immutable inputs."""
    _validate_inputs(inputs)
    turn = calculate_turn_context(inputs)
    players = {player.canonical_player_id: player for player in inputs.projected_players}
    drafted_ids = {
        pick.canonical_player_id
        for pick in inputs.completed_picks
        if pick.canonical_player_id is not None
    }
    user_ids = {
        pick.canonical_player_id
        for pick in inputs.completed_picks
        if pick.draft_slot == inputs.draft_slot and pick.canonical_player_id is not None
    }
    modeled_user_roster = tuple(
        players[player_id] for player_id in sorted(user_ids & players.keys())
    )
    current_roster = allocate_roster(inputs, modeled_user_roster)
    missing_user_ids = tuple(
        sorted((*user_ids.difference(players), *inputs.unresolved_roster_player_ids))
    )
    if missing_user_ids:
        current_roster = current_roster.model_copy(
            update={
                "unmodeled_player_ids": tuple(
                    sorted((*current_roster.unmodeled_player_ids, *missing_user_ids))
                )
            }
        )
    replacements = calculate_replacement_levels(inputs)
    replacement_by_position = {level.position: level for level in replacements}
    rankings = {ranking.canonical_player_id: ranking for ranking in inputs.expert_rankings}
    expert_percentiles = calculate_expert_percentiles(inputs.expert_rankings)

    available = [
        player
        for player in inputs.projected_players
        if player.canonical_player_id not in drafted_ids and _projection(player) is not None
    ]
    available_by_position = {
        position: _sort_players(player for player in available if player.position == position)
        for position in POSITIONS
    }
    excluded = {
        "drafted": len(drafted_ids & players.keys()),
        "missing_projection_baseline": sum(
            player.canonical_player_id not in drafted_ids and _projection(player) is None
            for player in inputs.projected_players
        ),
        "missing_structural_replacement": 0,
    }
    raw_candidates: list[dict[str, Any]] = []
    for player in sorted(available, key=_player_identity_key):
        replacement = replacement_by_position[player.position]
        projection = _projection(player)
        if projection is None:
            continue
        if replacement.replacement_projection is None:
            excluded["missing_structural_replacement"] += 1
            continue
        after = allocate_roster(inputs, (*modeled_user_roster, player))
        roster_effect = _roster_effect(current_roster, after, player)
        scarcity = calculate_scarcity(
            player, available_by_position[player.position], inputs.team_count
        )
        ranking = rankings.get(player.canonical_player_id)
        raw_candidates.append(
            {
                "player": player,
                "projection": projection,
                "projection_kind": _projection_kind(player),
                "replacement": replacement,
                "vorp": _rounded(projection - replacement.replacement_projection),
                "scarcity": scarcity,
                "ranking": ranking,
                "expert_percentile": expert_percentiles.get(player.canonical_player_id),
                "roster_effect": roster_effect,
            }
        )

    vorp_percentiles = _empirical_percentiles(
        {row["player"].canonical_player_id: row["vorp"] for row in raw_candidates}
    )
    scarcity_percentiles = _empirical_percentiles(
        {
            row["player"].canonical_player_id: row["scarcity"].scarcity_points
            for row in raw_candidates
            if row["scarcity"].scarcity_points is not None
        }
    )
    maximum_delta = max(
        (row["roster_effect"].starter_projection_delta for row in raw_candidates), default=0.0
    )

    scored: list[dict[str, Any]] = []
    for row in raw_candidates:
        player = row["player"]
        player_id = player.canonical_player_id
        roster_value = (
            _rounded(row["roster_effect"].starter_projection_delta / maximum_delta)
            if maximum_delta > 0
            else 0.0
        )
        roster_effect = row["roster_effect"].model_copy(update={"normalized_value": roster_value})
        vorp_value = vorp_percentiles[player_id]
        expert_value = row["expert_percentile"] or 0.0
        scarcity_value = scarcity_percentiles.get(player_id, 0.0)
        components = {
            "vorp": _component(row["vorp"], vorp_value, VORP_WEIGHT),
            "expert": _component(row["expert_percentile"], expert_value, EXPERT_RANK_WEIGHT),
            "scarcity": _component(
                row["scarcity"].scarcity_points, scarcity_value, SCARCITY_WEIGHT
            ),
            "roster": _component(
                roster_effect.starter_projection_delta, roster_value, ROSTER_FIT_WEIGHT
            ),
            "next_pick": _component(None, 0.0, NEXT_PICK_AVAILABILITY_WEIGHT),
        }
        score = _rounded(sum(component.contribution for component in components.values()))
        scored.append(
            {
                **row,
                "roster_effect": roster_effect,
                "components": components,
                "score": score,
            }
        )

    scored.sort(key=_candidate_sort_key)
    candidates = tuple(
        _candidate_explanation(row, rank) for rank, row in enumerate(scored, start=1)
    )
    baselines = _baselines(scored)
    limitations = ["Next-pick survival probability is unavailable and contributes zero."]
    if any(candidate.projection_value_kind == "known_component" for candidate in candidates):
        limitations.append(
            "Some candidates use league known-component points rather than exact totals."
        )
    return RecommendationResult(
        decision_at=inputs.decision_at,
        turn_context=turn,
        current_roster=current_roster,
        replacement_levels=replacements,
        candidates=candidates,
        baselines=baselines,
        model_specification=ModelSpecification(
            weights={
                "vorp": VORP_WEIGHT,
                "expert_rank": EXPERT_RANK_WEIGHT,
                "scarcity": SCARCITY_WEIGHT,
                "roster_fit": ROSTER_FIT_WEIGHT,
                "next_pick_availability": NEXT_PICK_AVAILABILITY_WEIGHT,
            }
        ),
        provenance=inputs.provenance,
        excluded_candidate_counts=excluded,
        limitations=tuple(limitations),
    )


def calculate_turn_context(inputs: RecommendationInputs) -> DraftTurnContext:
    if inputs.draft_type.casefold() != "snake":
        raise InputError(
            "unsupported_draft_format",
            "baseline-1.0 supports snake drafts only",
            {"draft_type": inputs.draft_type},
        )
    if inputs.roster.qb != 1:
        raise InputError(
            "unsupported_roster_format",
            "baseline-1.0 supports exactly one starting quarterback",
            {"starting_qb_slots": inputs.roster.qb},
        )
    if inputs.draft_slot > inputs.team_count:
        raise InputError(
            "invalid_draft_slot",
            "Draft slot exceeds the league team count",
            {"draft_slot": inputs.draft_slot, "team_count": inputs.team_count},
        )
    next_pick = max((pick.pick_no for pick in inputs.completed_picks), default=0) + 1
    final_pick = inputs.team_count * inputs.draft_rounds
    if next_pick > final_pick:
        raise InputError("draft_complete", "The selected draft has no remaining picks")
    current_round = (next_pick - 1) // inputs.team_count + 1
    next_user_pick = _next_scheduled_pick(
        next_pick, inputs.draft_slot, inputs.team_count, inputs.draft_rounds
    )
    if next_user_pick is None:
        raise InputError("draft_complete", "The user has no remaining scheduled selection")
    following = _next_scheduled_pick(
        next_user_pick + 1, inputs.draft_slot, inputs.team_count, inputs.draft_rounds
    )
    return DraftTurnContext(
        next_overall_pick=next_pick,
        current_round=current_round,
        snake_direction="forward" if current_round % 2 else "reverse",
        draft_slot=inputs.draft_slot,
        on_the_clock=next_pick == next_user_pick,
        user_next_scheduled_pick=next_user_pick,
        opponent_picks_before_next_user_pick=next_user_pick - next_pick,
        user_following_scheduled_pick=following,
        opponent_picks_between_user_selections=(following - next_user_pick - 1)
        if following is not None
        else None,
    )


def allocate_roster(
    inputs: RecommendationInputs, roster_players: tuple[RecommendationPlayerInput, ...]
) -> RosterAllocation:
    unique = {player.canonical_player_id: player for player in roster_players}
    modeled = [player for player in unique.values() if _projection(player) is not None]
    unmodeled = sorted(
        player.canonical_player_id for player in unique.values() if _projection(player) is None
    )
    assignments: list[LineupAssignment] = []
    remaining: list[RecommendationPlayerInput] = []
    fixed_counts = {
        "QB": inputs.roster.qb,
        "RB": inputs.roster.rb,
        "WR": inputs.roster.wr,
        "TE": inputs.roster.te,
    }
    for position in POSITIONS:
        position_players = _sort_players(
            player for player in modeled if player.position == position
        )
        fixed = position_players[: fixed_counts[position]]
        remaining.extend(position_players[fixed_counts[position] :])
        for index, player in enumerate(fixed, start=1):
            assignments.append(_assignment(f"{position}{index}", player))
    flex_players = _sort_players(
        player for player in remaining if player.position in FLEX_POSITIONS
    )[: inputs.roster.flex]
    for index, player in enumerate(flex_players, start=1):
        assignments.append(_assignment(f"FLEX{index}", player))
    starter_ids = {assignment.canonical_player_id for assignment in assignments}
    bench = sorted(
        player.canonical_player_id
        for player in modeled
        if player.canonical_player_id not in starter_ids
    )
    vacancies: dict[str, int] = {
        position: fixed_counts[position]
        - sum(
            assignment.position == position and assignment.slot.startswith(position)
            for assignment in assignments
        )
        for position in POSITIONS
    }
    vacancies["FLEX"] = inputs.roster.flex - sum(
        assignment.slot.startswith("FLEX") for assignment in assignments
    )
    assignments.sort(key=lambda assignment: _slot_key(assignment.slot))
    return RosterAllocation(
        starters=tuple(assignments),
        bench_player_ids=tuple(bench),
        unmodeled_player_ids=tuple(unmodeled),
        vacancies=vacancies,
        starting_lineup_projection=_rounded(
            sum(assignment.projection for assignment in assignments)
        ),
    )


def calculate_replacement_levels(inputs: RecommendationInputs) -> tuple[ReplacementLevel, ...]:
    universe = {
        position: _sort_players(
            player
            for player in inputs.projected_players
            if player.position == position and _projection(player) is not None
        )
        for position in POSITIONS
    }
    fixed_counts = {
        "QB": inputs.roster.qb,
        "RB": inputs.roster.rb,
        "WR": inputs.roster.wr,
        "TE": inputs.roster.te,
    }
    fixed_demand = {position: inputs.team_count * fixed_counts[position] for position in POSITIONS}
    flex_allocations: dict[OffensivePosition, int] = {position: 0 for position in POSITIONS}
    for _ in range(inputs.team_count * inputs.roster.flex):
        marginal: list[RecommendationPlayerInput] = []
        for position in FLEX_POSITIONS:
            index = fixed_demand[position] + flex_allocations[position]
            if index < len(universe[position]):
                marginal.append(universe[position][index])
        if not marginal:
            break
        selected = _sort_players(marginal)[0]
        flex_allocations[selected.position] += 1
    levels: list[ReplacementLevel] = []
    for position in POSITIONS:
        demand = fixed_demand[position] + flex_allocations[position]
        replacement = (
            universe[position][demand - 1] if demand and demand <= len(universe[position]) else None
        )
        levels.append(
            ReplacementLevel(
                position=position,
                universe_size=len(universe[position]),
                fixed_demand=fixed_demand[position],
                allocated_flex_demand=flex_allocations[position],
                total_demand=demand,
                replacement_player_id=replacement.canonical_player_id if replacement else None,
                replacement_projection=_projection(replacement) if replacement else None,
                replacement_projection_value_kind=_projection_kind(replacement)
                if replacement
                else None,
            )
        )
    return tuple(levels)


def calculate_scarcity(
    candidate: RecommendationPlayerInput,
    available_at_position: list[RecommendationPlayerInput],
    team_count: int,
) -> PositionalScarcity:
    index = next(
        index
        for index, player in enumerate(available_at_position)
        if player.canonical_player_id == candidate.canonical_player_id
    )
    comparison = available_at_position[index + 1 : index + 1 + team_count]
    if not comparison:
        return PositionalScarcity(comparison_player_ids=(), comparison_count=0)
    projections = [_projection(player) for player in comparison]
    numeric = [value for value in projections if value is not None]
    mean = _rounded(sum(numeric) / len(numeric))
    candidate_projection = _projection(candidate)
    assert candidate_projection is not None
    return PositionalScarcity(
        comparison_player_ids=tuple(player.canonical_player_id for player in comparison),
        comparison_count=len(comparison),
        comparison_mean_projection=mean,
        scarcity_points=_rounded(candidate_projection - mean),
    )


def calculate_expert_percentiles(
    rankings: tuple[ExpertRankingInput, ...],
) -> dict[str, float]:
    ranked = sorted(
        (ranking for ranking in rankings if ranking.overall_rank is not None),
        key=lambda ranking: (ranking.overall_rank, ranking.canonical_player_id),
    )
    if not ranked:
        return {}
    if len(ranked) == 1:
        return {ranked[0].canonical_player_id: 1.0}
    result: dict[str, float] = {}
    index = 0
    while index < len(ranked):
        end = index
        while end + 1 < len(ranked) and ranked[end + 1].overall_rank == ranked[index].overall_rank:
            end += 1
        midpoint = (index + end) / 2
        percentile = _rounded(1 - midpoint / (len(ranked) - 1))
        for ranking in ranked[index : end + 1]:
            result[ranking.canonical_player_id] = percentile
        index = end + 1
    return result


def _validate_inputs(inputs: RecommendationInputs) -> None:
    player_ids = [player.canonical_player_id for player in inputs.projected_players]
    ranking_ids = [ranking.canonical_player_id for ranking in inputs.expert_rankings]
    if len(player_ids) != len(set(player_ids)):
        raise InputError("duplicate_projection_player", "Projected player IDs must be unique")
    if len(ranking_ids) != len(set(ranking_ids)):
        raise InputError("duplicate_ranking_player", "Expert ranking player IDs must be unique")
    calculate_turn_context(inputs)


def _next_scheduled_pick(
    at_or_after: int, draft_slot: int, team_count: int, draft_rounds: int
) -> int | None:
    starting_round = max(1, (at_or_after - 1) // team_count + 1)
    for round_number in range(starting_round, draft_rounds + 1):
        within_round = draft_slot if round_number % 2 else team_count - draft_slot + 1
        pick = (round_number - 1) * team_count + within_round
        if pick >= at_or_after:
            return pick
    return None


def _projection(player: RecommendationPlayerInput | None) -> float | None:
    if player is None:
        return None
    if player.league_projected_points is not None:
        return player.league_projected_points
    return player.league_known_component_points


def _projection_kind(player: RecommendationPlayerInput) -> ProjectionValueKind:
    return "exact" if player.league_projected_points is not None else "known_component"


def _sort_players(
    players: Any,
) -> list[RecommendationPlayerInput]:
    return sorted(
        players,
        key=lambda player: (
            -float(_projection(player) or 0.0),
            POSITIONS.index(player.position),
            player.canonical_player_id,
        ),
    )


def _player_identity_key(player: RecommendationPlayerInput) -> tuple[str, str]:
    return player.player_name.casefold(), player.canonical_player_id


def _assignment(slot: str, player: RecommendationPlayerInput) -> LineupAssignment:
    projection = _projection(player)
    assert projection is not None
    return LineupAssignment(
        slot=slot,
        canonical_player_id=player.canonical_player_id,
        player_name=player.player_name,
        position=player.position,
        projection=projection,
        projection_value_kind=_projection_kind(player),
    )


def _slot_key(slot: str) -> tuple[int, int]:
    prefix = "".join(character for character in slot if not character.isdigit())
    suffix = int("".join(character for character in slot if character.isdigit()) or 0)
    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FLEX": 4}
    return order[prefix], suffix


def _roster_effect(
    before: RosterAllocation,
    after: RosterAllocation,
    candidate: RecommendationPlayerInput,
) -> RosterEffect:
    before_slots = {
        assignment.canonical_player_id: assignment.slot for assignment in before.starters
    }
    after_slots = {assignment.canonical_player_id: assignment.slot for assignment in after.starters}
    candidate_slot = after_slots.get(candidate.canonical_player_id)
    displaced = sorted(set(before_slots) - set(after_slots))
    reassignments = tuple(
        PlayerReassignment(
            canonical_player_id=player_id,
            from_slot=before_slots[player_id],
            to_slot=after_slots[player_id],
        )
        for player_id in sorted(set(before_slots) & set(after_slots))
        if before_slots[player_id] != after_slots[player_id]
    )
    moved_to_bench = tuple(sorted(set(before_slots) & set(after.bench_player_ids)))
    promoted = tuple(sorted(set(before.bench_player_ids) & set(after_slots)))
    if candidate_slot is None:
        category: Literal[
            "fills_fixed_vacancy",
            "fills_flex_vacancy",
            "upgrades_fixed_starter",
            "upgrades_flex_or_rebalances_lineup",
            "bench_depth",
        ] = "bench_depth"
    elif candidate_slot.startswith("FLEX") and before.vacancies["FLEX"] > 0:
        category = "fills_flex_vacancy"
    elif not candidate_slot.startswith("FLEX") and before.vacancies[candidate.position] > 0:
        category = "fills_fixed_vacancy"
    elif candidate_slot.startswith("FLEX") or reassignments:
        category = "upgrades_flex_or_rebalances_lineup"
    else:
        category = "upgrades_fixed_starter"
    return RosterEffect(
        category=category,
        lineup_projection_before=before.starting_lineup_projection,
        lineup_projection_after=after.starting_lineup_projection,
        starter_projection_delta=_rounded(
            max(0.0, after.starting_lineup_projection - before.starting_lineup_projection)
        ),
        normalized_value=0.0,
        candidate_assigned_slot=candidate_slot,
        displaced_starter_id=displaced[0] if displaced else None,
        reassignments=reassignments,
        moved_to_bench_player_ids=moved_to_bench,
        promoted_from_bench_player_ids=promoted,
        vacancies_before=dict(before.vacancies),
        vacancies_after=dict(after.vacancies),
    )


def _empirical_percentiles(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.values())
    count = len(ordered)
    if count == 0:
        return {}
    return {
        key: _rounded(
            (
                sum(other < value for other in ordered)
                + 0.5 * sum(other == value for other in ordered)
            )
            / count
        )
        for key, value in values.items()
    }


def _component(raw: float | None, normalized: float, weight: float) -> ScoreComponent:
    return ScoreComponent(
        raw_value=raw,
        normalized_value=_rounded(normalized),
        weight=weight,
        contribution=_rounded(normalized * weight),
    )


def _candidate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    ranking = row["ranking"]
    expert_rank = (
        ranking.overall_rank if ranking and ranking.overall_rank is not None else float("inf")
    )
    player = row["player"]
    return (
        -row["score"],
        -row["vorp"],
        expert_rank,
        -row["projection"],
        player.player_name.casefold(),
        player.canonical_player_id,
    )


def _candidate_explanation(row: dict[str, Any], rank: int) -> CandidateExplanation:
    player = row["player"]
    ranking = row["ranking"]
    limitations: list[str] = []
    if row["projection_kind"] == "known_component":
        limitations.append("Projection is a known-component total, not an exact league total.")
    if ranking is None or ranking.overall_rank is None:
        limitations.append("No matched expert overall rank is available.")
    if row["scarcity"].scarcity_points is None:
        limitations.append("No following available player exists for positional scarcity.")
    components = row["components"]
    return CandidateExplanation(
        recommendation_rank=rank,
        canonical_player_id=player.canonical_player_id,
        sleeper_player_id=player.sleeper_player_id,
        player_name=player.player_name,
        position=player.position,
        team=player.team,
        recommendation_score=row["score"],
        projection_baseline=row["projection"],
        projection_value_kind=row["projection_kind"],
        league_projected_points=player.league_projected_points,
        league_known_component_points=player.league_known_component_points,
        cbs_projected_points=player.cbs_projected_points,
        scoring_completeness=player.scoring_completeness,
        unprojected_scoring_keys=player.unprojected_scoring_keys,
        replacement=row["replacement"],
        vorp=row["vorp"],
        expert_overall_rank=ranking.overall_rank if ranking else None,
        expert_positional_rank=ranking.positional_rank if ranking else None,
        expert_percentile=row["expert_percentile"],
        scarcity=row["scarcity"],
        roster_effect=row["roster_effect"],
        vorp_component=components["vorp"],
        expert_component=components["expert"],
        scarcity_component=components["scarcity"],
        roster_fit_component=components["roster"],
        next_pick_component=components["next_pick"],
        next_pick_availability=NextPickAvailability(),
        limitations=tuple(limitations),
    )


def _baselines(scored: list[dict[str, Any]]) -> RecommendationBaselines:
    ranked = [row for row in scored if row["ranking"] and row["ranking"].overall_rank is not None]
    expert = min(
        ranked,
        key=lambda row: (row["ranking"].overall_rank, row["player"].canonical_player_id),
        default=None,
    )
    projection = min(
        scored,
        key=lambda row: (-row["projection"], row["player"].canonical_player_id),
        default=None,
    )
    vorp = min(
        scored,
        key=lambda row: (-row["vorp"], row["player"].canonical_player_id),
        default=None,
    )
    return RecommendationBaselines(
        highest_expert_rank=_baseline(expert, "expert"),
        highest_league_projection=_baseline(projection, "projection"),
        greedy_vorp=_baseline(vorp, "vorp"),
    )


def _baseline(row: dict[str, Any] | None, kind: str) -> BaselineSelection:
    if row is None:
        return BaselineSelection(canonical_player_id=None, player_name=None, raw_value=None)
    raw = (
        row["ranking"].overall_rank
        if kind == "expert"
        else row["projection"]
        if kind == "projection"
        else row["vorp"]
    )
    return BaselineSelection(
        canonical_player_id=row["player"].canonical_player_id,
        player_name=row["player"].player_name,
        raw_value=raw,
    )


def _rounded(value: float) -> float:
    return round(float(value), 6)
