from __future__ import annotations

import json
import math
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from fantasy_war_room.errors import InputError
from fantasy_war_room.models import PlayerDirectorySnapshot, RankingIssue, RankingSnapshot
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.services import canonical_hash
from fantasy_war_room.sleeper import SleeperPlayerProvider

CACHE_MAX_AGE = timedelta(hours=24)
RANKING_FIELDS = ("overall_rank", "positional_rank", "adp", "adp_sd", "projected_points")
NUMERIC_FIELDS = ("overall_rank", "adp", "adp_sd", "projected_points")


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join("".join(char if char.isalnum() else " " for char in normalized).lower().split())


def sync_players(
    provider: SleeperPlayerProvider,
    repository: IntelligenceRepository,
    cache_dir: Path,
    force: bool = False,
    observed_at: datetime | None = None,
) -> tuple[PlayerDirectorySnapshot, bool, str]:
    now = observed_at or datetime.now(UTC)
    cache_path = cache_dir.expanduser().resolve() / "sleeper" / "players-nfl.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    source = "network"
    if not force and cache_path.exists():
        modified = datetime.fromtimestamp(cache_path.stat().st_mtime, UTC)
        if now - modified < CACHE_MAX_AGE:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            fetched_at = modified
            source = "cache"
        else:
            payload = provider.get_nfl_players()
            fetched_at = now
    else:
        payload = provider.get_nfl_players()
        fetched_at = now
    if not isinstance(payload, dict):
        raise InputError("invalid_player_payload", "Sleeper player directory must be an object")
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if source == "network":
        cache_path.write_text(canonical_json + "\n", encoding="utf-8")
    players = [_canonical_player(str(player_id), raw) for player_id, raw in sorted(payload.items())]
    snapshot = PlayerDirectorySnapshot(
        snapshot_id=str(uuid4()),
        observed_at=now,
        fetched_at=fetched_at,
        payload_hash=canonical_hash(payload),
        player_count=len(players),
        raw_cache_path=str(cache_path),
    )
    return snapshot, repository.insert_player_directory(snapshot, players), source


def _canonical_player(player_id: str, raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    first_name = str(value.get("first_name") or "")
    last_name = str(value.get("last_name") or "")
    full_name = str(value.get("full_name") or f"{first_name} {last_name}").strip()
    provider_ids = {"sleeper": player_id}
    aliases = {
        "gsis": ("gsis_id",),
        "espn": ("espn_id",),
        "yahoo": ("yahoo_id",),
        "sportradar": ("sportradar_id",),
        "fantasydata": ("fantasy_data_id", "fantasydata_id"),
    }
    for provider, keys in aliases.items():
        identifier = next((value.get(key) for key in keys if value.get(key) is not None), None)
        if identifier is not None and str(identifier).strip():
            provider_ids[provider] = str(identifier)
    fantasy_positions = value.get("fantasy_positions") or []
    if not isinstance(fantasy_positions, list):
        fantasy_positions = []
    return {
        "canonical_player_id": str(uuid4()),
        "provider_player_id": player_id,
        "first_name": first_name,
        "last_name": last_name,
        "normalized_full_name": normalize_name(full_name),
        "position": _optional_text(value.get("position")),
        "fantasy_positions": [str(item) for item in fantasy_positions],
        "team": _optional_text(value.get("team")),
        "active": value.get("active") if isinstance(value.get("active"), bool) else None,
        "status": _optional_text(value.get("status")),
        "injury_status": _optional_text(value.get("injury_status")),
        "years_experience": _optional_number(value.get("years_exp")),
        "provider_ids": provider_ids,
        "raw_payload": value,
    }


def import_rankings(
    path: Path,
    repository: IntelligenceRepository,
    source: str,
    season: str,
    scoring_format: str,
    league_size: int,
    source_version: str | None = None,
    observed_at: datetime | None = None,
) -> tuple[RankingSnapshot, bool]:
    if league_size <= 0:
        raise InputError("invalid_league_size", "League size must be greater than zero")
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_file():
        raise InputError("ranking_file_not_found", f"Ranking file does not exist: {resolved_path}")
    try:
        frame = pl.read_csv(resolved_path, infer_schema=False)
    except Exception as exc:
        raise InputError("invalid_ranking_csv", f"Cannot read ranking CSV: {exc}") from exc
    if "player_name" not in frame.columns:
        raise InputError("missing_player_name", "Ranking CSV requires a player_name column")
    at = observed_at or datetime.now(UTC)
    imported_at = datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    issues: list[RankingIssue] = []
    for row_number, raw in enumerate(frame.to_dicts(), start=2):
        row = {key: _blank_to_none(value) for key, value in raw.items()}
        player_name = str(row.get("player_name") or "").strip()
        if not player_name:
            raise InputError("invalid_player_name", f"Row {row_number} has no player_name")
        for field in NUMERIC_FIELDS:
            row[field] = _parse_numeric(row.get(field), field, row_number)
        if not any(row.get(field) is not None for field in RANKING_FIELDS):
            continue
        resolution_row = {
            **row,
            "normalized_name": normalize_name(player_name),
            "position": _optional_text(row.get("position")),
            "team": _optional_text(row.get("team")),
        }
        canonical_id, status, reason, candidates = repository.resolve_player(resolution_row, at)
        entry = {
            "source_row_number": row_number,
            "canonical_player_id": canonical_id,
            "player_name": player_name,
            "position": resolution_row["position"],
            "team": resolution_row["team"],
            "overall_rank": row.get("overall_rank"),
            "positional_rank": _optional_text(row.get("positional_rank")),
            "adp": row.get("adp"),
            "adp_sd": row.get("adp_sd"),
            "projected_points": row.get("projected_points"),
            "match_status": status,
            "raw_payload": raw,
        }
        entries.append(entry)
        if status != "matched":
            issues.append(
                RankingIssue(
                    ranking_snapshot_id="pending",
                    source_row_number=row_number,
                    source_player_name=player_name,
                    source_position=resolution_row["position"],
                    source_team=resolution_row["team"],
                    match_status=status,
                    reason=reason,
                    candidate_player_ids=candidates,
                    raw_payload=raw,
                )
            )
    content = {
        "source_version": source_version,
        "rows": [
            {key: value for key, value in entry.items() if key != "canonical_player_id"}
            for entry in entries
        ],
    }
    snapshot_id = str(uuid4())
    for issue in issues:
        issue.ranking_snapshot_id = snapshot_id
    snapshot = RankingSnapshot(
        ranking_snapshot_id=snapshot_id,
        source=source,
        source_version=source_version,
        season=season,
        scoring_format=scoring_format,
        league_size=league_size,
        observed_at=at,
        imported_at=imported_at,
        payload_hash=canonical_hash(content),
        original_filename=resolved_path.name,
        total_row_count=frame.height,
        matched_row_count=sum(entry["match_status"] == "matched" for entry in entries),
        unresolved_row_count=sum(entry["match_status"] == "unresolved" for entry in entries),
        ambiguous_row_count=sum(entry["match_status"] == "ambiguous" for entry in entries),
    )
    return snapshot, repository.insert_ranking_snapshot(snapshot, entries, issues)


def _parse_numeric(value: Any, field: str, row_number: int) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError("invalid_numeric_field", f"Row {row_number} has invalid {field}") from exc
    if not math.isfinite(number):
        raise InputError("invalid_numeric_field", f"Row {row_number} has invalid {field}")
    return number


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


def _optional_text(value: Any) -> str | None:
    return None if value is None or not str(value).strip() else str(value).strip()


def _optional_number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
