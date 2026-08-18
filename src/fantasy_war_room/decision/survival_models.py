from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fantasy_war_room.decision.models import RosterConfiguration

SurvivalModelVersion = Literal[
    "adp-only-1.0",
    "adp-dispersion-1.0",
    "adp-dispersion-roster-1.0",
]
CandidateSurvivalStatus = Literal[
    "modeled",
    "already_drafted",
    "missing_compatible_adp",
    "insufficient_modeled_pool",
    "invalid_candidate",
]


class SurvivalModel(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)


class SurvivalModelSpecification(SurvivalModel):
    schema_version: Literal["fwr.survival-model-specification/1.0"] = (
        "fwr.survival-model-specification/1.0"
    )
    model_version: SurvivalModelVersion
    pick_weight_model: str
    dispersion_model: str
    roster_adjustment_model: str
    rng_version: Literal["splitmix64-inverse-cdf-1.0"] = "splitmix64-inverse-cdf-1.0"
    parameters: dict[str, int | float | str | bool]


class SurvivalDraftContext(SurvivalModel):
    schema_version: Literal["fwr.survival-draft-context/1.0"] = "fwr.survival-draft-context/1.0"
    draft_snapshot_id: str
    draft_id: str
    observed_at: datetime
    payload_hash: str
    team_count: int = Field(gt=0)
    round_count: int = Field(gt=0)
    draft_type: Literal["snake"] = "snake"
    user_draft_slot: int = Field(gt=0)
    current_overall_pick: int = Field(gt=0)
    user_is_on_the_clock: bool
    simulation_start_pick: int = Field(gt=0)
    target_user_pick: int = Field(gt=0)
    intervening_opponent_pick_count: int = Field(ge=0)


class PassNowInterval(SurvivalModel):
    schema_version: Literal["fwr.pass-now-interval/1.0"] = "fwr.pass-now-interval/1.0"
    current_overall_pick: int = Field(gt=0)
    user_is_on_the_clock: bool
    simulation_start_pick: int = Field(gt=0)
    target_user_pick: int = Field(gt=0)
    intervening_opponent_pick_count: int = Field(ge=0)


class SurvivalAdpSnapshotInput(SurvivalModel):
    schema_version: Literal["fwr.survival-adp-snapshot-input/1.0"] = (
        "fwr.survival-adp-snapshot-input/1.0"
    )
    adp_snapshot_id: str
    source: str
    source_version: str
    observed_at: datetime
    imported_at: datetime
    payload_hash: str
    source_payload_hash: str | None = None
    transformation_version: str | None = None
    season: str
    league_size: int = Field(gt=0)
    scoring_format: str
    draft_type: Literal["snake"] = "snake"


class SurvivalPlayerInput(SurvivalModel):
    schema_version: Literal["fwr.survival-player-input/1.0"] = "fwr.survival-player-input/1.0"
    canonical_player_id: str
    sleeper_player_id: str | None = None
    position: str
    overall_adp: float | None = Field(default=None, gt=0)
    adp_sd: float | None = Field(default=None, ge=0)
    sample_size: int | None = Field(default=None, ge=0)


class SurvivalCandidateInput(SurvivalModel):
    schema_version: Literal["fwr.survival-candidate-input/1.0"] = "fwr.survival-candidate-input/1.0"
    canonical_player_id: str


class SurvivalCompletedPick(SurvivalModel):
    schema_version: Literal["fwr.survival-completed-pick/1.0"] = "fwr.survival-completed-pick/1.0"
    pick_no: int = Field(gt=0)
    draft_slot: int | None = Field(default=None, gt=0)
    canonical_player_id: str | None = None
    position: str | None = None


class OpponentRosterState(SurvivalModel):
    schema_version: Literal["fwr.opponent-roster-state/1.0"] = "fwr.opponent-roster-state/1.0"
    draft_slot: int = Field(gt=0)
    qb: int = Field(default=0, ge=0)
    rb: int = Field(default=0, ge=0)
    wr: int = Field(default=0, ge=0)
    te: int = Field(default=0, ge=0)
    k: int = Field(default=0, ge=0)
    defense: int = Field(default=0, ge=0)


class NextPickSurvivalInputs(SurvivalModel):
    schema_version: Literal["fwr.next-pick-survival-input/1.0"] = "fwr.next-pick-survival-input/1.0"
    decision_at: datetime
    draft: SurvivalDraftContext
    adp: SurvivalAdpSnapshotInput
    available_players: tuple[SurvivalPlayerInput, ...]
    candidates: tuple[SurvivalCandidateInput, ...]
    completed_picks: tuple[SurvivalCompletedPick, ...]
    opponent_rosters: tuple[OpponentRosterState, ...]
    roster_configuration: RosterConfiguration
    simulation_count: int = Field(ge=1, le=100_000)
    seed: int = Field(ge=0, le=2**64 - 1)
    model_specification: SurvivalModelSpecification


class PickMassFit(SurvivalModel):
    schema_version: Literal["fwr.pick-mass-fit/1.0"] = "fwr.pick-mass-fit/1.0"
    status: Literal["fitted", "fallback", "unavailable"]
    mass: tuple[float, ...] | None
    fit_model_version: str
    mean_error: float | None = None
    variance_error: float | None = None
    iterations: int | None = None
    fallback_reason: str | None = None


class ModeledPoolCoverage(SurvivalModel):
    schema_version: Literal["fwr.survival-pool-coverage/1.0"] = "fwr.survival-pool-coverage/1.0"
    policy_version: Literal["pool-coverage-policy-1.0"] = "pool-coverage-policy-1.0"
    total_available_relevant_players: int = Field(ge=0)
    compatible_adp_available_players: int = Field(ge=0)
    modeled_available_players: int = Field(ge=0)
    missing_compatible_adp_players: int = Field(ge=0)
    failed_distribution_fit_players: int = Field(ge=0)
    dispersion_fallback_players: int = Field(ge=0)
    coverage_rate: float = Field(ge=0, le=1)
    modeled_pool_to_intervening_pick_ratio: float | None = Field(default=None, ge=0)
    modeled_by_position: dict[str, int]
    missing_by_position: dict[str, int]
    hard_minimum_required: int = Field(ge=1)
    hard_minimum_satisfied: bool
    warning_codes: tuple[str, ...]


class CandidateSurvivalResult(SurvivalModel):
    schema_version: Literal["fwr.candidate-survival-result/1.0"] = (
        "fwr.candidate-survival-result/1.0"
    )
    canonical_player_id: str
    status: CandidateSurvivalStatus
    target_user_pick: int = Field(gt=0)
    survived_simulation_count: int | None = Field(default=None, ge=0)
    simulation_count: int = Field(ge=1)
    simulated_availability_rate: float | None = Field(default=None, ge=0, le=1)
    monte_carlo_standard_error: float | None = Field(default=None, ge=0)
    interpretation: Literal["conditional_on_user_passing_candidate"] = (
        "conditional_on_user_passing_candidate"
    )


class NextPickSurvivalResult(SurvivalModel):
    schema_version: Literal["fwr.next-pick-survival/1.0"] = "fwr.next-pick-survival/1.0"
    model_version: SurvivalModelVersion
    decision_at: datetime
    seed: int
    simulation_count: int
    current_overall_pick: int
    simulation_start_pick: int
    target_user_pick: int
    intervening_opponent_pick_count: int
    user_is_on_the_clock: bool
    draft_snapshot_id: str
    adp_snapshot_id: str
    input_fingerprint: str
    model_specification: SurvivalModelSpecification
    pool_coverage: ModeledPoolCoverage
    candidates: tuple[CandidateSurvivalResult, ...]
    limitations: tuple[str, ...]


class SimulatedSelection(SurvivalModel):
    pick_no: int
    draft_slot: int
    canonical_player_id: str
    position: str


class OpponentIntervalResult(SurvivalModel):
    selections: tuple[SimulatedSelection, ...]
    remaining_player_ids: tuple[str, ...]
    opponent_rosters: tuple[OpponentRosterState, ...]
