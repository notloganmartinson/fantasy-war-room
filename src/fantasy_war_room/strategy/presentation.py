from __future__ import annotations

from fantasy_war_room.strategy.models import StrategyRecommendationResult


def limit_strategy_result(
    result: StrategyRecommendationResult, limit: int
) -> StrategyRecommendationResult:
    """Limit only actionable presentation rows after complete strategy evaluation."""
    return result.model_copy(update={"candidates": result.candidates[:limit]})
