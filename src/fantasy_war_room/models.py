from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class LeagueSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: str = "1.0"
    league_id: str
    name: str
    status: str
    draft_id: str | None
    total_rosters: int


class Snapshot(BaseModel):
    schema_version: str = "1.0"
    snapshot_id: str
    league_id: str
    draft_id: str
    observed_at: datetime
    source_updated_at: datetime | None
    payload_hash: str
    pick_count: int
    league: dict[str, Any]
    draft: dict[str, Any]
    picks: list[dict[str, Any]]
