from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from fantasy_war_room.bootstrap import readiness
from fantasy_war_room.config import Settings
from fantasy_war_room.errors import ConfigurationError, FwrError, InputError
from fantasy_war_room.external_sources import (
    FFC_SOURCE,
    NFLVERSE_SOURCE,
    PortableIntelligenceProvider,
    PublicIntelligenceAdapter,
    classify_ffc_scoring,
)
from fantasy_war_room.market_imports import import_adp_frame, import_team_schedule_frame
from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import (
    IntelligenceRepository,
    _recommendation_draft_settings,
    _recommendation_league_format,
    _recommendation_roster_configuration,
    _recommendation_scoring_format,
)


@dataclass(frozen=True)
class ActiveIntelligenceContext:
    snapshot: Snapshot
    season: str
    league_size: int
    scoring_format: str
    draft_type: str
    scoring_settings: dict[str, float]


def active_intelligence_context(
    settings: Settings, repository: IntelligenceRepository
) -> ActiveIntelligenceContext:
    context = settings.active_context
    if context is None:
        raise ConfigurationError(
            "active_league_required", "Run fwr setup before bootstrapping football intelligence"
        )
    if not repository.path.expanduser().exists():
        raise ConfigurationError(
            "draft_snapshot_required", "Run fwr setup to synchronize the active league first"
        )
    repository.initialize()
    with duckdb.connect(str(repository.path)) as connection:
        row = connection.execute(
            "SELECT draft_id FROM draft_snapshots WHERE league_id=? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            [context.league_id],
        ).fetchone()
    if row is None:
        raise ConfigurationError(
            "draft_snapshot_required", "Run fwr setup to synchronize the active league first"
        )
    snapshot = repository.state_at(str(row[0]), datetime.now(UTC))
    assert snapshot is not None
    league_size, _, draft_type = _recommendation_draft_settings(snapshot)
    league_type, keeper_status = _recommendation_league_format(snapshot.scoring_context or {})
    roster = _recommendation_roster_configuration(snapshot.scoring_context or {})
    if league_type != "redraft" or keeper_status != "non_keeper" or roster.qb != 1:
        raise InputError(
            "unsupported_league_format",
            "Portable intelligence bootstrap supports NFL redraft single-QB non-keeper leagues",
        )
    scoring = (snapshot.scoring_context or {}).get("scoring_settings")
    if not isinstance(scoring, dict):
        raise InputError("incompatible_scoring_context", "Active league has no scoring settings")
    try:
        normalized = {str(key): float(value) for key, value in scoring.items()}
    except (TypeError, ValueError) as exc:
        raise InputError(
            "incompatible_scoring_context", "Active league scoring settings are not numeric"
        ) from exc
    authoritative_season = str(snapshot.draft.get("season") or "")
    if not authoritative_season or authoritative_season != context.season:
        raise InputError(
            "incompatible_season_context",
            "Active league season does not match the synchronized draft season",
            {
                "configured_season": context.season,
                "draft_season": authoritative_season or None,
            },
        )
    return ActiveIntelligenceContext(
        snapshot=snapshot,
        season=authoritative_season,
        league_size=league_size,
        scoring_format=_recommendation_scoring_format(normalized),
        draft_type=draft_type,
        scoring_settings=normalized,
    )


def bootstrap_data(
    settings: Settings,
    *,
    cache_dir: Path,
    repository_root: Path,
    force: bool = False,
    provider: PortableIntelligenceProvider | None = None,
) -> dict[str, Any]:
    repository = IntelligenceRepository(settings.db_path)
    context = active_intelligence_context(settings, repository)
    owned_provider = provider is None
    source_provider = provider or PublicIntelligenceAdapter(
        settings.http_timeout_seconds, cache_dir
    )
    results: dict[str, dict[str, Any]] = {}
    try:
        player_available = _player_directory_available(repository)
        results["player_directory"] = {
            "status": "current" if player_available else "missing",
            "provider": "sleeper",
            "message": "Canonical player directory is available"
            if player_available
            else "Run fwr setup or fwr players sync before importing player intelligence",
        }
        if player_available:
            results["adp"] = _bootstrap_adp(source_provider, repository, context, force)
        else:
            results["adp"] = {
                "status": "skipped",
                "provider": FFC_SOURCE,
                "message": "ADP skipped because the canonical player directory is missing",
            }
        results["team_schedule"] = _bootstrap_schedule(source_provider, repository, context, force)
    finally:
        if owned_provider:
            source_provider.close()
    credential_present = bool(os.environ.get("FWR_FANTASYPROS_API_KEY"))
    optional_message = (
        "FantasyPros automatic acquisition is not implemented; use the existing import command"
        if credential_present
        else "No portable provider is configured; optional FantasyPros access requires credentials"
    )
    results["rankings"] = {
        "status": "provider_not_configured",
        "automatic": False,
        "credential": "present" if credential_present else "absent",
        "message": optional_message,
    }
    results["projections"] = dict(results["rankings"])
    ready = readiness(settings, repository_root=repository_root)
    return {
        "schema_version": "1.0",
        "active_league_id": settings.active_league_id,
        "draft_id": context.snapshot.draft_id,
        "season": context.season,
        "format": {
            "league_size": context.league_size,
            "scoring_format": context.scoring_format,
            "draft_type": context.draft_type,
        },
        "sources": results,
        "recommendation_ready": ready["ready"],
        "readiness_checks": ready["checks"],
    }


def _bootstrap_adp(
    provider: PortableIntelligenceProvider,
    repository: IntelligenceRepository,
    context: ActiveIntelligenceContext,
    force: bool,
) -> dict[str, Any]:
    try:
        adp_scoring_format = classify_ffc_scoring(context.scoring_settings)
        data = provider.acquire_adp(
            season=context.season,
            league_size=context.league_size,
            scoring_format=adp_scoring_format,
            draft_type=context.draft_type,
            force=force,
        )
        snapshot, created = import_adp_frame(
            data.frame,
            repository,
            original_filename="fantasy-football-calculator.json",
            source=FFC_SOURCE,
            source_version=data.source_version,
            season=context.season,
            scoring_format={
                "full_ppr": "ppr",
                "half_ppr": "half_ppr",
                "standard": "standard",
            }[adp_scoring_format],
            league_size=context.league_size,
            draft_type=context.draft_type,
            observed_at=data.fetched_at,
            source_uri=data.source_uri,
            fetched_at=data.fetched_at,
            source_payload_hash=data.payload_hash,
            transformation_version=data.transformation_version,
        )
        return {
            "status": "acquired" if created else "unchanged",
            "provider": FFC_SOURCE,
            "snapshot_id": snapshot.adp_snapshot_id,
            "source_version": snapshot.source_version,
            "from_cache": data.from_cache,
            "matched": snapshot.matched_row_count,
            "unresolved": snapshot.unresolved_row_count,
            "ambiguous": snapshot.ambiguous_row_count,
        }
    except FwrError as exc:
        return _source_failure(FFC_SOURCE, exc)


def _bootstrap_schedule(
    provider: PortableIntelligenceProvider,
    repository: IntelligenceRepository,
    context: ActiveIntelligenceContext,
    force: bool,
) -> dict[str, Any]:
    try:
        data = provider.acquire_byes(season=context.season, force=force)
        snapshot, created = import_team_schedule_frame(
            data.frame,
            repository,
            original_filename="nflverse-games.csv",
            source=NFLVERSE_SOURCE,
            source_version=data.source_version,
            season=context.season,
            observed_at=data.fetched_at,
            source_uri=data.source_uri,
            fetched_at=data.fetched_at,
            source_payload_hash=data.payload_hash,
            transformation_version=data.transformation_version,
        )
        return {
            "status": "acquired" if created else "unchanged",
            "provider": NFLVERSE_SOURCE,
            "snapshot_id": snapshot.schedule_snapshot_id,
            "source_version": snapshot.source_version,
            "from_cache": data.from_cache,
            "teams": snapshot.total_row_count,
        }
    except FwrError as exc:
        return _source_failure(NFLVERSE_SOURCE, exc)


def _source_failure(provider: str, error: FwrError) -> dict[str, Any]:
    return {
        "status": "unsupported" if isinstance(error, InputError) else "failed",
        "provider": provider,
        "error": {"code": error.code, "message": error.message, "details": error.details},
    }


def _player_directory_available(repository: IntelligenceRepository) -> bool:
    with duckdb.connect(str(repository.path)) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM player_directory_snapshots "
                "WHERE provider='sleeper' AND sport='nfl' LIMIT 1"
            ).fetchone()
            is not None
        )
