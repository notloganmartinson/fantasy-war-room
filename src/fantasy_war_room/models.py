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
    schema_version: str = "1.1"
    snapshot_id: str
    league_id: str | None
    draft_id: str
    observed_at: datetime
    source_updated_at: datetime | None
    payload_hash: str
    pick_count: int
    league: dict[str, Any]
    draft: dict[str, Any]
    picks: list[dict[str, Any]]
    source_league_id: str | None = None
    scoring_context_league_id: str | None = None
    scoring_context: dict[str, Any] | None = None
    draft_context_type: str = "league"


class DraftSummary(BaseModel):
    schema_version: str = "1.0"
    draft_id: str
    status: str
    draft_type: str
    season: str
    team_count: int
    rounds: int | None = None
    league_id: str | None = None
    is_mock: bool
    name: str | None = None
    locally_stored: bool = False


class PlayerDirectorySnapshot(BaseModel):
    schema_version: str = "1.0"
    snapshot_id: str
    provider: str = "sleeper"
    sport: str = "nfl"
    observed_at: datetime
    fetched_at: datetime
    payload_hash: str
    player_count: int
    raw_cache_path: str


class PlayerSearchResult(BaseModel):
    schema_version: str = "1.0"
    canonical_player_id: str
    sleeper_player_id: str
    first_name: str
    last_name: str
    normalized_full_name: str
    position: str | None
    team: str | None
    active: bool | None
    injury_status: str | None
    player_snapshot_id: str
    player_observed_at: datetime

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> PlayerSearchResult:
        return cls(
            canonical_player_id=row[0],
            sleeper_player_id=row[1],
            first_name=row[2],
            last_name=row[3],
            normalized_full_name=row[4],
            position=row[5],
            team=row[6],
            active=row[7],
            injury_status=row[8],
            player_snapshot_id=row[9],
            player_observed_at=row[10],
        )


class RankingSnapshot(BaseModel):
    schema_version: str = "1.0"
    ranking_snapshot_id: str
    source: str
    source_version: str | None
    season: str
    scoring_format: str
    league_size: int
    observed_at: datetime
    imported_at: datetime
    payload_hash: str
    original_filename: str
    total_row_count: int
    matched_row_count: int
    unresolved_row_count: int
    ambiguous_row_count: int
    resolver_version: str = "2.0"
    reprocessed_from_snapshot_id: str | None = None

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> RankingSnapshot:
        return cls(
            ranking_snapshot_id=row[0],
            source=row[1],
            source_version=row[2],
            season=row[3],
            scoring_format=row[4],
            league_size=row[5],
            observed_at=row[6],
            imported_at=row[7],
            payload_hash=row[8],
            original_filename=row[9],
            total_row_count=row[10],
            matched_row_count=row[11],
            unresolved_row_count=row[12],
            ambiguous_row_count=row[13],
            schema_version=row[14],
            resolver_version=row[15],
            reprocessed_from_snapshot_id=row[16],
        )


class RankingIssue(BaseModel):
    schema_version: str = "1.0"
    ranking_snapshot_id: str
    source_row_number: int
    source_player_name: str
    source_position: str | None
    source_team: str | None
    match_status: str
    reason: str
    candidate_player_ids: list[str]
    raw_payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> RankingIssue:
        import json

        return cls(
            ranking_snapshot_id=row[0],
            source_row_number=row[1],
            source_player_name=row[2],
            source_position=row[3],
            source_team=row[4],
            match_status=row[5],
            reason=row[6],
            candidate_player_ids=json.loads(row[7]),
            raw_payload=json.loads(row[8]),
        )


class AdpSnapshot(BaseModel):
    schema_version: str = "1.1"
    adp_snapshot_id: str
    source: str
    source_version: str
    season: str
    league_size: int
    scoring_format: str
    draft_type: str
    observed_at: datetime
    imported_at: datetime
    payload_hash: str
    identity_resolver_version: str
    original_filename: str
    total_row_count: int
    matched_row_count: int
    unresolved_row_count: int
    ambiguous_row_count: int
    source_uri: str | None = None
    fetched_at: datetime | None = None
    source_payload_hash: str | None = None
    transformation_version: str | None = None

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> AdpSnapshot:
        return cls(
            adp_snapshot_id=row[0],
            source=row[1],
            source_version=row[2],
            season=row[3],
            league_size=row[4],
            scoring_format=row[5],
            draft_type=row[6],
            observed_at=row[7],
            imported_at=row[8],
            payload_hash=row[9],
            identity_resolver_version=row[10],
            original_filename=row[11],
            total_row_count=row[12],
            matched_row_count=row[13],
            unresolved_row_count=row[14],
            ambiguous_row_count=row[15],
            schema_version=row[16],
            source_uri=row[17],
            fetched_at=row[18],
            source_payload_hash=row[19],
            transformation_version=row[20],
        )


class AdpIssue(BaseModel):
    schema_version: str = "1.0"
    adp_snapshot_id: str
    source_row_number: int
    source_player_name: str
    source_position: str | None
    source_team: str | None
    match_status: str
    reason: str
    candidate_player_ids: list[str]
    raw_payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> AdpIssue:
        import json

        return cls(
            adp_snapshot_id=row[0],
            source_row_number=row[1],
            source_player_name=row[2],
            source_position=row[3],
            source_team=row[4],
            match_status=row[5],
            reason=row[6],
            candidate_player_ids=json.loads(row[7]),
            raw_payload=json.loads(row[8]),
        )


class MarketBoardSnapshot(BaseModel):
    schema_version: str = "1.0"
    market_board_snapshot_id: str
    source: str
    source_version: str
    transformation_version: str
    derived_from_adp_snapshot_id: str
    season: str
    league_size: int
    scoring_format: str
    draft_type: str
    observed_at: datetime
    fetched_at: datetime | None
    imported_at: datetime
    payload_hash: str
    source_uri: str | None
    source_payload_hash: str | None
    identity_resolver_version: str
    total_row_count: int
    matched_row_count: int
    unresolved_row_count: int
    ambiguous_row_count: int

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> MarketBoardSnapshot:
        return cls(
            market_board_snapshot_id=row[0],
            source=row[1],
            source_version=row[2],
            transformation_version=row[3],
            derived_from_adp_snapshot_id=row[4],
            season=row[5],
            league_size=row[6],
            scoring_format=row[7],
            draft_type=row[8],
            observed_at=row[9],
            fetched_at=row[10],
            imported_at=row[11],
            payload_hash=row[12],
            source_uri=row[13],
            source_payload_hash=row[14],
            identity_resolver_version=row[15],
            total_row_count=row[16],
            matched_row_count=row[17],
            unresolved_row_count=row[18],
            ambiguous_row_count=row[19],
            schema_version=row[20],
        )


class MarketBoardIssue(BaseModel):
    schema_version: str = "1.0"
    market_board_snapshot_id: str
    source_row_number: int
    source_player_name: str
    source_position: str | None
    source_team: str | None
    match_status: str
    reason: str
    candidate_player_ids: list[str]
    raw_payload: dict[str, Any]


class TeamScheduleSnapshot(BaseModel):
    schema_version: str = "1.1"
    schedule_snapshot_id: str
    source: str
    source_version: str
    season: str
    observed_at: datetime
    imported_at: datetime
    payload_hash: str
    original_filename: str
    total_row_count: int
    source_uri: str | None = None
    fetched_at: datetime | None = None
    source_payload_hash: str | None = None
    transformation_version: str | None = None

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> TeamScheduleSnapshot:
        return cls(
            schedule_snapshot_id=row[0],
            source=row[1],
            source_version=row[2],
            season=row[3],
            observed_at=row[4],
            imported_at=row[5],
            payload_hash=row[6],
            original_filename=row[7],
            total_row_count=row[8],
            schema_version=row[9],
            source_uri=row[10],
            fetched_at=row[11],
            source_payload_hash=row[12],
            transformation_version=row[13],
        )


class ProjectionSnapshot(BaseModel):
    schema_version: str = "1.0"
    projection_snapshot_id: str
    source: str
    source_version: str
    season: str
    horizon: str
    source_scoring_format: str
    observed_at: datetime
    imported_at: datetime
    payload_hash: str
    total_row_count: int
    matched_row_count: int
    unresolved_row_count: int
    ambiguous_row_count: int
    player_snapshot_id: str
    league_snapshot_id: str
    scoring_settings_hash: str
    scoring_settings: dict[str, float]
    scoring_calculator_version: str

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> ProjectionSnapshot:
        import json

        return cls(
            projection_snapshot_id=row[0],
            source=row[1],
            source_version=row[2],
            season=row[3],
            horizon=row[4],
            source_scoring_format=row[5],
            observed_at=row[6],
            imported_at=row[7],
            payload_hash=row[8],
            total_row_count=row[9],
            matched_row_count=row[10],
            unresolved_row_count=row[11],
            ambiguous_row_count=row[12],
            player_snapshot_id=row[13],
            league_snapshot_id=row[14],
            scoring_settings_hash=row[15],
            scoring_settings=json.loads(row[16]),
            scoring_calculator_version=row[17],
            schema_version=row[18],
        )


class ProjectionIssue(BaseModel):
    schema_version: str = "1.0"
    projection_snapshot_id: str
    source_position: str
    source_row_number: int
    source_player_name: str
    source_team: str | None
    match_status: str
    reason: str
    candidate_player_ids: list[str]
    raw_payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> ProjectionIssue:
        import json

        return cls(
            projection_snapshot_id=row[0],
            source_position=row[1],
            source_row_number=row[2],
            source_player_name=row[3],
            source_team=row[4],
            match_status=row[5],
            reason=row[6],
            candidate_player_ids=json.loads(row[7]),
            raw_payload=json.loads(row[8]),
            schema_version=row[9],
        )


class BoardPlayer(BaseModel):
    schema_version: str = "1.0"
    canonical_player_id: str
    sleeper_player_id: str | None
    player_name: str
    position: str | None
    team: str | None
    overall_rank: float | None
    positional_rank: str | None
    adp: float | None
    adp_sd: float | None
    projected_points: float | None
    ranking_source: str
    draft_snapshot_id: str
    draft_observed_at: datetime
    player_snapshot_id: str
    player_observed_at: datetime
    ranking_snapshot_id: str
    ranking_observed_at: datetime

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> BoardPlayer:
        return cls(
            canonical_player_id=row[0],
            sleeper_player_id=row[1],
            player_name=row[2],
            position=row[3],
            team=row[4],
            overall_rank=row[5],
            positional_rank=row[6],
            adp=row[7],
            adp_sd=row[8],
            projected_points=row[9],
            ranking_source=row[10],
            draft_snapshot_id=row[11],
            draft_observed_at=row[12],
            player_snapshot_id=row[13],
            player_observed_at=row[14],
            ranking_snapshot_id=row[15],
            ranking_observed_at=row[16],
        )
