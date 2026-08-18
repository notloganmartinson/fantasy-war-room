from __future__ import annotations

import math
from collections.abc import Iterable

from fantasy_war_room.decision.survival_evaluation_models import (
    CalibrationBucket,
    EvaluationCandidatePolicy,
    LabeledHistoricalPrediction,
    SurvivalModelEvaluation,
)
from fantasy_war_room.decision.survival_models import NextPickSurvivalInputs, SurvivalModelVersion

LOG_LOSS_CLIP_EPSILON = 1e-6
CALIBRATION_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def select_evaluation_candidates(
    inputs: NextPickSurvivalInputs,
    recommendation_player_ids: Iterable[str],
    policy: EvaluationCandidatePolicy = EvaluationCandidatePolicy(),
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    available = {player.canonical_player_id: player for player in inputs.available_players}
    recommendations = tuple(
        player_id
        for player_id in dict.fromkeys(recommendation_player_ids)
        if player_id in available
    )[: policy.recommendation_limit]
    distance = inputs.draft.team_count * policy.adp_window_league_rounds
    lower = inputs.draft.target_user_pick - distance
    upper = inputs.draft.target_user_pick + distance
    adp_window = tuple(
        player.canonical_player_id
        for player in inputs.available_players
        if player.overall_adp is not None and lower <= player.overall_adp <= upper
    )
    union = tuple(sorted(set(recommendations) | set(adp_window)))
    return union, tuple(sorted(recommendations)), tuple(sorted(adp_window))


def brier_score(predictions: Iterable[tuple[float, bool]]) -> float | None:
    rows = tuple(predictions)
    if not rows:
        return None
    return sum((rate - float(actual)) ** 2 for rate, actual in rows) / len(rows)


def log_loss(
    predictions: Iterable[tuple[float, bool]], epsilon: float = LOG_LOSS_CLIP_EPSILON
) -> float | None:
    rows = tuple(predictions)
    if not rows:
        return None
    return -sum(
        math.log(min(1 - epsilon, max(epsilon, rate)))
        if actual
        else math.log(1 - min(1 - epsilon, max(epsilon, rate)))
        for rate, actual in rows
    ) / len(rows)


def calibration_buckets(
    predictions: Iterable[tuple[float, bool]],
) -> tuple[CalibrationBucket, ...]:
    rows = tuple(predictions)
    result: list[CalibrationBucket] = []
    for index, (lower, upper) in enumerate(
        zip(CALIBRATION_EDGES[:-1], CALIBRATION_EDGES[1:], strict=True)
    ):
        inclusive = index == len(CALIBRATION_EDGES) - 2
        selected = [
            (rate, actual)
            for rate, actual in rows
            if lower <= rate and (rate <= upper if inclusive else rate < upper)
        ]
        result.append(
            CalibrationBucket(
                lower_bound=lower,
                upper_bound=upper,
                upper_bound_inclusive=inclusive,
                count=len(selected),
                mean_simulated_availability_rate=(
                    sum(rate for rate, _ in selected) / len(selected) if selected else None
                ),
                observed_survival_rate=(
                    sum(actual for _, actual in selected) / len(selected) if selected else None
                ),
            )
        )
    return tuple(result)


def evaluate_model(
    model_version: SurvivalModelVersion, predictions: Iterable[LabeledHistoricalPrediction]
) -> SurvivalModelEvaluation:
    rows = tuple(predictions)
    values = tuple((row.simulated_availability_rate, row.survived) for row in rows)
    count = len(values)
    return SurvivalModelEvaluation(
        model_version=model_version,
        evaluated_candidate_count=count,
        actual_survival_rate=(sum(actual for _, actual in values) / count if count else None),
        mean_simulated_availability_rate=(
            sum(rate for rate, _ in values) / count if count else None
        ),
        brier_score=brier_score(values),
        log_loss=log_loss(values),
        calibration_buckets=calibration_buckets(values),
    )
