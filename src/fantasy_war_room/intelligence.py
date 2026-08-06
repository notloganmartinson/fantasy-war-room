from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from fantasy_war_room.errors import InputError
from fantasy_war_room.identity import normalize_name
from fantasy_war_room.models import PlayerDirectorySnapshot, RankingIssue, RankingSnapshot
from fantasy_war_room.repository import IntelligenceRepository
from fantasy_war_room.services import canonical_hash
from fantasy_war_room.sleeper import SleeperPlayerProvider

CACHE_MAX_AGE = timedelta(hours=24)
RANKING_RESOLVER_VERSION = "2.0"
RANKING_FIELDS = ("overall_rank", "positional_rank", "adp", "adp_sd", "projected_points")
NUMERIC_FIELDS = ("overall_rank", "adp", "adp_sd", "projected_points")


def sync_players(
    provider: SleeperPlayerProvider,
    repository: IntelligenceRepository,
    cache_dir: Path,
    force: bool = False,
    observed_at: datetime | None = None,
    timings: dict[str, float] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[PlayerDirectorySnapshot, bool, str]:
    started = time.perf_counter()
    now = observed_at or datetime.now(UTC)
    cache_path = cache_dir.expanduser().resolve() / "sleeper" / "players-nfl.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    source = "network"
    io_started = time.perf_counter()
    if not force and cache_path.exists():
        modified = datetime.fromtimestamp(cache_path.stat().st_mtime, UTC)
        if now - modified < CACHE_MAX_AGE:
            with cache_path.open(encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
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
    io_elapsed = time.perf_counter() - io_started
    if source == "network":
        payload_hash = _write_canonical_cache(cache_path, payload)
    else:
        payload_hash = _canonical_cache_hash(cache_path)
    normalization_started = time.perf_counter()
    players = [_canonical_player(str(player_id), raw) for player_id, raw in sorted(payload.items())]
    normalization_elapsed = time.perf_counter() - normalization_started
    del payload
    snapshot = PlayerDirectorySnapshot(
        snapshot_id=str(uuid4()),
        observed_at=now,
        fetched_at=fetched_at,
        payload_hash=payload_hash,
        player_count=len(players),
        raw_cache_path=str(cache_path),
    )
    repository_timings: dict[str, float] = {}
    created = repository.insert_player_directory(snapshot, players, repository_timings, diagnostics)
    if timings is not None:
        timings.update(
            {
                "cache_read_or_network_download": io_elapsed,
                "parsing_and_normalization": normalization_elapsed,
                "identity_resolution": repository_timings.get("identity_resolution", 0.0),
                "database_persistence": repository_timings.get("database_persistence", 0.0),
                "total": time.perf_counter() - started,
            }
        )
    return snapshot, created, source


class _HashingTextWriter:
    def __init__(self, raw: Any, digest: Any) -> None:
        self.raw = raw
        self.digest = digest

    def write(self, value: str) -> int:
        encoded = value.encode("utf-8")
        self.digest.update(encoded)
        return self.raw.write(encoded)


def _write_canonical_cache(path: Path, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as raw:
            writer = _HashingTextWriter(raw, digest)
            json.dump(payload, writer, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            raw.write(b"\n")
        os.replace(temporary_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    return digest.hexdigest()


def _canonical_cache_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as cache_file:
        pending = b""
        while chunk := cache_file.read(1024 * 1024):
            pending += chunk
            if len(pending) > 1:
                digest.update(pending[:-1])
                pending = pending[-1:]
    if pending and pending != b"\n":
        digest.update(pending)
    return digest.hexdigest()


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
        canonical_id, status, reason, candidates, match_method = repository.resolve_player(
            resolution_row, at
        )
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
            "match_method": match_method,
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
        resolver_version=RANKING_RESOLVER_VERSION,
    )
    return snapshot, repository.insert_ranking_snapshot(snapshot, entries, issues)


def reprocess_rankings(
    repository: IntelligenceRepository,
    snapshot_id: str,
    observed_at: datetime | None = None,
) -> tuple[RankingSnapshot, bool]:
    original, rows = repository.ranking_snapshot_for_reprocessing(snapshot_id)
    at = observed_at or datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    issues: list[RankingIssue] = []
    new_snapshot_id = str(uuid4())
    for source_row_number, raw in rows:
        row = {key: _blank_to_none(value) for key, value in raw.items()}
        player_name = str(row.get("player_name") or "").strip()
        position = _optional_text(row.get("position"))
        team = _optional_text(row.get("team"))
        resolution_row = {
            **row,
            "player_name": player_name,
            "normalized_name": normalize_name(player_name),
            "position": position,
            "team": team,
        }
        canonical_id, status, reason, candidates, match_method = repository.resolve_player(
            resolution_row, at
        )
        entry = {
            "source_row_number": source_row_number,
            "canonical_player_id": canonical_id,
            "player_name": player_name,
            "position": position,
            "team": team,
            "overall_rank": _optional_number(row.get("overall_rank")),
            "positional_rank": _optional_text(row.get("positional_rank")),
            "adp": _optional_number(row.get("adp")),
            "adp_sd": _optional_number(row.get("adp_sd")),
            "projected_points": _optional_number(row.get("projected_points")),
            "match_status": status,
            "match_method": match_method,
            "raw_payload": raw,
        }
        entries.append(entry)
        if status != "matched":
            issues.append(
                RankingIssue(
                    ranking_snapshot_id=new_snapshot_id,
                    source_row_number=source_row_number,
                    source_player_name=player_name,
                    source_position=position,
                    source_team=team,
                    match_status=status,
                    reason=reason,
                    candidate_player_ids=candidates,
                    raw_payload=raw,
                )
            )
    snapshot = original.model_copy(
        update={
            "ranking_snapshot_id": new_snapshot_id,
            "observed_at": at,
            "imported_at": datetime.now(UTC),
            "matched_row_count": sum(e["match_status"] == "matched" for e in entries),
            "unresolved_row_count": sum(e["match_status"] == "unresolved" for e in entries),
            "ambiguous_row_count": sum(e["match_status"] == "ambiguous" for e in entries),
            "resolver_version": RANKING_RESOLVER_VERSION,
            "reprocessed_from_snapshot_id": original.ranking_snapshot_id,
        }
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
