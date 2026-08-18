from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fantasy_war_room.decision.survival_models import SurvivalModelVersion


class EvaluationModel(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)


class EvaluationCandidatePolicy(EvaluationModel):
    schema_version: Literal["fwr.survival-evaluation-candidates/1.0"] = (
        "fwr.survival-evaluation-candidates/1.0"
    )
    policy_version: Literal["decision-candidates-1.0"] = "decision-candidates-1.0"
    recommendation_limit: int = 10
    adp_window_league_rounds: int = 1


class HistoricalPrediction(EvaluationModel):
    feature_snapshot_id: str
    feature_cutoff: datetime
    current_overall_pick: int
    target_user_pick: int
    canonical_player_id: str
    model_version: SurvivalModelVersion
    simulated_availability_rate: float = Field(ge=0, le=1)


class LabeledHistoricalPrediction(HistoricalPrediction):
    label_snapshot_id: str
    survived: bool


class CalibrationBucket(EvaluationModel):
    lower_bound: float
    upper_bound: float
    upper_bound_inclusive: bool
    count: int = Field(ge=0)
    mean_simulated_availability_rate: float | None = Field(default=None, ge=0, le=1)
    observed_survival_rate: float | None = Field(default=None, ge=0, le=1)


class SurvivalModelEvaluation(EvaluationModel):
    model_version: SurvivalModelVersion
    evaluated_candidate_count: int = Field(ge=0)
    actual_survival_rate: float | None = Field(default=None, ge=0, le=1)
    mean_simulated_availability_rate: float | None = Field(default=None, ge=0, le=1)
    brier_score: float | None = Field(default=None, ge=0, le=1)
    log_loss: float | None = Field(default=None, ge=0)
    log_loss_clip_epsilon: float = 1e-6
    calibration_buckets: tuple[CalibrationBucket, ...]


class EvaluationCandidatePopulationSummary(EvaluationModel):
    total_available_players: int = Field(ge=0)
    adp_covered_available_players: int = Field(ge=0)
    eligible_evaluation_candidates: int = Field(ge=0)
    modeled_evaluation_candidates: int = Field(ge=0)
    recommendation_selected_count: int = Field(ge=0)
    adp_window_selected_count: int = Field(ge=0)
    overlap_count: int = Field(ge=0)
    missing_adp_eligible_count: int = Field(ge=0)
    candidate_policy_version: str


class HistoricalCaseProvenance(EvaluationModel):
    feature_snapshot_id: str
    feature_cutoff: datetime
    label_snapshot_id: str | None
    label_observed_at: datetime | None
    current_overall_pick: int
    simulation_start_pick: int
    target_user_pick: int
    adp_snapshot_id: str
    recommendation_snapshot_id: str | None = None
    eligible_candidate_ids: tuple[str, ...]
    labeled_candidate_ids: tuple[str, ...]
    observed_survivor_ids: tuple[str, ...] = ()
    observed_opponent_selected_ids: tuple[str, ...] = ()
    excluded_current_user_selection_id: str | None = None


class SurvivalEvaluationReport(EvaluationModel):
    schema_version: Literal["fwr.survival-evaluation/1.0"] = "fwr.survival-evaluation/1.0"
    evaluation_policy: EvaluationCandidatePolicy
    draft_id: str
    draft_slot: int
    seed: int
    simulation_count: int
    eligible_decision_point_count: int
    evaluated_decision_point_count: int
    candidate_population: EvaluationCandidatePopulationSummary
    models: tuple[SurvivalModelEvaluation, ...]
    exclusions: dict[str, int]
    cases: tuple[HistoricalCaseProvenance, ...]
    evidence_sufficient_for_default_change: bool
    recommended_default: SurvivalModelVersion
    recommendation_reason: str
    limitations: tuple[str, ...]
