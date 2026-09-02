from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from fantasy_war_room.errors import InputError
from fantasy_war_room.identity import normalize_name
from fantasy_war_room.models import AdpIssue, AdpSnapshot, TeamScheduleSnapshot
from fantasy_war_room.repository import IntelligenceRepository

RESOLVER_VERSION = "2.0"


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _csv(path: Path, required: set[str]) -> tuple[Path, pl.DataFrame]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise InputError("import_file_not_found", f"Import file does not exist: {resolved}")
    try:
        frame = pl.read_csv(resolved, infer_schema=False)
    except Exception as error:
        raise InputError("invalid_csv", f"Cannot read CSV: {error}") from error
    missing = sorted(required - set(frame.columns))
    if missing:
        raise InputError(
            "missing_csv_columns", "CSV is missing required columns", {"columns": missing}
        )
    return resolved, frame


def import_adp(
    path: Path,
    repository: IntelligenceRepository,
    *,
    source: str,
    source_version: str,
    season: str,
    scoring_format: str,
    league_size: int,
    draft_type: str,
    observed_at: datetime | None = None,
    source_uri: str | None = None,
    fetched_at: datetime | None = None,
    source_payload_hash: str | None = None,
    transformation_version: str | None = None,
) -> tuple[AdpSnapshot, bool]:
    if league_size <= 0:
        raise InputError("invalid_league_size", "League size must be greater than zero")
    resolved, frame = _csv(path, {"player_name", "overall_adp"})
    return import_adp_frame(
        frame,
        repository,
        original_filename=resolved.name,
        source=source,
        source_version=source_version,
        season=season,
        scoring_format=scoring_format,
        league_size=league_size,
        draft_type=draft_type,
        observed_at=observed_at,
        source_uri=source_uri,
        fetched_at=fetched_at,
        source_payload_hash=source_payload_hash,
        transformation_version=transformation_version,
    )


def import_adp_frame(
    frame: pl.DataFrame,
    repository: IntelligenceRepository,
    *,
    original_filename: str,
    source: str,
    source_version: str,
    season: str,
    scoring_format: str,
    league_size: int,
    draft_type: str,
    observed_at: datetime | None = None,
    source_uri: str | None = None,
    fetched_at: datetime | None = None,
    source_payload_hash: str | None = None,
    transformation_version: str | None = None,
) -> tuple[AdpSnapshot, bool]:
    if league_size <= 0:
        raise InputError("invalid_league_size", "League size must be greater than zero")
    missing = sorted({"player_name", "overall_adp"} - set(frame.columns))
    if missing:
        raise InputError(
            "missing_csv_columns", "ADP data is missing required columns", {"columns": missing}
        )
    at, imported_at = observed_at or fetched_at or datetime.now(UTC), datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    issues: list[AdpIssue] = []
    snapshot_id = str(uuid4())
    for row_number, raw in enumerate(frame.to_dicts(), start=2):
        name = str(raw.get("player_name") or "").strip()
        if not name:
            raise InputError("invalid_player_name", f"Row {row_number} has no player_name")
        try:
            overall_adp = float(str(raw.get("overall_adp") or ""))
            adp_sd = float(raw["adp_sd"]) if raw.get("adp_sd") not in (None, "") else None
            sample_size = (
                int(raw["sample_size"]) if raw.get("sample_size") not in (None, "") else None
            )
        except ValueError as error:
            raise InputError(
                "invalid_adp_value", f"Row {row_number} has invalid numeric data"
            ) from error
        position = str(raw.get("position") or "").strip().upper() or None
        team = str(raw.get("team") or "").strip().upper() or None
        canonical_id, status, reason, candidates, method = repository.resolve_player(
            {
                "player_name": name,
                "normalized_name": normalize_name(name),
                "position": position,
                "team": team,
            },
            at,
        )
        entry = {
            "source_row_number": row_number,
            "canonical_player_id": canonical_id,
            "player_name": name,
            "position": position,
            "team": team,
            "overall_adp": overall_adp,
            "adp_sd": adp_sd,
            "sample_size": sample_size,
            "match_status": status,
            "match_method": method,
            "raw_payload": raw,
        }
        entries.append(entry)
        if status != "matched":
            issues.append(
                AdpIssue(
                    adp_snapshot_id=snapshot_id,
                    source_row_number=row_number,
                    source_player_name=name,
                    source_position=position,
                    source_team=team,
                    match_status=status,
                    reason=reason,
                    candidate_player_ids=candidates,
                    raw_payload=raw,
                )
            )
    content = {
        "source_version": source_version,
        "rows": [{k: v for k, v in row.items() if k != "canonical_player_id"} for row in entries],
    }
    snapshot = AdpSnapshot(
        adp_snapshot_id=snapshot_id,
        source=source,
        source_version=source_version,
        season=season,
        league_size=league_size,
        scoring_format=scoring_format,
        draft_type=draft_type,
        observed_at=at,
        imported_at=imported_at,
        payload_hash=_hash(content),
        identity_resolver_version=RESOLVER_VERSION,
        original_filename=original_filename,
        total_row_count=frame.height,
        matched_row_count=sum(r["match_status"] == "matched" for r in entries),
        unresolved_row_count=sum(r["match_status"] == "unresolved" for r in entries),
        ambiguous_row_count=sum(r["match_status"] == "ambiguous" for r in entries),
        source_uri=source_uri,
        fetched_at=fetched_at,
        source_payload_hash=source_payload_hash,
        transformation_version=transformation_version,
    )
    created = repository.insert_adp_snapshot(snapshot, entries, issues)
    if not created:
        persisted = next(
            row
            for row in repository.adp_snapshots()
            if row.source == snapshot.source
            and row.source_version == snapshot.source_version
            and row.season == snapshot.season
            and row.league_size == snapshot.league_size
            and row.scoring_format == snapshot.scoring_format
            and row.draft_type == snapshot.draft_type
            and row.payload_hash == snapshot.payload_hash
        )
        return persisted, False
    return snapshot, True


def import_team_schedule(
    path: Path,
    repository: IntelligenceRepository,
    *,
    source: str,
    source_version: str,
    season: str,
    observed_at: datetime | None = None,
    source_uri: str | None = None,
    fetched_at: datetime | None = None,
    source_payload_hash: str | None = None,
    transformation_version: str | None = None,
) -> tuple[TeamScheduleSnapshot, bool]:
    resolved, frame = _csv(path, {"team", "bye_week"})
    return import_team_schedule_frame(
        frame,
        repository,
        original_filename=resolved.name,
        source=source,
        source_version=source_version,
        season=season,
        observed_at=observed_at,
        source_uri=source_uri,
        fetched_at=fetched_at,
        source_payload_hash=source_payload_hash,
        transformation_version=transformation_version,
    )


def import_team_schedule_frame(
    frame: pl.DataFrame,
    repository: IntelligenceRepository,
    *,
    original_filename: str,
    source: str,
    source_version: str,
    season: str,
    observed_at: datetime | None = None,
    source_uri: str | None = None,
    fetched_at: datetime | None = None,
    source_payload_hash: str | None = None,
    transformation_version: str | None = None,
) -> tuple[TeamScheduleSnapshot, bool]:
    missing = sorted({"team", "bye_week"} - set(frame.columns))
    if missing:
        raise InputError(
            "missing_csv_columns", "Schedule data is missing required columns", {"columns": missing}
        )
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, raw in enumerate(frame.to_dicts(), start=2):
        team = str(raw.get("team") or "").strip().upper()
        try:
            bye = int(str(raw.get("bye_week") or ""))
        except ValueError as error:
            raise InputError(
                "invalid_bye_week", f"Row {row_number} has invalid bye_week"
            ) from error
        if not team or team in seen or not 1 <= bye <= 18:
            raise InputError(
                "invalid_schedule_row", f"Row {row_number} has invalid or duplicate team"
            )
        seen.add(team)
        entries.append({"team": team, "bye_week": bye, "raw_payload": raw})
    at, imported_at = observed_at or fetched_at or datetime.now(UTC), datetime.now(UTC)
    snapshot = TeamScheduleSnapshot(
        schedule_snapshot_id=str(uuid4()),
        source=source,
        source_version=source_version,
        season=season,
        observed_at=at,
        imported_at=imported_at,
        payload_hash=_hash({"source_version": source_version, "rows": entries}),
        original_filename=original_filename,
        total_row_count=frame.height,
        source_uri=source_uri,
        fetched_at=fetched_at,
        source_payload_hash=source_payload_hash,
        transformation_version=transformation_version,
    )
    return snapshot, repository.insert_schedule_snapshot(snapshot, entries)
