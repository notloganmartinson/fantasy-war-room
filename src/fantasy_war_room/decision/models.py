from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OffensivePosition = Literal["QB", "RB", "WR", "TE"]
ProjectionValueKind = Literal["exact", "known_component"]
RecommendationModelVersion = Literal["baseline-1.0", "trusted-board-1.0", "trusted-board-1.1"]


class DecisionModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class RosterConfiguration(DecisionModel):
    schema_version: str = "1.0"
    qb: int = Field(ge=0)
    rb: int = Field(ge=0)
    wr: int = Field(ge=0)
    te: int = Field(ge=0)
    flex: int = Field(ge=0)
    bench: int = Field(default=0, ge=0)
    k: int = Field(default=0, ge=0)
    defense: int = Field(default=0, ge=0)


class CompletedDraftPick(DecisionModel):
    schema_version: str = "1.0"
    pick_no: int = Field(gt=0)
    draft_slot: int | None = Field(default=None, gt=0)
    canonical_player_id: str | None = None
    sleeper_player_id: str | None = None
    position: str | None = None


class RecommendationPlayerInput(DecisionModel):
    schema_version: str = "1.0"
    canonical_player_id: str
    sleeper_player_id: str | None = None
    player_name: str
    position: OffensivePosition
    team: str | None = None
    league_projected_points: float | None = None
    league_known_component_points: float | None = None
    cbs_projected_points: float | None = None
    scoring_completeness: Literal["complete", "partial"]
    unprojected_scoring_keys: tuple[str, ...] = ()


class ExpertRankingInput(DecisionModel):
    schema_version: str = "1.0"
    canonical_player_id: str
    overall_rank: float | None = None
    positional_rank: str | None = None
    tier: str | None = None


class RecommendationProvenance(DecisionModel):
    schema_version: str = "1.0"
    draft_snapshot_id: str
    player_snapshot_id: str
    ranking_snapshot_id: str
    projection_snapshot_id: str
    ranking_source: str
    ranking_source_version: str | None = None
    projection_source: str
    projection_source_version: str
    ranking_resolver_version: str
    scoring_calculator_version: str
    scoring_settings_hash: str
    draft_observed_at: datetime | None = None
    player_observed_at: datetime | None = None
    player_fetched_at: datetime | None = None
    ranking_observed_at: datetime | None = None
    ranking_imported_at: datetime | None = None
    projection_observed_at: datetime | None = None
    projection_imported_at: datetime | None = None
    projection_player_snapshot_id: str | None = None
    projection_league_snapshot_id: str | None = None
    scoring_context_league_id: str | None = None


class RecommendationInputs(DecisionModel):
    schema_version: str = "1.0"
    decision_at: datetime
    team_count: int = Field(gt=0)
    draft_type: str
    draft_rounds: int = Field(gt=0)
    draft_slot: int = Field(gt=0)
    roster: RosterConfiguration
    completed_picks: tuple[CompletedDraftPick, ...]
    projected_players: tuple[RecommendationPlayerInput, ...]
    expert_rankings: tuple[ExpertRankingInput, ...]
    provenance: RecommendationProvenance
    unresolved_roster_player_ids: tuple[str, ...] = ()
    sport: Literal["nfl"] = "nfl"
    league_type: Literal["redraft", "keeper", "dynasty", "unknown"] = "unknown"
    keeper_status: Literal["non_keeper", "keeper", "unknown"] = "unknown"
    scoring_format: Literal["full_ppr", "half_ppr", "standard", "custom"] = "full_ppr"


class DraftTurnContext(DecisionModel):
    schema_version: str = "1.0"
    next_overall_pick: int
    current_round: int
    snake_direction: Literal["forward", "reverse"]
    draft_slot: int
    on_the_clock: bool
    user_next_scheduled_pick: int
    opponent_picks_before_next_user_pick: int
    user_following_scheduled_pick: int | None
    opponent_picks_between_user_selections: int | None


class LineupAssignment(DecisionModel):
    schema_version: str = "1.0"
    slot: str
    canonical_player_id: str
    player_name: str
    position: OffensivePosition
    projection: float
    projection_value_kind: ProjectionValueKind


class RosterAllocation(DecisionModel):
    schema_version: str = "1.0"
    allocator_version: str = "max-projection-offensive-flex-1.0"
    starters: tuple[LineupAssignment, ...]
    bench_player_ids: tuple[str, ...]
    unmodeled_player_ids: tuple[str, ...]
    vacancies: dict[str, int]
    starting_lineup_projection: float


class ReplacementLevel(DecisionModel):
    schema_version: str = "1.0"
    replacement_model_version: str = "structural-starter-demand-1.0"
    position: OffensivePosition
    universe_size: int
    fixed_demand: int
    allocated_flex_demand: int
    total_demand: int
    replacement_player_id: str | None
    replacement_projection: float | None
    replacement_projection_value_kind: ProjectionValueKind | None


class PositionalScarcity(DecisionModel):
    schema_version: str = "1.0"
    scarcity_model_version: str = "one-round-drop-1.0"
    comparison_player_ids: tuple[str, ...]
    comparison_count: int
    comparison_mean_projection: float | None = None
    scarcity_points: float | None = None


class PlayerReassignment(DecisionModel):
    schema_version: str = "1.0"
    canonical_player_id: str
    from_slot: str
    to_slot: str


class RosterEffect(DecisionModel):
    schema_version: str = "1.0"
    category: Literal[
        "fills_fixed_vacancy",
        "fills_flex_vacancy",
        "upgrades_fixed_starter",
        "upgrades_flex_or_rebalances_lineup",
        "bench_depth",
    ]
    lineup_projection_before: float
    lineup_projection_after: float
    starter_projection_delta: float
    normalized_value: float
    candidate_assigned_slot: str | None
    displaced_starter_id: str | None
    reassignments: tuple[PlayerReassignment, ...]
    moved_to_bench_player_ids: tuple[str, ...]
    promoted_from_bench_player_ids: tuple[str, ...]
    vacancies_before: dict[str, int]
    vacancies_after: dict[str, int]


class ScoreComponent(DecisionModel):
    schema_version: str = "1.0"
    raw_value: float | None
    normalized_value: float
    weight: float
    contribution: float


class NextPickAvailability(DecisionModel):
    schema_version: str = "1.0"
    status: Literal["unsupported_uncalibrated"] = "unsupported_uncalibrated"
    probability_available_at_next_pick: None = None
    reason: str = "No calibrated ADP survival model is available"


class CandidateExplanation(DecisionModel):
    schema_version: str = "1.0"
    recommendation_rank: int
    canonical_player_id: str
    sleeper_player_id: str | None
    player_name: str
    position: OffensivePosition
    team: str | None
    recommendation_score: float
    projection_baseline: float
    projection_value_kind: ProjectionValueKind
    league_projected_points: float | None
    league_known_component_points: float | None
    cbs_projected_points: float | None
    scoring_completeness: Literal["complete", "partial"]
    unprojected_scoring_keys: tuple[str, ...]
    replacement: ReplacementLevel
    vorp: float
    expert_overall_rank: float | None
    expert_positional_rank: str | None
    expert_percentile: float | None
    scarcity: PositionalScarcity
    roster_effect: RosterEffect
    vorp_component: ScoreComponent
    expert_component: ScoreComponent
    scarcity_component: ScoreComponent
    roster_fit_component: ScoreComponent
    next_pick_component: ScoreComponent
    next_pick_availability: NextPickAvailability
    limitations: tuple[str, ...]


class BaselineSelection(DecisionModel):
    schema_version: str = "1.0"
    canonical_player_id: str | None
    player_name: str | None
    raw_value: float | None


class RecommendationBaselines(DecisionModel):
    schema_version: str = "1.0"
    highest_expert_rank: BaselineSelection
    highest_league_projection: BaselineSelection
    greedy_vorp: BaselineSelection


class ModelSpecification(DecisionModel):
    schema_version: str = "1.0"
    recommendation_model_version: RecommendationModelVersion = "baseline-1.0"
    roster_allocator_version: str = "max-projection-offensive-flex-1.0"
    replacement_model_version: str = "structural-starter-demand-1.0"
    scarcity_model_version: str = "one-round-drop-1.0"
    survival_model_version: str = "unavailable-1.0"
    weights: dict[str, float]


class RecommendationResult(DecisionModel):
    schema_version: str = "1.0"
    decision_at: datetime
    turn_context: DraftTurnContext
    current_roster: RosterAllocation
    replacement_levels: tuple[ReplacementLevel, ...]
    candidates: tuple[CandidateExplanation, ...]
    baselines: RecommendationBaselines
    model_specification: ModelSpecification
    provenance: RecommendationProvenance
    excluded_candidate_counts: dict[str, int]
    limitations: tuple[str, ...]


class TrustedBoardCandidateExplanation(CandidateExplanation):
    schema_version: str = "1.1"
    trusted_rank_value: float | None
    trusted_rank_component: ScoreComponent
    trusted_tier: str | None
    trusted_tier_value: float | None
    trusted_tier_component: ScoreComponent


class TrustedBoardModelSpecification(ModelSpecification):
    schema_version: str = "1.1"
    recommendation_model_version: Literal["trusted-board-1.1"] = "trusted-board-1.1"
    trusted_rank_transform_version: str = "exponential-half-life-20-1.0"
    trusted_tier_transform_version: str = "ordinal-s-through-i-1.0"


class TrustedBoardRecommendationResult(RecommendationResult):
    schema_version: str = "1.1"
    candidates: tuple[TrustedBoardCandidateExplanation, ...]
    model_specification: TrustedBoardModelSpecification
