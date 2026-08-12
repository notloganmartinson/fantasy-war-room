from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from fantasy_war_room.decision.models import (
    CompletedDraftPick,
    ExpertRankingInput,
    RecommendationInputs,
    RecommendationPlayerInput,
    RecommendationProvenance,
    RosterConfiguration,
)
from fantasy_war_room.decision.recommend import (
    allocate_roster,
    calculate_expert_percentiles,
    calculate_replacement_levels,
    calculate_scarcity,
    calculate_trusted_rank_values,
    calculate_turn_context,
    recommend,
)
from fantasy_war_room.errors import InputError


def test_snake_turn_arithmetic_at_forward_reverse_and_turn_boundary() -> None:
    forward = _inputs(completed=tuple(_pick(number) for number in range(1, 5)), slot=5)
    forward_context = calculate_turn_context(forward)
    assert forward_context.next_overall_pick == 5
    assert forward_context.current_round == 1
    assert forward_context.snake_direction == "forward"
    assert forward_context.on_the_clock is True
    assert forward_context.user_following_scheduled_pick == 16
    assert forward_context.opponent_picks_between_user_selections == 10

    reverse = _inputs(completed=tuple(_pick(number) for number in range(1, 14)), slot=5)
    reverse_context = calculate_turn_context(reverse)
    assert reverse_context.next_overall_pick == 14
    assert reverse_context.current_round == 2
    assert reverse_context.snake_direction == "reverse"
    assert reverse_context.on_the_clock is False
    assert reverse_context.user_next_scheduled_pick == 16
    assert reverse_context.opponent_picks_before_next_user_pick == 2

    turn = _inputs(completed=tuple(_pick(number) for number in range(1, 10)), slot=10)
    turn_context = calculate_turn_context(turn)
    assert turn_context.user_next_scheduled_pick == 10
    assert turn_context.user_following_scheduled_pick == 11
    assert turn_context.opponent_picks_between_user_selections == 0


def test_projection_aware_flex_allocation_and_starter_upgrade() -> None:
    players = (
        _player("rb-a", "RB", 100),
        _player("rb-b", "RB", 90),
        _player("wr-a", "WR", 80),
        _player("te-a", "TE", 70),
        _player("qb-a", "QB", 120),
    )
    inputs = _inputs(players=players, roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1))
    allocation = allocate_roster(inputs, players)
    slots = {assignment.canonical_player_id: assignment.slot for assignment in allocation.starters}
    assert slots == {
        "qb-a": "QB1",
        "rb-a": "RB1",
        "rb-b": "FLEX1",
        "wr-a": "WR1",
        "te-a": "TE1",
    }

    roster_picks = tuple(
        CompletedDraftPick(pick_no=index, draft_slot=1, canonical_player_id=player_id)
        for index, player_id in enumerate(("qb-a", "rb-a", "rb-b", "wr-a", "te-a"), 1)
    )
    universe = (*players, _player("rb-elite", "RB", 110), *_depth_players())
    result = recommend(
        _inputs(
            players=universe,
            completed=roster_picks,
            slot=1,
            team_count=2,
            roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1),
        )
    )
    elite = next(
        candidate for candidate in result.candidates if candidate.canonical_player_id == "rb-elite"
    )
    assert elite.roster_effect.lineup_projection_before == 460
    assert elite.roster_effect.lineup_projection_after == 480
    assert elite.roster_effect.starter_projection_delta == 20
    assert elite.roster_effect.candidate_assigned_slot == "RB1"
    assert elite.roster_effect.displaced_starter_id == "rb-b"
    assert elite.roster_effect.moved_to_bench_player_ids == ("rb-b",)
    assert any(
        reassignment.canonical_player_id == "rb-a"
        and reassignment.from_slot == "RB1"
        and reassignment.to_slot == "FLEX1"
        for reassignment in elite.roster_effect.reassignments
    )


def test_candidate_can_fill_vacancy_or_remain_bench_depth() -> None:
    universe = (*_depth_players(), _player("wr-low", "WR", 1))
    inputs = _inputs(
        players=universe,
        completed=(CompletedDraftPick(pick_no=1, draft_slot=1, canonical_player_id="qb-1"),),
        slot=1,
        team_count=2,
        roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=0),
    )
    result = recommend(inputs)
    rb = next(
        candidate for candidate in result.candidates if candidate.canonical_player_id == "rb-1"
    )
    assert rb.roster_effect.category == "fills_fixed_vacancy"
    assert rb.roster_effect.vacancies_before["RB"] == 1
    assert rb.roster_effect.vacancies_after["RB"] == 0

    full_roster = (
        CompletedDraftPick(pick_no=1, draft_slot=1, canonical_player_id="qb-1"),
        CompletedDraftPick(pick_no=2, draft_slot=1, canonical_player_id="rb-1"),
        CompletedDraftPick(pick_no=3, draft_slot=1, canonical_player_id="wr-1"),
        CompletedDraftPick(pick_no=4, draft_slot=1, canonical_player_id="te-1"),
    )
    full_result = recommend(inputs.model_copy(update={"completed_picks": full_roster}))
    low = next(
        candidate
        for candidate in full_result.candidates
        if candidate.canonical_player_id == "wr-low"
    )
    assert low.roster_effect.category == "bench_depth"
    assert low.roster_effect.starter_projection_delta == 0


def test_structural_replacement_is_static_when_players_are_drafted() -> None:
    players = _depth_players()
    roster = RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1)
    before = _inputs(players=players, team_count=2, roster=roster)
    drafted = before.model_copy(
        update={
            "completed_picks": (
                CompletedDraftPick(pick_no=1, draft_slot=1, canonical_player_id="rb-1"),
                CompletedDraftPick(pick_no=2, draft_slot=2, canonical_player_id="wr-1"),
                CompletedDraftPick(pick_no=3, draft_slot=2, canonical_player_id="qb-1"),
            )
        }
    )

    assert calculate_replacement_levels(before) == calculate_replacement_levels(drafted)


def test_flex_demand_uses_highest_marginal_projection() -> None:
    players = (
        *(_player(f"qb-{i}", "QB", 300 - i) for i in range(1, 4)),
        _player("rb-1", "RB", 200),
        _player("rb-2", "RB", 190),
        _player("rb-3", "RB", 180),
        _player("wr-1", "WR", 170),
        _player("wr-2", "WR", 160),
        _player("wr-3", "WR", 100),
        _player("te-1", "TE", 150),
        _player("te-2", "TE", 140),
        _player("te-3", "TE", 130),
    )
    levels = calculate_replacement_levels(
        _inputs(
            players=players,
            team_count=2,
            roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1),
        )
    )
    by_position = {level.position: level for level in levels}
    assert by_position["RB"].allocated_flex_demand == 1
    assert by_position["TE"].allocated_flex_demand == 1
    assert by_position["WR"].allocated_flex_demand == 0
    assert by_position["RB"].replacement_player_id == "rb-3"
    assert by_position["TE"].replacement_player_id == "te-3"


def test_vorp_and_one_round_available_scarcity() -> None:
    players = [
        _player("rb-a", "RB", 100),
        _player("rb-b", "RB", 80),
        _player("rb-c", "RB", 60),
        _player("rb-d", "RB", 40),
    ]
    scarcity = calculate_scarcity(players[0], players, team_count=2)
    assert scarcity.comparison_player_ids == ("rb-b", "rb-c")
    assert scarcity.comparison_mean_projection == 70
    assert scarcity.scarcity_points == 30

    result = recommend(_inputs(players=(*_depth_players(), *players), team_count=2))
    candidate = next(item for item in result.candidates if item.canonical_player_id == "rb-a")
    assert (
        candidate.vorp
        == candidate.projection_baseline - candidate.replacement.replacement_projection
    )


def test_expert_percentile_ties_and_missing_rank() -> None:
    rankings = (
        ExpertRankingInput(canonical_player_id="a", overall_rank=1),
        ExpertRankingInput(canonical_player_id="b", overall_rank=2),
        ExpertRankingInput(canonical_player_id="c", overall_rank=2),
        ExpertRankingInput(canonical_player_id="d", overall_rank=None),
    )
    percentiles = calculate_expert_percentiles(rankings)
    assert percentiles == {"a": 1.0, "b": 0.25, "c": 0.25}
    assert "d" not in percentiles


def test_equal_projection_allocation_and_component_ties_are_deterministic() -> None:
    tied = (
        _player("rb-a", "RB", 100),
        _player("rb-b", "RB", 100),
        _player("wr-a", "WR", 100),
        _player("wr-b", "WR", 100),
        _player("qb-a", "QB", 100),
        _player("te-a", "TE", 100),
    )
    inputs = _inputs(
        players=tied,
        team_count=1,
        roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1),
    )
    allocation = allocate_roster(inputs, tied)
    slots = {assignment.canonical_player_id: assignment.slot for assignment in allocation.starters}
    assert slots["rb-a"] == "RB1"
    assert slots["rb-b"] == "FLEX1"
    assert "wr-a" in slots
    assert "wr-b" not in slots

    result = recommend(inputs)
    tied_vorp = {
        candidate.vorp_component.normalized_value
        for candidate in result.candidates
        if candidate.position in {"RB", "WR"}
    }
    assert len(tied_vorp) == 1


def test_partial_projection_fallback_missing_projection_and_missing_rank() -> None:
    partial = _player("partial", "RB", None, known=88)
    missing = _player("missing", "WR", None, known=None, cbs=999)
    inputs = _inputs(players=(*_depth_players(), partial, missing), team_count=2)
    result = recommend(inputs)
    candidate = next(item for item in result.candidates if item.canonical_player_id == "partial")
    assert candidate.projection_baseline == 88
    assert candidate.projection_value_kind == "known_component"
    assert candidate.league_projected_points is None
    assert candidate.expert_percentile is None
    assert candidate.expert_component.contribution == 0
    assert result.excluded_candidate_counts["missing_projection_baseline"] == 1
    assert all(candidate.canonical_player_id != "missing" for candidate in result.candidates)


def test_roster_fit_normalization_and_exact_score_arithmetic() -> None:
    result = recommend(_inputs(players=_depth_players(), team_count=2))
    maximum = max(
        candidate.roster_effect.starter_projection_delta for candidate in result.candidates
    )
    for candidate in result.candidates:
        expected = candidate.roster_effect.starter_projection_delta / maximum
        assert candidate.roster_fit_component.normalized_value == pytest.approx(expected)
        assert candidate.recommendation_score == pytest.approx(
            candidate.vorp_component.contribution
            + candidate.expert_component.contribution
            + candidate.scarcity_component.contribution
            + candidate.roster_fit_component.contribution
            + candidate.next_pick_component.contribution
        )
        assert candidate.next_pick_component.weight == 0
        assert candidate.next_pick_availability.probability_available_at_next_pick is None


def test_zero_roster_delta_normalizes_to_zero() -> None:
    players = _depth_players()
    completed = tuple(
        CompletedDraftPick(pick_no=index, draft_slot=1, canonical_player_id=player_id)
        for index, player_id in enumerate(("qb-1", "rb-1", "wr-1", "te-1"), 1)
    )
    result = recommend(
        _inputs(
            players=players,
            completed=completed,
            slot=1,
            team_count=2,
            roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=0),
        )
    )
    bench_candidates = [
        candidate
        for candidate in result.candidates
        if candidate.roster_effect.starter_projection_delta == 0
    ]
    assert bench_candidates
    assert all(
        candidate.roster_fit_component.normalized_value == 0 for candidate in bench_candidates
    )


def test_baselines_select_documented_candidates() -> None:
    players = _depth_players()
    rankings = tuple(
        ExpertRankingInput(canonical_player_id=player.canonical_player_id, overall_rank=index)
        for index, player in enumerate(reversed(players), 1)
    )
    result = recommend(_inputs(players=players, rankings=rankings, team_count=2))
    assert (
        result.baselines.highest_expert_rank.canonical_player_id == players[-1].canonical_player_id
    )
    assert result.baselines.highest_league_projection.canonical_player_id == "qb-1"
    greedy = max(
        result.candidates, key=lambda candidate: (candidate.vorp, candidate.canonical_player_id)
    )
    assert result.baselines.greedy_vorp.raw_value == greedy.vorp


def test_shuffled_inputs_produce_identical_output() -> None:
    players = list(_depth_players())
    rankings = [
        ExpertRankingInput(canonical_player_id=player.canonical_player_id, overall_rank=index)
        for index, player in enumerate(players, 1)
    ]
    completed = [_pick(1, 2, "rb-1"), _pick(2, 1, "wr-1")]
    original = _inputs(players=tuple(players), rankings=tuple(rankings), completed=tuple(completed))
    random.Random(7).shuffle(players)
    random.Random(8).shuffle(rankings)
    random.Random(9).shuffle(completed)
    shuffled = original.model_copy(
        update={
            "projected_players": tuple(players),
            "expert_rankings": tuple(rankings),
            "completed_picks": tuple(completed),
        }
    )

    assert recommend(original).model_dump(mode="json") == recommend(shuffled).model_dump(
        mode="json"
    )


def test_model_selection_changes_only_weights_contributions_scores_and_order() -> None:
    inputs = _inputs(players=_depth_players(), team_count=2)
    default = recommend(inputs)
    explicit_baseline = recommend(inputs, "baseline-1.0")
    trusted = recommend(inputs, "trusted-board-1.0")

    assert default == explicit_baseline
    assert explicit_baseline.model_specification.weights == {
        "vorp": 50.0,
        "expert_rank": 20.0,
        "scarcity": 20.0,
        "roster_fit": 10.0,
        "next_pick_availability": 0.0,
    }
    assert trusted.model_specification.weights == {
        "vorp": 30.0,
        "expert_rank": 50.0,
        "scarcity": 15.0,
        "roster_fit": 5.0,
        "next_pick_availability": 0.0,
    }
    baseline_by_id = {
        candidate.canonical_player_id: candidate for candidate in explicit_baseline.candidates
    }
    trusted_by_id = {candidate.canonical_player_id: candidate for candidate in trusted.candidates}
    assert baseline_by_id.keys() == trusted_by_id.keys()
    for player_id in baseline_by_id:
        baseline_candidate = baseline_by_id[player_id]
        trusted_candidate = trusted_by_id[player_id]
        assert baseline_candidate.projection_baseline == trusted_candidate.projection_baseline
        assert baseline_candidate.replacement == trusted_candidate.replacement
        assert baseline_candidate.vorp == trusted_candidate.vorp
        assert baseline_candidate.expert_percentile == trusted_candidate.expert_percentile
        assert baseline_candidate.scarcity == trusted_candidate.scarcity
        assert baseline_candidate.roster_effect == trusted_candidate.roster_effect
        for component_name in (
            "vorp_component",
            "expert_component",
            "scarcity_component",
            "roster_fit_component",
            "next_pick_component",
        ):
            baseline_component = getattr(baseline_candidate, component_name)
            trusted_component = getattr(trusted_candidate, component_name)
            assert baseline_component.raw_value == trusted_component.raw_value
            assert baseline_component.normalized_value == trusted_component.normalized_value


def test_trusted_board_priority_and_large_value_override() -> None:
    targets = (
        _player("board-1", "WR", 220),
        _player("board-2", "WR", 218),
        _player("board-3", "WR", 216),
        _player("large-value", "WR", 350),
        _player("low-ranked-modest", "WR", 225),
    )
    support = (
        _player("support-qb", "QB", 200),
        _player("support-rb", "RB", 180),
        _player("support-te", "TE", 160),
    )
    rankings = tuple(
        ExpertRankingInput(canonical_player_id=player_id, overall_rank=rank)
        for player_id, rank in (
            ("board-1", 1),
            ("board-2", 2),
            ("board-3", 3),
            ("large-value", 4),
            ("low-ranked-modest", 20),
        )
    )
    result = recommend(
        _inputs(
            players=(*targets, *support),
            rankings=rankings,
            team_count=1,
            roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=0),
        ),
        "trusted-board-1.0",
    )
    order = [candidate.canonical_player_id for candidate in result.candidates]
    assert order.index("low-ranked-modest") > order.index("board-1")
    assert order.index("low-ranked-modest") > order.index("board-2")
    assert order.index("low-ranked-modest") > order.index("board-3")
    assert order.index("large-value") < order.index("board-3")


def test_trusted_board_1_1_rank_transform_tiers_and_policy_behavior() -> None:
    requested_ranks = (1, 3, 5, 10, 15, 20, 30, 50, 100, 150, 199)
    transformed = calculate_trusted_rank_values(
        tuple(
            ExpertRankingInput(canonical_player_id=f"rank-{rank}", overall_rank=rank)
            for rank in requested_ranks
        )
    )
    assert [transformed[f"rank-{rank}"] for rank in requested_ranks] == pytest.approx(
        [2 ** (-(rank - 1) / 20) for rank in requested_ranks],
        abs=1e-6,
    )

    players = (
        _player("higher-board", "WR", 220),
        _player("nearby-large-value", "WR", 350),
        _player("lower-board-modest", "WR", 225),
        _player("fallback", "WR", 210),
        _player("support-qb", "QB", 200),
        _player("support-rb", "RB", 180),
        _player("support-te", "TE", 160),
    )
    rankings = (
        ExpertRankingInput(canonical_player_id="higher-board", overall_rank=9, tier="A"),
        ExpertRankingInput(canonical_player_id="nearby-large-value", overall_rank=10, tier="A"),
        ExpertRankingInput(canonical_player_id="lower-board-modest", overall_rank=16, tier="B"),
        ExpertRankingInput(canonical_player_id="fallback", overall_rank=50),
    )
    result = recommend(
        _inputs(
            players=players,
            rankings=rankings,
            team_count=1,
            roster=RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=0),
        ),
        "trusted-board-1.1",
    )
    assert result.model_specification.recommendation_model_version == "trusted-board-1.1"
    assert result.model_specification.weights == {
        "vorp": 25.0,
        "trusted_rank": 40.0,
        "scarcity": 15.0,
        "roster_fit": 5.0,
        "next_pick_availability": 0.0,
        "trusted_tier": 15.0,
    }
    by_id = {candidate.canonical_player_id: candidate for candidate in result.candidates}
    assert by_id["higher-board"].trusted_rank_value == pytest.approx(2 ** (-8 / 20))
    assert by_id["higher-board"].trusted_rank_component.weight == 40.0
    assert by_id["higher-board"].trusted_tier_value == pytest.approx(8 / 9)
    assert by_id["lower-board-modest"].trusted_tier_value == pytest.approx(7 / 9)
    assert by_id["fallback"].trusted_tier is None
    assert by_id["fallback"].trusted_tier_component.contribution == 0
    assert "No trusted analyst tier is available." in by_id["fallback"].limitations
    order = [candidate.canonical_player_id for candidate in result.candidates]
    assert order.index("higher-board") < order.index("lower-board-modest")
    assert order.index("nearby-large-value") < order.index("higher-board")


@pytest.mark.parametrize(
    ("draft_type", "quarterbacks", "code"),
    [("linear", 1, "unsupported_draft_format"), ("snake", 2, "unsupported_roster_format")],
)
def test_unsupported_formats_are_rejected(draft_type: str, quarterbacks: int, code: str) -> None:
    inputs = _inputs(
        draft_type=draft_type,
        roster=RosterConfiguration(qb=quarterbacks, rb=1, wr=1, te=1, flex=1),
    )
    with pytest.raises(InputError) as error:
        recommend(inputs)
    assert error.value.code == code


def _inputs(
    *,
    players: tuple[RecommendationPlayerInput, ...] | None = None,
    rankings: tuple[ExpertRankingInput, ...] = (),
    completed: tuple[CompletedDraftPick, ...] = (),
    team_count: int = 10,
    slot: int = 1,
    draft_type: str = "snake",
    roster: RosterConfiguration | None = None,
) -> RecommendationInputs:
    return RecommendationInputs(
        decision_at=datetime(2026, 8, 20, 19, tzinfo=UTC),
        team_count=team_count,
        draft_type=draft_type,
        draft_rounds=15,
        draft_slot=slot,
        roster=roster or RosterConfiguration(qb=1, rb=1, wr=1, te=1, flex=1),
        completed_picks=completed,
        projected_players=players or _depth_players(),
        expert_rankings=rankings,
        provenance=RecommendationProvenance(
            draft_snapshot_id="draft-snapshot",
            player_snapshot_id="player-snapshot",
            ranking_snapshot_id="ranking-snapshot",
            projection_snapshot_id="projection-snapshot",
            ranking_source="rotoworld",
            ranking_source_version="2026",
            projection_source="cbs",
            projection_source_version="2026",
            ranking_resolver_version="2.0",
            scoring_calculator_version="1.1",
            scoring_settings_hash="settings-hash",
        ),
    )


def _player(
    player_id: str,
    position: str,
    exact: float | None,
    *,
    known: float | None = None,
    cbs: float | None = None,
) -> RecommendationPlayerInput:
    return RecommendationPlayerInput(
        canonical_player_id=player_id,
        sleeper_player_id=f"s-{player_id}",
        player_name=player_id.upper(),
        position=position,  # type: ignore[arg-type]
        league_projected_points=exact,
        league_known_component_points=known if known is not None else exact,
        cbs_projected_points=exact if cbs is None else cbs,
        scoring_completeness="complete" if exact is not None else "partial",
        unprojected_scoring_keys=() if exact is not None else ("bonus_rec_yd_100",),
    )


def _depth_players() -> tuple[RecommendationPlayerInput, ...]:
    values = {
        "QB": (300, 280, 260),
        "RB": (220, 200, 180, 160),
        "WR": (210, 190, 170, 150),
        "TE": (180, 160, 140, 120),
    }
    return tuple(
        _player(f"{position.lower()}-{index}", position, projection)
        for position, projections in values.items()
        for index, projection in enumerate(projections, 1)
    )


def _pick(
    pick_no: int, draft_slot: int = 2, canonical_player_id: str | None = None
) -> CompletedDraftPick:
    return CompletedDraftPick(
        pick_no=pick_no,
        draft_slot=draft_slot,
        canonical_player_id=canonical_player_id,
    )
