from __future__ import annotations

from datetime import datetime

from fantasy_war_room.decision.models import RecommendationModelVersion, RecommendationResult
from fantasy_war_room.decision.recommend import recommend
from fantasy_war_room.errors import InputError
from fantasy_war_room.repository import IntelligenceRepository


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
) -> RecommendationResult:
    """Select immutable local inputs, run the chosen policy, then limit presentation."""
    inputs = repository.recommendation_inputs(
        at,
        draft_id=draft_id,
        league_id=league_id,
        sleeper_user_id=sleeper_user_id,
        draft_slot=draft_slot,
        ranking_source=ranking_source,
    )
    result = recommend(inputs, model_version)
    if not result.candidates:
        raise InputError(
            "insufficient_projection_depth",
            "No candidates have sufficient structural projection depth to score",
            {"excluded_candidate_counts": result.excluded_candidate_counts},
        )
    return result.model_copy(update={"candidates": result.candidates[:limit]})
