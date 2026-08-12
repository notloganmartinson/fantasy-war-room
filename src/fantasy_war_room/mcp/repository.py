from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from fantasy_war_room.database import with_database_lock_retry
from fantasy_war_room.decision.models import RecommendationInputs, RecommendationProvenance
from fantasy_war_room.errors import ConfigurationError, InputError, NotFoundError
from fantasy_war_room.models import ProjectionSnapshot, RankingSnapshot, Snapshot
from fantasy_war_room.repository import (
    MIGRATIONS,
    _canonical_hash,
    _recommendation_draft_settings,
    _recommendation_league_format,
    _recommendation_picks,
    _recommendation_projection_players,
    _recommendation_rankings,
    _recommendation_roster_configuration,
    _recommendation_scoring_format,
    _resolve_recommendation_draft_slot,
    _select_recommendation_draft,
    _snapshot_from_row,
)


class McpReadRepository:
    """Short-lived, retrying, read-only access to one DuckDB database."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def read(
        self,
        at: datetime | None,
        *,
        draft_id: str,
        sleeper_user_id: str | None,
        draft_slot: int | None,
        ranking_source: str,
        projection_source: str = "cbs",
        sleep: Callable[[float], None] | None = None,
    ) -> tuple[RecommendationInputs, Snapshot]:
        if not self.path.exists():
            raise NotFoundError(
                "Fantasy War Room database does not exist",
                {"database": str(self.path)},
                code="database_not_found",
            )

        def operation() -> tuple[RecommendationInputs, Snapshot]:
            connection: duckdb.DuckDBPyConnection | None = None
            try:
                connection = duckdb.connect(str(self.path), read_only=True)
                connection.begin()
                _validate_schema(connection)
                decision_at = at or datetime.now(UTC)
                inputs, snapshot = _inputs_from_connection(
                    connection,
                    decision_at,
                    draft_id=draft_id,
                    sleeper_user_id=sleeper_user_id,
                    draft_slot=draft_slot,
                    ranking_source=ranking_source,
                    projection_source=projection_source,
                )
                connection.commit()
                return inputs, snapshot
            except Exception:
                if connection is not None:
                    with suppress(Exception):
                        connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()

        if sleep is None:
            return with_database_lock_retry(operation)
        return with_database_lock_retry(operation, sleep=sleep)


def _validate_schema(connection: duckdb.DuckDBPyConnection) -> None:
    try:
        row = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
    except duckdb.Error as error:
        raise ConfigurationError(
            "database_schema_incompatible",
            "Fantasy War Room database is not initialized",
        ) from error
    expected = MIGRATIONS[-1][0]
    actual = int(row[0]) if row is not None and row[0] is not None else 0
    if actual != expected:
        raise ConfigurationError(
            "database_schema_incompatible",
            "Fantasy War Room database migrations are not current",
            {"expected_version": expected, "actual_version": actual},
        )


def _inputs_from_connection(
    connection: duckdb.DuckDBPyConnection,
    at: datetime,
    *,
    draft_id: str,
    sleeper_user_id: str | None,
    draft_slot: int | None,
    ranking_source: str,
    projection_source: str,
) -> tuple[RecommendationInputs, Snapshot]:
    draft_row = _select_recommendation_draft(connection, at, draft_id, None)
    draft_snapshot = _snapshot_from_row(draft_row)
    scoring_context = draft_snapshot.scoring_context
    if scoring_context is None or draft_snapshot.scoring_context_league_id is None:
        raise ConfigurationError(
            "mock_scoring_context_required",
            "Standalone draft has no explicit league scoring context",
            {"draft_id": draft_snapshot.draft_id},
        )
    scoring = scoring_context.get("scoring_settings")
    if not isinstance(scoring, dict):
        raise InputError(
            "incompatible_scoring_context",
            "Selected draft scoring context has no scoring settings",
            {"draft_id": draft_snapshot.draft_id},
        )
    try:
        normalized_scoring = {str(key): float(value) for key, value in scoring.items()}
    except (TypeError, ValueError) as error:
        raise InputError(
            "incompatible_scoring_context",
            "Selected draft scoring settings contain a nonnumeric value",
            {"draft_id": draft_snapshot.draft_id},
        ) from error
    scoring_hash = _canonical_hash(normalized_scoring)
    season = str(draft_snapshot.draft.get("season") or "")
    if not season:
        raise InputError(
            "incompatible_scoring_context",
            "Selected draft has no season",
            {"draft_id": draft_snapshot.draft_id},
        )
    player_row = connection.execute(
        "SELECT snapshot_id, observed_at, fetched_at FROM player_directory_snapshots "
        "WHERE provider = 'sleeper' AND sport = 'nfl' AND observed_at <= ? "
        "AND fetched_at <= ? ORDER BY observed_at DESC, fetched_at DESC, snapshot_id DESC LIMIT 1",
        [at, at],
    ).fetchone()
    if player_row is None:
        raise NotFoundError(
            "No eligible player-directory snapshot exists as of the decision time",
            {"as_of": at.isoformat()},
            code="missing_player_snapshot",
        )
    ranking_row = connection.execute(
        "SELECT * FROM ranking_snapshots WHERE observed_at <= ? AND imported_at <= ? "
        "AND season = ? AND source = ? "
        "ORDER BY observed_at DESC, imported_at DESC, ranking_snapshot_id DESC LIMIT 1",
        [at, at, season, ranking_source],
    ).fetchone()
    if ranking_row is None:
        raise NotFoundError(
            "No eligible ranking snapshot exists for the selected source",
            {"as_of": at.isoformat(), "source": ranking_source, "season": season},
            code="missing_ranking_snapshot",
        )
    ranking = RankingSnapshot.from_row(ranking_row)
    projection_row = connection.execute(
        "SELECT * FROM projection_snapshots WHERE observed_at <= ? AND imported_at <= ? "
        "AND season = ? AND source = ? AND scoring_settings_hash = ? "
        "ORDER BY observed_at DESC, imported_at DESC, projection_snapshot_id DESC LIMIT 1",
        [at, at, season, projection_source, scoring_hash],
    ).fetchone()
    if projection_row is None:
        incompatible = connection.execute(
            "SELECT scoring_settings_hash FROM projection_snapshots "
            "WHERE observed_at <= ? AND imported_at <= ? AND season = ? AND source = ? "
            "ORDER BY observed_at DESC, imported_at DESC LIMIT 1",
            [at, at, season, projection_source],
        ).fetchone()
        if incompatible is not None:
            raise InputError(
                "incompatible_scoring_context",
                "Projection scoring settings do not match the selected draft context",
                {
                    "draft_scoring_settings_hash": scoring_hash,
                    "projection_scoring_settings_hash": str(incompatible[0]),
                },
            )
        raise NotFoundError(
            "No compatible projection snapshot exists as of the decision time",
            {"as_of": at.isoformat(), "source": projection_source, "season": season},
            code="missing_projection_snapshot",
        )
    projection = ProjectionSnapshot.from_row(projection_row)
    resolved_slot = _resolve_recommendation_draft_slot(draft_snapshot, draft_slot, sleeper_user_id)
    roster = _recommendation_roster_configuration(scoring_context)
    team_count, rounds, draft_type = _recommendation_draft_settings(draft_snapshot)
    league_type, keeper_status = _recommendation_league_format(scoring_context)
    scoring_format = _recommendation_scoring_format(normalized_scoring)
    provider_rows = connection.execute(
        "SELECT canonical_player_id, provider_player_id FROM player_provider_ids "
        "WHERE provider = 'sleeper' AND first_observed_at <= ? ORDER BY provider_player_id",
        [player_row[1]],
    ).fetchall()
    sleeper_to_canonical = {
        str(provider_id): str(canonical_id) for canonical_id, provider_id in provider_rows
    }
    canonical_to_sleepers: dict[str, list[str]] = {}
    for provider_id, canonical_id in sleeper_to_canonical.items():
        canonical_to_sleepers.setdefault(canonical_id, []).append(provider_id)
    completed, unresolved_roster = _recommendation_picks(
        draft_snapshot, resolved_slot, sleeper_to_canonical
    )
    projected_players = _recommendation_projection_players(
        connection, projection.projection_snapshot_id, canonical_to_sleepers
    )
    rankings = _recommendation_rankings(connection, ranking.ranking_snapshot_id)
    inputs = RecommendationInputs(
        decision_at=at,
        team_count=team_count,
        draft_type=draft_type,
        draft_rounds=rounds,
        draft_slot=resolved_slot,
        roster=roster,
        completed_picks=completed,
        projected_players=projected_players,
        expert_rankings=rankings,
        unresolved_roster_player_ids=unresolved_roster,
        sport="nfl",
        league_type=league_type,
        keeper_status=keeper_status,
        scoring_format=scoring_format,
        provenance=RecommendationProvenance(
            draft_snapshot_id=draft_snapshot.snapshot_id,
            player_snapshot_id=str(player_row[0]),
            ranking_snapshot_id=ranking.ranking_snapshot_id,
            projection_snapshot_id=projection.projection_snapshot_id,
            ranking_source=ranking.source,
            ranking_source_version=ranking.source_version,
            projection_source=projection.source,
            projection_source_version=projection.source_version,
            ranking_resolver_version=ranking.resolver_version,
            scoring_calculator_version=projection.scoring_calculator_version,
            scoring_settings_hash=projection.scoring_settings_hash,
            draft_observed_at=draft_snapshot.observed_at,
            player_observed_at=player_row[1],
            player_fetched_at=player_row[2],
            ranking_observed_at=ranking.observed_at,
            ranking_imported_at=ranking.imported_at,
            projection_observed_at=projection.observed_at,
            projection_imported_at=projection.imported_at,
            projection_player_snapshot_id=projection.player_snapshot_id,
            projection_league_snapshot_id=projection.league_snapshot_id,
            scoring_context_league_id=draft_snapshot.scoring_context_league_id,
        ),
    )
    return inputs, draft_snapshot
