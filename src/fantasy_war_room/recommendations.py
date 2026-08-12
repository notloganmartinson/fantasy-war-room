from __future__ import annotations

from datetime import datetime

from fantasy_war_room.decision.models import RecommendationModelVersion, RecommendationResult
from fantasy_war_room.decision.recommend import recommend
from fantasy_war_room.errors import InputError
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.strategy.adjust import apply_strategy, validate_strategy_compatibility
from fantasy_war_room.strategy.models import StrategyProfile, StrategyRecommendationResult
from fantasy_war_room.strategy.presentation import limit_strategy_result


def build_recommendation(
    repository: IntelligenceRepository,
    at: datetime,
    *,
    draft_id: str | None,
    league_id: str | None,
    sleeper_user_id: str | None,
    draft_slot: int | None,
    ranking_source: str | None,
    limit: int,
    model_version: RecommendationModelVersion = "baseline-1.0",
    strategy_profile: StrategyProfile | None = None,
) -> RecommendationResult | StrategyRecommendationResult:
    """Select immutable local inputs, run the chosen policy, then limit presentation."""
    inputs = repository.recommendation_inputs(
        at,
        draft_id=draft_id,
        league_id=league_id,
        sleeper_user_id=sleeper_user_id,
        draft_slot=draft_slot,
        ranking_source=ranking_source,
    )
    if strategy_profile is not None:
        validate_strategy_compatibility(inputs, strategy_profile, raw_model=model_version)
    result = recommend(inputs, model_version)
    if not result.candidates:
        raise InputError(
            "insufficient_projection_depth",
            "No candidates have sufficient structural projection depth to score",
            {"excluded_candidate_counts": result.excluded_candidate_counts},
        )
    if strategy_profile is not None:
        return limit_strategy_result(apply_strategy(result, inputs, strategy_profile), limit)
    return result.model_copy(update={"candidates": result.candidates[:limit]})
