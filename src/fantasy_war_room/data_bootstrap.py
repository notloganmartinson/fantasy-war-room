from __future__ import annotations

from contextlib import suppress
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
from fantasy_war_room.market_board import derive_market_board
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
    optional_message = (
        "Optional FantasyPros acquisition is not implemented; use compatible manual imports"
    )
    results["rankings"] = {
        "status": "provider_not_configured",
        "credential": "absent",
        "automatic": False,
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


def data_status(settings: Settings, *, repository_root: Path) -> dict[str, Any]:
    repository = IntelligenceRepository(settings.db_path)
    context = active_intelligence_context(settings, repository)
    now = datetime.now(UTC)
    scoring = None
    with suppress(InputError, KeyError):
        scoring = {
            "full_ppr": "ppr",
            "half_ppr": "half_ppr",
            "standard": "standard",
        }[classify_ffc_scoring(context.scoring_settings)]
    with duckdb.connect(str(repository.path), read_only=True) as connection:
        player = connection.execute(
            "SELECT snapshot_id, observed_at, fetched_at, schema_version "
            "FROM player_directory_snapshots "
            "WHERE provider='sleeper' AND sport='nfl' "
            "ORDER BY observed_at DESC, fetched_at DESC LIMIT 1"
        ).fetchone()
        adp = board = None
        if scoring is not None:
            adp = connection.execute(
                "SELECT adp_snapshot_id, source, source_version, observed_at, fetched_at, "
                "imported_at FROM adp_snapshots WHERE season=? AND league_size=? "
                "AND scoring_format=? AND draft_type=? ORDER BY observed_at DESC, imported_at DESC "
                "LIMIT 1",
                [context.season, context.league_size, scoring, context.draft_type],
            ).fetchone()
            board = connection.execute(
                "SELECT market_board_snapshot_id, source, source_version, observed_at, fetched_at, "
                "imported_at FROM market_board_snapshots WHERE season=? AND league_size=? "
                "AND scoring_format=? AND draft_type=? "
                "AND source='fantasy-football-calculator-market-board' "
                "AND transformation_version='ffc-adp-to-market-board-1.0' "
                "ORDER BY observed_at DESC, imported_at DESC "
                "LIMIT 1",
                [context.season, context.league_size, scoring, context.draft_type],
            ).fetchone()
        schedule = connection.execute(
            "SELECT schedule_snapshot_id, source, source_version, observed_at, fetched_at, "
            "imported_at FROM team_schedule_snapshots WHERE season=? "
            "ORDER BY observed_at DESC, imported_at DESC LIMIT 1",
            [context.season],
        ).fetchone()

    def source_status(
        row: tuple[Any, ...] | None, provider: str, *, configured: bool = True
    ) -> dict[str, Any]:
        if row is None:
            return {
                "provider": provider,
                "state": "missing" if configured else "manual",
                "configured": configured,
                "compatible": False,
                "latest_compatible_snapshot": None,
            }
        observed = row[3]
        return {
            "provider": row[1],
            "source_version": row[2],
            "state": "available",
            "configured": configured,
            "compatible": True,
            "latest_compatible_snapshot": row[0],
            "observed_at": observed,
            "fetched_at": row[4],
            "imported_at": row[5],
            "age_seconds": max(0.0, (now - observed).total_seconds()),
        }

    player_status = {
        "provider": "sleeper",
        "source_version": str(player[3]) if player else None,
        "state": "available" if player else "missing",
        "configured": True,
        "compatible": bool(player),
        "latest_compatible_snapshot": player[0] if player else None,
        "observed_at": player[1] if player else None,
        "fetched_at": player[2] if player else None,
        "age_seconds": max(0.0, (now - player[1]).total_seconds()) if player else None,
    }
    configured_readiness = readiness(settings, repository_root=repository_root)
    active = settings.active_context
    assert active is not None
    portable_context = active.model_copy(
        update={
            "recommendation_model": "portable-market-1.0",
            "ranking_source": None,
            "strategy": None,
        }
    )
    portable_settings = settings.model_copy(
        update={"league_contexts": {**settings.league_contexts, active.league_id: portable_context}}
    )
    portable_readiness = readiness(portable_settings, repository_root=repository_root)
    return {
        "schema_version": "1.0",
        "active_league_id": active.league_id,
        "format": {
            "season": context.season,
            "league_size": context.league_size,
            "scoring_format": context.scoring_format,
            "draft_type": context.draft_type,
        },
        "sources": {
            "player_directory": player_status,
            "ffc_adp": source_status(adp, FFC_SOURCE),
            "portable_market_board": source_status(
                board, "fantasy-football-calculator-market-board"
            ),
            "team_schedule": source_status(schedule, NFLVERSE_SOURCE),
            "custom_rankings": {
                "provider": "manual",
                "state": "manual",
                "configured": bool(active.ranking_source),
                "compatible": None,
            },
            "custom_projections": {
                "provider": "manual",
                "state": "manual",
                "configured": active.recommendation_model != "portable-market-1.0",
                "compatible": None,
            },
        },
        "model_readiness": {
            "portable-market-1.0": {
                "status": "READY" if portable_readiness["ready"] else "NOT READY",
                "ready": portable_readiness["ready"],
                "checks": portable_readiness["checks"],
            },
            "configured": {
                "model": configured_readiness.get("recommendation_model"),
                "status": "READY" if configured_readiness["ready"] else "NOT READY",
                "ready": configured_readiness["ready"],
                "checks": configured_readiness["checks"],
            },
        },
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
        board, board_created = derive_market_board(repository, snapshot.adp_snapshot_id)
        return {
            "status": "acquired" if created else "unchanged",
            "provider": FFC_SOURCE,
            "snapshot_id": snapshot.adp_snapshot_id,
            "source_version": snapshot.source_version,
            "from_cache": data.from_cache,
            "matched": snapshot.matched_row_count,
            "unresolved": snapshot.unresolved_row_count,
            "ambiguous": snapshot.ambiguous_row_count,
            "market_board": {
                "status": "derived" if board_created else "unchanged",
                "source": board.source,
                "snapshot_id": board.market_board_snapshot_id,
                "transformation_version": board.transformation_version,
                "derived_from_adp_snapshot_id": board.derived_from_adp_snapshot_id,
            },
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
