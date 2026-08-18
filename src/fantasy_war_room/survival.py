from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from fantasy_war_room.decision.survival import simulate_next_pick_survival
from fantasy_war_room.decision.survival_models import (
    NextPickSurvivalInputs,
    NextPickSurvivalResult,
    SurvivalModelVersion,
)
from fantasy_war_room.repository import IntelligenceRepository


class SurvivalApplicationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal["fwr.survival-response/1.0"] = "fwr.survival-response/1.0"
    simulation: NextPickSurvivalResult
    provenance: dict[str, Any]


def build_survival_response(
    repository: IntelligenceRepository,
    at: datetime,
    *,
    draft_id: str | None,
    league_id: str | None,
    sleeper_user_id: str | None,
    draft_slot: int | None,
    candidate_player_ids: tuple[str, ...],
    simulation_count: int = 5_000,
    seed: int = 0,
    model_version: SurvivalModelVersion = "adp-only-1.0",
    adp_source: str | None = None,
) -> SurvivalApplicationResponse:
    inputs, provenance = repository.survival_inputs(
        at,
        draft_id=draft_id,
        league_id=league_id,
        sleeper_user_id=sleeper_user_id,
        draft_slot=draft_slot,
        candidate_player_ids=candidate_player_ids,
        simulation_count=simulation_count,
        seed=seed,
        model_version=model_version,
        adp_source=adp_source,
    )
    return survival_response(inputs, provenance)


def survival_response(
    inputs: NextPickSurvivalInputs, provenance: dict[str, Any]
) -> SurvivalApplicationResponse:
    return SurvivalApplicationResponse(
        simulation=simulate_next_pick_survival(inputs),
        provenance=provenance,
    )
