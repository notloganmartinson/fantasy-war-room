from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

from fantasy_war_room.decision.models import CandidateExplanation, RecommendationResult

WindowMode = Literal["hard_gate", "promotion_only"]
TargetState = Literal[
    "too_early",
    "in_window",
    "deferred_pending_market_context",
    "acquired_by_user",
    "selected_by_opponent",
    "window_expired",
    "fallback_inactive",
]


class StrategyModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class TargetProfile(StrategyModel):
    player_name: str
    sleeper_player_id: str | None = None
    priority: Literal["high", "preferred"]
    role: Literal["primary", "fallback"] = "primary"
    fallback_for: str | None = None
    window_mode: WindowMode
    earliest_round: int | None = Field(default=None, ge=1)
    latest_round: int | None = Field(default=None, ge=1)
    preferred_overall_pick: int | None = Field(default=None, ge=1)
    max_raw_score_deficit: float = Field(default=5.0, ge=0)
    max_raw_rank_displacement: int = Field(default=2, ge=0)
    deferred_until_market_context: bool = False

    @model_validator(mode="after")
    def valid_window(self) -> TargetProfile:
        if (
            self.earliest_round is not None
            and self.latest_round is not None
            and self.latest_round < self.earliest_round
        ):
            raise ValueError("latest_round must not precede earliest_round")
        if self.deferred_until_market_context and self.window_mode != "promotion_only":
            raise ValueError("market-deferred targets must use promotion_only")
        return self


class RedundantDepthPolicy(StrategyModel):
    demotion_class: Literal["redundant_qb_depth", "redundant_te_depth"]
    late_round_exception_start_round: int | None = Field(default=None, ge=1)


class ReservedPositionTarget(StrategyModel):
    position: Literal["QB", "RB", "WR", "TE"]
    target_player_name: str
    suppress_other_candidates_while_active: bool = True


class Te2ValuePolicy(StrategyModel):
    starter_or_flex_class: Literal["te2_starter_or_flex"] = "te2_starter_or_flex"
    bench_value_class: Literal["te2_bench_value"] = "te2_bench_value"
    ordinary_depth_class: Literal["redundant_te_depth"] = "redundant_te_depth"
    max_bench_value_raw_score_deficit: float = Field(default=5.0, ge=0)
    max_bench_value_raw_rank_displacement: int = Field(default=5, ge=0)


class RosterCompletionGuard(StrategyModel):
    required_positions: tuple[Literal["K", "DEF"], ...] = ("K", "DEF")
    trigger: Literal["remaining_user_picks_lte_unfilled_required_slots"] = (
        "remaining_user_picks_lte_unfilled_required_slots"
    )


class StrategyProfile(StrategyModel):
    schema_version: Literal["1.1"] = "1.1"
    profile_name: str
    strategy_adjuster_version: Literal["lexicographic-strategy-1.1"] = "lexicographic-strategy-1.1"
    required_raw_model: Literal["trusted-board-1.1"] = "trusted-board-1.1"
    required_ranking_source: str = "parlay-play-hybrid"
    sport: Literal["nfl"] = "nfl"
    team_count: int = Field(default=10, gt=0)
    compatible_draft_slots: tuple[int, ...] = (7,)
    draft_type: Literal["snake"] = "snake"
    league_type: Literal["redraft"] = "redraft"
    keeper_status: Literal["non_keeper"] = "non_keeper"
    scoring_format: Literal["full_ppr"] = "full_ppr"
    qb: int = 1
    te: int = 1
    flex: int = 2
    k: int = 1
    defense: int = 1
    allow_wr_wr_start: bool = True
    default_max_target_raw_score_deficit: float = 5.0
    default_max_target_raw_rank_displacement: int = 2
    qb2_policy: RedundantDepthPolicy
    te2_policy: Te2ValuePolicy
    reserved_position_targets: tuple[ReservedPositionTarget, ...] = ()
    te3_prohibited: bool = True
    prefer_rb_wr_over_redundant_qb_te: bool = True
    roster_completion_guard: RosterCompletionGuard = RosterCompletionGuard()
    targets: tuple[TargetProfile, ...]

    @model_validator(mode="after")
    def valid_reserved_targets(self) -> StrategyProfile:
        targets = {target.player_name: target for target in self.targets}
        positions: set[str] = set()
        for reserved in self.reserved_position_targets:
            if reserved.position in positions:
                raise ValueError(
                    f"reserved position {reserved.position} is configured more than once"
                )
            positions.add(reserved.position)
            if reserved.target_player_name not in targets:
                raise ValueError(
                    f"reserved target {reserved.target_player_name!r} is not a configured target"
                )
        return self


class TargetEvaluation(StrategyModel):
    player_name: str
    canonical_player_id: str | None
    state: TargetState
    window_mode: WindowMode
    raw_rank: int | None = None
    raw_score_deficit: float | None = None
    raw_rank_displacement: int | None = None
    within_cost_ceiling: bool = False
    reason: str


class StrategyCandidate(StrategyModel):
    schema_version: Literal["1.1"] = "1.1"
    strategy_rank: int
    canonical_player_id: str
    raw_rank: int
    raw_score: float
    eligible: bool
    target_promotion_class: Literal["eligible_target_within_cost", "no_promotion"]
    positional_utility_class: Literal[
        "normal_depth",
        "te2_starter_or_flex",
        "te2_bench_value",
        "redundant_qb_depth",
        "redundant_te_depth",
        "reserved_position_suppressed",
    ]
    reason_codes: tuple[str, ...]
    raw_candidate: SerializeAsAny[CandidateExplanation]


class StrategyProvenance(StrategyModel):
    schema_version: Literal["1.0"] = "1.0"
    profile_name: str
    profile_hash: str
    profile_temporal_status: Literal["current_explicit_profile"] = "current_explicit_profile"
    immutable_profile_snapshot_used: bool = False
    required_raw_model: str
    required_ranking_source: str


class ReservedPositionTargetState(StrategyModel):
    position: Literal["QB", "RB", "WR", "TE"]
    target_player_name: str
    canonical_player_id: str | None
    target_state: TargetState
    active: bool
    suppression_applied: bool
    reason: str


class StrategyValueSummaryItem(StrategyModel):
    canonical_player_id: str
    player_name: str
    position: Literal["QB", "RB", "WR", "TE"]
    raw_rank: int
    raw_score: float
    strategy_rank: int | None
    eligible: bool
    positional_utility_class: str
    reason_codes: tuple[str, ...]


class StrategyValueSummary(StrategyModel):
    schema_version: Literal["1.0"] = "1.0"
    best_raw_candidate: StrategyValueSummaryItem | None
    actionable_choice: StrategyValueSummaryItem | None
    best_available_by_position: dict[str, StrategyValueSummaryItem | None]
    highest_raw_ranked_suppressed_candidate: StrategyValueSummaryItem | None
    highest_raw_ranked_redundancy_affected_candidate: StrategyValueSummaryItem | None


class RosterCompletionDirective(StrategyModel):
    schema_version: Literal["1.0"] = "1.0"
    code: Literal["roster_completion_required"] = "roster_completion_required"
    rule_version: Literal["required-k-def-reservation-1.0"] = "required-k-def-reservation-1.0"
    boundary_status: Literal["exact_boundary", "already_impossible"]
    remaining_user_selections: int = Field(ge=0)
    unfilled_required_positions: tuple[str, ...]
    required_position_count: int = Field(ge=1)
    message: str = "Fill a required K or DEF slot; M3.5A does not select a specific K or DEF."


class StrategyRecommendationResult(StrategyModel):
    schema_version: Literal["1.1"] = "1.1"
    raw_recommendation: SerializeAsAny[RecommendationResult]
    actionable: bool
    actionable_choice: StrategyCandidate | None
    directive: RosterCompletionDirective | None
    candidates: tuple[StrategyCandidate, ...]
    evaluated_candidates: tuple[StrategyCandidate, ...]
    prohibited_candidates: tuple[StrategyCandidate, ...]
    targets: tuple[TargetEvaluation, ...]
    reserved_position_targets: tuple[ReservedPositionTargetState, ...]
    value_summary: StrategyValueSummary
    roster_completion_required: bool
    remaining_user_selections: int
    unfilled_required_positions: tuple[str, ...]
    strategy_provenance: StrategyProvenance
