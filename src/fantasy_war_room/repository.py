from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

import duckdb
import polars as pl

from fantasy_war_room.decision.models import (
    CompletedDraftPick,
    ExpertRankingInput,
    OffensivePosition,
    RecommendationInputs,
    RecommendationPlayerInput,
    RecommendationProvenance,
    RosterConfiguration,
)
from fantasy_war_room.errors import (
    ConfigurationError,
    DataIntegrityError,
    InputError,
    NotFoundError,
)
from fantasy_war_room.identity import (
    alias_targets,
    normalize_name,
    strict_name,
    suffix_insensitive_name,
)
from fantasy_war_room.models import (
    AdpIssue,
    AdpSnapshot,
    BoardPlayer,
    PlayerDirectorySnapshot,
    PlayerSearchResult,
    ProjectionIssue,
    ProjectionSnapshot,
    RankingIssue,
    RankingSnapshot,
    Snapshot,
    TeamScheduleSnapshot,
)

MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS draft_snapshots (
 snapshot_id VARCHAR PRIMARY KEY, league_id VARCHAR NOT NULL, draft_id VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, source_updated_at TIMESTAMPTZ,
 payload_hash VARCHAR NOT NULL, pick_count INTEGER NOT NULL,
 league_payload JSON NOT NULL, draft_payload JSON NOT NULL, picks_payload JSON NOT NULL,
 UNIQUE(draft_id, payload_hash)
);
CREATE TABLE IF NOT EXISTS draft_snapshot_picks (
 snapshot_id VARCHAR NOT NULL, draft_id VARCHAR NOT NULL, pick_no INTEGER NOT NULL,
 round INTEGER, draft_slot INTEGER, roster_id VARCHAR, picked_by VARCHAR,
 player_id VARCHAR, player_name VARCHAR, position VARCHAR, team VARCHAR,
 raw_payload JSON NOT NULL, PRIMARY KEY(snapshot_id, pick_no)
);
"""

MIGRATION_2 = """
CREATE TABLE draft_snapshots_v2 (
 snapshot_id VARCHAR PRIMARY KEY, league_id VARCHAR NOT NULL, draft_id VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, source_updated_at TIMESTAMPTZ,
 payload_hash VARCHAR NOT NULL, pick_count INTEGER NOT NULL,
 league_payload JSON NOT NULL, draft_payload JSON NOT NULL, picks_payload JSON NOT NULL
);
INSERT INTO draft_snapshots_v2 SELECT * FROM draft_snapshots;
DROP TABLE draft_snapshots;
ALTER TABLE draft_snapshots_v2 RENAME TO draft_snapshots;
"""

MIGRATION_3 = """
ALTER TABLE schema_migrations ADD COLUMN name VARCHAR;
UPDATE schema_migrations SET name = 'initial_m1_schema' WHERE version = 1;
UPDATE schema_migrations SET name = 'repeatable_draft_states' WHERE version = 2;
CREATE TABLE player_directory_snapshots (
 snapshot_id VARCHAR PRIMARY KEY, provider VARCHAR NOT NULL, sport VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, fetched_at TIMESTAMPTZ NOT NULL,
 payload_hash VARCHAR NOT NULL, player_count INTEGER NOT NULL, raw_cache_path VARCHAR NOT NULL,
 schema_version VARCHAR NOT NULL
);
CREATE TABLE canonical_players (
 canonical_player_id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE player_provider_ids (
 canonical_player_id VARCHAR NOT NULL, provider VARCHAR NOT NULL,
 provider_player_id VARCHAR NOT NULL, first_observed_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(provider, provider_player_id), UNIQUE(canonical_player_id, provider)
);
CREATE TABLE player_observations (
 snapshot_id VARCHAR NOT NULL, canonical_player_id VARCHAR NOT NULL,
 provider_player_id VARCHAR NOT NULL, first_name VARCHAR NOT NULL, last_name VARCHAR NOT NULL,
 normalized_full_name VARCHAR NOT NULL, position VARCHAR, fantasy_positions JSON NOT NULL,
 team VARCHAR, active BOOLEAN, status VARCHAR, injury_status VARCHAR,
 years_experience DOUBLE, provider_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(snapshot_id, canonical_player_id)
);
CREATE TABLE ranking_snapshots (
 ranking_snapshot_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, source_version VARCHAR,
 season VARCHAR NOT NULL, scoring_format VARCHAR NOT NULL, league_size INTEGER NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, imported_at TIMESTAMPTZ NOT NULL,
 payload_hash VARCHAR NOT NULL, original_filename VARCHAR NOT NULL,
 total_row_count INTEGER NOT NULL, matched_row_count INTEGER NOT NULL,
 unresolved_row_count INTEGER NOT NULL, ambiguous_row_count INTEGER NOT NULL,
 schema_version VARCHAR NOT NULL
);
CREATE TABLE ranking_entries (
 ranking_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 canonical_player_id VARCHAR, source_player_name VARCHAR NOT NULL,
 source_position VARCHAR, source_team VARCHAR, overall_rank DOUBLE,
 positional_rank VARCHAR, adp DOUBLE, adp_sd DOUBLE, projected_points DOUBLE,
 match_status VARCHAR NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(ranking_snapshot_id, source_row_number)
);
CREATE TABLE ranking_match_issues (
 ranking_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 match_status VARCHAR NOT NULL, reason VARCHAR NOT NULL,
 candidate_player_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(ranking_snapshot_id, source_row_number)
);
"""

MIGRATION_4 = """
CREATE TABLE player_observations_v4 (
 snapshot_id VARCHAR NOT NULL, canonical_player_id VARCHAR NOT NULL,
 provider_player_id VARCHAR NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
 first_name VARCHAR NOT NULL, last_name VARCHAR NOT NULL,
 normalized_full_name VARCHAR NOT NULL, position VARCHAR, fantasy_positions JSON NOT NULL,
 team VARCHAR, active BOOLEAN, status VARCHAR, injury_status VARCHAR,
 years_experience DOUBLE, provider_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(snapshot_id, canonical_player_id)
);
INSERT INTO player_observations_v4 (
 snapshot_id, canonical_player_id, provider_player_id, observed_at,
 first_name, last_name, normalized_full_name, position, fantasy_positions,
 team, active, status, injury_status, years_experience, provider_ids, raw_payload
)
SELECT o.snapshot_id, o.canonical_player_id, o.provider_player_id, s.observed_at,
 o.first_name, o.last_name, o.normalized_full_name, o.position, o.fantasy_positions,
 o.team, o.active, o.status, o.injury_status, o.years_experience, o.provider_ids, o.raw_payload
FROM player_observations o
JOIN player_directory_snapshots s ON s.snapshot_id = o.snapshot_id;
DROP TABLE player_observations;
ALTER TABLE player_observations_v4 RENAME TO player_observations;
CREATE TABLE ranking_match_issues_v4 (
 ranking_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 source_player_name VARCHAR NOT NULL, source_position VARCHAR, source_team VARCHAR,
 match_status VARCHAR NOT NULL, reason VARCHAR NOT NULL,
 candidate_player_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(ranking_snapshot_id, source_row_number)
);
INSERT INTO ranking_match_issues_v4 (
 ranking_snapshot_id, source_row_number, source_player_name, source_position, source_team,
 match_status, reason, candidate_player_ids, raw_payload
)
SELECT i.ranking_snapshot_id, i.source_row_number, e.source_player_name,
 e.source_position, e.source_team, i.match_status, i.reason,
 i.candidate_player_ids, i.raw_payload
FROM ranking_match_issues i
JOIN ranking_entries e ON e.ranking_snapshot_id = i.ranking_snapshot_id
 AND e.source_row_number = i.source_row_number;
DROP TABLE ranking_match_issues;
ALTER TABLE ranking_match_issues_v4 RENAME TO ranking_match_issues;
"""

MIGRATION_5 = """
CREATE TABLE player_provider_ids_v5 (
 canonical_player_id VARCHAR NOT NULL, provider VARCHAR NOT NULL,
 provider_player_id VARCHAR NOT NULL, first_observed_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(provider, provider_player_id)
);
INSERT INTO player_provider_ids_v5 (
 canonical_player_id, provider, provider_player_id, first_observed_at
)
SELECT canonical_player_id, provider, provider_player_id, first_observed_at
FROM player_provider_ids;
DROP TABLE player_provider_ids;
ALTER TABLE player_provider_ids_v5 RENAME TO player_provider_ids;
CREATE TABLE player_observations_v5 (
 snapshot_id VARCHAR NOT NULL, canonical_player_id VARCHAR NOT NULL,
 provider VARCHAR NOT NULL, provider_player_id VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, first_name VARCHAR NOT NULL, last_name VARCHAR NOT NULL,
 normalized_full_name VARCHAR NOT NULL, position VARCHAR, fantasy_positions JSON NOT NULL,
 team VARCHAR, active BOOLEAN, status VARCHAR, injury_status VARCHAR,
 years_experience DOUBLE, provider_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(snapshot_id, provider, provider_player_id)
);
INSERT INTO player_observations_v5 (
 snapshot_id, canonical_player_id, provider, provider_player_id, observed_at,
 first_name, last_name, normalized_full_name, position, fantasy_positions,
 team, active, status, injury_status, years_experience, provider_ids, raw_payload
)
SELECT snapshot_id, canonical_player_id, 'sleeper', provider_player_id, observed_at,
 first_name, last_name, normalized_full_name, position, fantasy_positions,
 team, active, status, injury_status, years_experience, provider_ids, raw_payload
FROM player_observations;
DROP TABLE player_observations;
ALTER TABLE player_observations_v5 RENAME TO player_observations;
"""

MIGRATION_6 = """
ALTER TABLE ranking_snapshots ADD COLUMN resolver_version VARCHAR;
ALTER TABLE ranking_snapshots ADD COLUMN reprocessed_from_snapshot_id VARCHAR;
UPDATE ranking_snapshots SET resolver_version = '1.0' WHERE resolver_version IS NULL;
ALTER TABLE ranking_entries ADD COLUMN match_method VARCHAR;
UPDATE ranking_entries SET match_method = CASE
 WHEN match_status = 'matched' THEN 'legacy' ELSE NULL END;
"""

MIGRATION_7 = """
CREATE TABLE projection_snapshots (
 projection_snapshot_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL,
 source_version VARCHAR NOT NULL, season VARCHAR NOT NULL, horizon VARCHAR NOT NULL,
 source_scoring_format VARCHAR NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
 imported_at TIMESTAMPTZ NOT NULL, payload_hash VARCHAR NOT NULL,
 total_row_count INTEGER NOT NULL, matched_row_count INTEGER NOT NULL,
 unresolved_row_count INTEGER NOT NULL, ambiguous_row_count INTEGER NOT NULL,
 player_snapshot_id VARCHAR NOT NULL, league_snapshot_id VARCHAR NOT NULL,
 scoring_settings_hash VARCHAR NOT NULL, scoring_settings JSON NOT NULL,
 scoring_calculator_version VARCHAR NOT NULL, schema_version VARCHAR NOT NULL
);
CREATE TABLE projection_snapshot_sources (
 projection_snapshot_id VARCHAR NOT NULL, position VARCHAR NOT NULL,
 original_filename VARCHAR NOT NULL, source_page_hash VARCHAR NOT NULL,
 row_count INTEGER NOT NULL, published_columns JSON NOT NULL,
 PRIMARY KEY(projection_snapshot_id, position)
);
CREATE TABLE projection_entries (
 projection_snapshot_id VARCHAR NOT NULL, source_position VARCHAR NOT NULL,
 source_row_number INTEGER NOT NULL, canonical_player_id VARCHAR,
 source_player_name VARCHAR NOT NULL, position VARCHAR NOT NULL, team VARCHAR,
 games DOUBLE, passing_attempts DOUBLE, passing_completions DOUBLE,
 passing_yards DOUBLE, passing_yards_per_game DOUBLE, passing_touchdowns DOUBLE,
 interceptions DOUBLE, passer_rating DOUBLE, rushing_attempts DOUBLE,
 rushing_yards DOUBLE, rushing_yards_per_attempt DOUBLE, rushing_touchdowns DOUBLE,
 targets DOUBLE, receptions DOUBLE, receiving_yards DOUBLE,
 receiving_yards_per_game DOUBLE, receiving_yards_per_reception DOUBLE,
 receiving_touchdowns DOUBLE, fumbles_lost DOUBLE,
 cbs_projected_points DOUBLE, cbs_projected_points_per_game DOUBLE,
 league_known_component_points DOUBLE, league_projected_points DOUBLE,
 scoring_completeness VARCHAR NOT NULL, unprojected_scoring_keys JSON NOT NULL,
 match_status VARCHAR NOT NULL, match_method VARCHAR, raw_payload JSON NOT NULL,
 schema_version VARCHAR NOT NULL,
 PRIMARY KEY(projection_snapshot_id, source_position, source_row_number)
);
CREATE TABLE projection_kicker_stats (
 projection_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 field_goals_made DOUBLE, field_goals_attempted DOUBLE, longest_field_goal DOUBLE,
 field_goals_made_1_19 DOUBLE, field_goals_attempted_1_19 DOUBLE,
 field_goals_made_20_29 DOUBLE, field_goals_attempted_20_29 DOUBLE,
 field_goals_made_30_39 DOUBLE, field_goals_attempted_30_39 DOUBLE,
 field_goals_made_40_49 DOUBLE, field_goals_attempted_40_49 DOUBLE,
 field_goals_made_50_plus DOUBLE, field_goals_attempted_50_plus DOUBLE,
 extra_points_made DOUBLE, extra_points_attempted DOUBLE,
 PRIMARY KEY(projection_snapshot_id, source_row_number)
);
CREATE TABLE projection_dst_stats (
 projection_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 defensive_interceptions DOUBLE, safeties DOUBLE, sacks DOUBLE, tackles DOUBLE,
 defensive_fumbles_recovered DOUBLE, forced_fumbles DOUBLE,
 defensive_touchdowns DOUBLE, points_allowed DOUBLE, points_allowed_per_game DOUBLE,
 passing_yards_allowed DOUBLE, rushing_yards_allowed DOUBLE,
 total_yards_allowed DOUBLE, yards_allowed_per_game DOUBLE,
 PRIMARY KEY(projection_snapshot_id, source_row_number)
);
CREATE TABLE projection_match_issues (
 projection_snapshot_id VARCHAR NOT NULL, source_position VARCHAR NOT NULL,
 source_row_number INTEGER NOT NULL, source_player_name VARCHAR NOT NULL,
 source_team VARCHAR, match_status VARCHAR NOT NULL, reason VARCHAR NOT NULL,
 candidate_player_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 schema_version VARCHAR NOT NULL,
 PRIMARY KEY(projection_snapshot_id, source_position, source_row_number)
);
"""

MIGRATION_8 = """
ALTER TABLE draft_snapshots ALTER COLUMN league_id DROP NOT NULL;
ALTER TABLE draft_snapshots ADD COLUMN source_league_id VARCHAR;
ALTER TABLE draft_snapshots ADD COLUMN scoring_context_league_id VARCHAR;
ALTER TABLE draft_snapshots ADD COLUMN scoring_context_payload JSON;
ALTER TABLE draft_snapshots ADD COLUMN draft_context_type VARCHAR;
UPDATE draft_snapshots SET source_league_id = league_id,
 scoring_context_league_id = league_id, scoring_context_payload = league_payload,
 draft_context_type = 'league';
"""

MIGRATION_9 = """
CREATE TABLE adp_snapshots (
 adp_snapshot_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL,
 source_version VARCHAR NOT NULL, season VARCHAR NOT NULL, league_size INTEGER NOT NULL,
 scoring_format VARCHAR NOT NULL, draft_type VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, imported_at TIMESTAMPTZ NOT NULL,
 payload_hash VARCHAR NOT NULL, identity_resolver_version VARCHAR NOT NULL,
 original_filename VARCHAR NOT NULL, total_row_count INTEGER NOT NULL,
 matched_row_count INTEGER NOT NULL, unresolved_row_count INTEGER NOT NULL,
 ambiguous_row_count INTEGER NOT NULL, schema_version VARCHAR NOT NULL
);
CREATE TABLE adp_entries (
 adp_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 canonical_player_id VARCHAR, source_player_name VARCHAR NOT NULL,
 source_position VARCHAR, source_team VARCHAR, overall_adp DOUBLE NOT NULL,
 adp_sd DOUBLE, sample_size INTEGER, match_status VARCHAR NOT NULL,
 match_method VARCHAR, raw_payload JSON NOT NULL,
 PRIMARY KEY(adp_snapshot_id, source_row_number)
);
CREATE TABLE adp_match_issues (
 adp_snapshot_id VARCHAR NOT NULL, source_row_number INTEGER NOT NULL,
 source_player_name VARCHAR NOT NULL, source_position VARCHAR, source_team VARCHAR,
 match_status VARCHAR NOT NULL, reason VARCHAR NOT NULL,
 candidate_player_ids JSON NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(adp_snapshot_id, source_row_number)
);
"""

MIGRATION_10 = """
CREATE TABLE team_schedule_snapshots (
 schedule_snapshot_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL,
 source_version VARCHAR NOT NULL, season VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, imported_at TIMESTAMPTZ NOT NULL,
 payload_hash VARCHAR NOT NULL, original_filename VARCHAR NOT NULL,
 total_row_count INTEGER NOT NULL, schema_version VARCHAR NOT NULL
);
CREATE TABLE team_schedule_entries (
 schedule_snapshot_id VARCHAR NOT NULL, team VARCHAR NOT NULL,
 bye_week INTEGER NOT NULL, raw_payload JSON NOT NULL,
 PRIMARY KEY(schedule_snapshot_id, team)
);
"""

MIGRATIONS = (
    (1, "initial_m1_schema", MIGRATION_1),
    (2, "repeatable_draft_states", MIGRATION_2),
    (3, "m2_player_intelligence", MIGRATION_3),
    (4, "m2_observation_schema_alignment", MIGRATION_4),
    (5, "m2_player_source_observations", MIGRATION_5),
    (6, "m2_ranking_resolution_provenance", MIGRATION_6),
    (7, "projection_intelligence", MIGRATION_7),
    (8, "standalone_draft_context", MIGRATION_8),
    (9, "immutable_adp_intelligence", MIGRATION_9),
    (10, "team_schedule_intelligence", MIGRATION_10),
)


class MigrationError(RuntimeError):
    """Raised when persisted data cannot be migrated without loss."""


class SnapshotRepository:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                legacy_schema = connection.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_name = 'draft_snapshots'"
                ).fetchone()
                applied = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall()
                }
                if legacy_schema and 1 not in applied:
                    connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
                    applied.add(1)
                for version, name, sql in MIGRATIONS:
                    if version not in applied:
                        if version == 4:
                            _preflight_migration_4(connection)
                        connection.execute(sql)
                        if version < 3:
                            connection.execute(
                                "INSERT INTO schema_migrations (version) VALUES (?)", [version]
                            )
                        else:
                            connection.execute(
                                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                                [version, name],
                            )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def latest_hash(self, draft_id: str) -> str | None:
        with duckdb.connect(str(self.path)) as connection:
            row = connection.execute(
                "SELECT payload_hash FROM draft_snapshots WHERE draft_id = ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [draft_id],
            ).fetchone()
        return str(row[0]) if row else None

    def insert(self, snapshot: Snapshot) -> bool:
        self.initialize()
        if self.latest_hash(snapshot.draft_id) == snapshot.payload_hash:
            return False
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                connection.execute(
                    "INSERT INTO draft_snapshots (snapshot_id, league_id, draft_id, observed_at, "
                    "source_updated_at, payload_hash, pick_count, league_payload, draft_payload, "
                    "picks_payload, source_league_id, scoring_context_league_id, "
                    "scoring_context_payload, draft_context_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        snapshot.snapshot_id,
                        snapshot.league_id,
                        snapshot.draft_id,
                        snapshot.observed_at,
                        snapshot.source_updated_at,
                        snapshot.payload_hash,
                        snapshot.pick_count,
                        json.dumps(snapshot.league),
                        json.dumps(snapshot.draft),
                        json.dumps(snapshot.picks),
                        snapshot.source_league_id
                        if snapshot.source_league_id is not None
                        else snapshot.league_id,
                        snapshot.scoring_context_league_id
                        if snapshot.scoring_context_league_id is not None
                        else snapshot.league_id,
                        json.dumps(snapshot.scoring_context)
                        if snapshot.scoring_context is not None
                        else (
                            json.dumps(snapshot.league)
                            if snapshot.draft_context_type == "league"
                            else None
                        ),
                        snapshot.draft_context_type,
                    ],
                )
                for index, pick in enumerate(snapshot.picks, start=1):
                    metadata = pick.get("metadata") or {}
                    connection.execute(
                        "INSERT INTO draft_snapshot_picks VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            snapshot.snapshot_id,
                            snapshot.draft_id,
                            pick.get("pick_no", index),
                            pick.get("round"),
                            pick.get("draft_slot"),
                            _text(pick.get("roster_id")),
                            _text(pick.get("picked_by")),
                            _text(pick.get("player_id")),
                            metadata.get("first_name", "")
                            + (
                                " "
                                if metadata.get("first_name") and metadata.get("last_name")
                                else ""
                            )
                            + metadata.get("last_name", ""),
                            metadata.get("position"),
                            metadata.get("team"),
                            json.dumps(pick),
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def state_at(self, draft_id: str, at: datetime) -> Snapshot | None:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            row = connection.execute(
                "SELECT * FROM draft_snapshots WHERE draft_id = ? AND observed_at <= ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [draft_id, at],
            ).fetchone()
        if row is None:
            return None
        return Snapshot(
            snapshot_id=row[0],
            league_id=row[1],
            draft_id=row[2],
            observed_at=row[3],
            source_updated_at=row[4],
            payload_hash=row[5],
            pick_count=row[6],
            league=json.loads(row[7]),
            draft=json.loads(row[8]),
            picks=json.loads(row[9]),
            source_league_id=row[10],
            scoring_context_league_id=row[11],
            scoring_context=json.loads(row[12]) if row[12] is not None else None,
            draft_context_type=row[13],
        )

    def stored_draft_ids(self) -> set[str]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute("SELECT DISTINCT draft_id FROM draft_snapshots").fetchall()
        return {str(row[0]) for row in rows}


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _preflight_migration_4(connection: duckdb.DuckDBPyConnection) -> None:
    orphaned_player_count = _count_query(
        connection,
        (
            "SELECT count(*) FROM player_observations o "
            "WHERE NOT EXISTS (SELECT 1 FROM player_directory_snapshots s "
            "WHERE s.snapshot_id = o.snapshot_id)"
        ),
    )
    orphaned_player_ids = connection.execute(
        "SELECT o.snapshot_id, o.canonical_player_id FROM player_observations o "
        "WHERE NOT EXISTS (SELECT 1 FROM player_directory_snapshots s "
        "WHERE s.snapshot_id = o.snapshot_id) "
        "ORDER BY o.snapshot_id, o.canonical_player_id LIMIT 10"
    ).fetchall()
    unmatched_issue_count = _count_query(
        connection,
        (
            "SELECT count(*) FROM ranking_match_issues i "
            "WHERE NOT EXISTS (SELECT 1 FROM ranking_entries e "
            "WHERE e.ranking_snapshot_id = i.ranking_snapshot_id "
            "AND e.source_row_number = i.source_row_number)"
        ),
    )
    unmatched_issue_ids = connection.execute(
        "SELECT i.ranking_snapshot_id, i.source_row_number FROM ranking_match_issues i "
        "WHERE NOT EXISTS (SELECT 1 FROM ranking_entries e "
        "WHERE e.ranking_snapshot_id = i.ranking_snapshot_id "
        "AND e.source_row_number = i.source_row_number) "
        "ORDER BY i.ranking_snapshot_id, i.source_row_number LIMIT 10"
    ).fetchall()
    problems: list[str] = []
    if orphaned_player_count:
        problems.append(
            "orphaned player_observations "
            f"count={orphaned_player_count}, sample(snapshot_id, canonical_player_id)="
            f"{orphaned_player_ids}"
        )
    if unmatched_issue_count:
        problems.append(
            "unmatched ranking_match_issues "
            f"count={unmatched_issue_count}, sample(ranking_snapshot_id, source_row_number)="
            f"{unmatched_issue_ids}"
        )
    if problems:
        raise MigrationError(
            "Migration 4 cannot preserve all historical rows; " + "; ".join(problems)
        )


def _count_query(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise MigrationError("Migration 4 preflight count query returned no result")
    return int(row[0])


def _records_represent_same_player(records: list[dict[str, Any]]) -> bool:
    names = {str(record["normalized_full_name"]) for record in records}
    return len(names) == 1 or "duplicate player" in names


def _safe_player_details(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "sleeper_player_id": player["provider_player_id"],
        "normalized_name": player["normalized_full_name"],
        "position": player["position"],
        "team": player["team"],
    }


def _safe_collision_details(
    duplicate_claims: dict[tuple[str, str], list[dict[str, Any]]],
    unsafe_claims: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for claim, records in duplicate_claims.items():
        sleeper_ids = tuple(sorted(str(record["provider_player_id"]) for record in records))
        canonical_ids = {str(record["_resolved_canonical_player_id"]) for record in records}
        group_key = ("|".join(sorted(canonical_ids)), sleeper_ids)
        detail = grouped.setdefault(
            group_key,
            {
                "canonical_player_ids": sorted(canonical_ids),
                "records": [_safe_player_details(record) for record in records],
                "merge_provider_ids": [],
                "resolution": "quarantined" if claim in unsafe_claims else "merged",
            },
        )
        detail["merge_provider_ids"].append({"provider": claim[0], "provider_player_id": claim[1]})
    return sorted(grouped.values(), key=lambda detail: detail["records"][0]["sleeper_player_id"])


class IntelligenceRepository(SnapshotRepository):
    def latest_player_hash(self, provider: str, sport: str) -> str | None:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            row = connection.execute(
                "SELECT payload_hash FROM player_directory_snapshots "
                "WHERE provider = ? AND sport = ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [provider, sport],
            ).fetchone()
        return str(row[0]) if row else None

    def insert_player_directory(
        self,
        snapshot: PlayerDirectorySnapshot,
        players: list[dict[str, Any]],
        timings: dict[str, float] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> bool:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                latest = connection.execute(
                    "SELECT payload_hash FROM player_directory_snapshots "
                    "WHERE provider = ? AND sport = ? "
                    "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                    [snapshot.provider, snapshot.sport],
                ).fetchone()
                if latest and str(latest[0]) == snapshot.payload_hash:
                    connection.rollback()
                    if timings is not None:
                        timings.update(identity_resolution=0.0, database_persistence=0.0)
                    return False
                identity_started = time.perf_counter()
                existing_rows = connection.execute(
                    "SELECT canonical_player_id, provider, provider_player_id "
                    "FROM player_provider_ids"
                ).fetchall()
                identity_map = {
                    (str(provider), str(provider_id)): str(canonical_id)
                    for canonical_id, provider, provider_id in existing_rows
                }
                known_canonical_ids = {str(row[0]) for row in existing_rows}
                claim_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for player in players:
                    for provider, provider_id in player["provider_ids"].items():
                        if provider != "sleeper":
                            claim_groups.setdefault((provider, provider_id), []).append(player)
                duplicate_claims = {
                    key: records for key, records in claim_groups.items() if len(records) > 1
                }
                unsafe_claims = {
                    key
                    for key, records in duplicate_claims.items()
                    if not _records_represent_same_player(records)
                }
                canonical_rows: list[dict[str, Any]] = []
                mapping_rows: list[dict[str, Any]] = []
                observation_rows: list[dict[str, Any]] = []
                for player in players:
                    provider_ids = player["provider_ids"]
                    matches = {
                        identity_map[(provider, provider_id)]
                        for provider, provider_id in provider_ids.items()
                        if (provider, provider_id) in identity_map
                        and (provider == "sleeper" or (provider, provider_id) not in unsafe_claims)
                    }
                    if len(matches) > 1:
                        details = _safe_player_details(player)
                        details["matched_canonical_player_ids"] = sorted(matches)
                        raise DataIntegrityError(
                            "Provider identifiers resolve one Sleeper record to multiple "
                            "existing canonical players",
                            {"conflict_count": 1, "collisions": [details]},
                        )
                    canonical_id = next(iter(matches), str(player["canonical_player_id"]))
                    player["_resolved_canonical_player_id"] = canonical_id
                    if canonical_id not in known_canonical_ids:
                        known_canonical_ids.add(canonical_id)
                        canonical_rows.append(
                            {
                                "canonical_player_id": canonical_id,
                                "created_at": snapshot.observed_at,
                            }
                        )
                    for provider, provider_id in provider_ids.items():
                        key = (provider, provider_id)
                        if provider != "sleeper" and key in unsafe_claims:
                            continue
                        if key not in identity_map:
                            identity_map[key] = canonical_id
                            mapping_rows.append(
                                {
                                    "canonical_player_id": canonical_id,
                                    "provider": provider,
                                    "provider_player_id": provider_id,
                                    "first_observed_at": snapshot.observed_at,
                                }
                            )
                    observation_rows.append(
                        {
                            "snapshot_id": snapshot.snapshot_id,
                            "canonical_player_id": canonical_id,
                            "provider": snapshot.provider,
                            "provider_player_id": player["provider_player_id"],
                            "observed_at": snapshot.observed_at,
                            "first_name": player["first_name"],
                            "last_name": player["last_name"],
                            "normalized_full_name": player["normalized_full_name"],
                            "position": player["position"],
                            "fantasy_positions": json.dumps(player["fantasy_positions"]),
                            "team": player["team"],
                            "active": player["active"],
                            "status": player["status"],
                            "injury_status": player["injury_status"],
                            "years_experience": player["years_experience"],
                            "provider_ids": json.dumps(provider_ids, sort_keys=True),
                            "raw_payload": json.dumps(player["raw_payload"], sort_keys=True),
                        }
                    )
                players.clear()
                collision_details = _safe_collision_details(duplicate_claims, unsafe_claims)
                if diagnostics is not None:
                    diagnostics.update(
                        collision_count=len(collision_details),
                        merged_collision_count=sum(
                            detail["resolution"] == "merged" for detail in collision_details
                        ),
                        quarantined_collision_count=sum(
                            detail["resolution"] == "quarantined" for detail in collision_details
                        ),
                        collisions=collision_details,
                    )
                identity_elapsed = time.perf_counter() - identity_started
                persistence_started = time.perf_counter()
                connection.execute(
                    "INSERT INTO player_directory_snapshots (snapshot_id, provider, sport, "
                    "observed_at, fetched_at, payload_hash, player_count, raw_cache_path, "
                    "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        snapshot.snapshot_id,
                        snapshot.provider,
                        snapshot.sport,
                        snapshot.observed_at,
                        snapshot.fetched_at,
                        snapshot.payload_hash,
                        snapshot.player_count,
                        snapshot.raw_cache_path,
                        snapshot.schema_version,
                    ],
                )
                self._bulk_insert_player_rows(
                    connection, canonical_rows, mapping_rows, observation_rows
                )
                connection.commit()
                if timings is not None:
                    timings.update(
                        identity_resolution=identity_elapsed,
                        database_persistence=time.perf_counter() - persistence_started,
                    )
            except Exception:
                connection.rollback()
                raise
        return True

    def _bulk_insert_player_rows(
        self,
        connection: duckdb.DuckDBPyConnection,
        canonical_rows: list[dict[str, Any]],
        mapping_rows: list[dict[str, Any]],
        observation_rows: list[dict[str, Any]],
    ) -> None:
        inserts = (
            (
                "canonical_batch",
                canonical_rows,
                "INSERT INTO canonical_players (canonical_player_id, created_at) "
                "SELECT canonical_player_id, created_at FROM canonical_batch",
            ),
            (
                "mapping_batch",
                mapping_rows,
                "INSERT INTO player_provider_ids (canonical_player_id, provider, "
                "provider_player_id, first_observed_at) SELECT canonical_player_id, provider, "
                "provider_player_id, first_observed_at FROM mapping_batch",
            ),
            (
                "observation_batch",
                observation_rows,
                "INSERT INTO player_observations (snapshot_id, canonical_player_id, "
                "provider, provider_player_id, observed_at, first_name, last_name, "
                "normalized_full_name, "
                "position, fantasy_positions, team, active, status, injury_status, "
                "years_experience, provider_ids, raw_payload) SELECT snapshot_id, "
                "canonical_player_id, provider, provider_player_id, observed_at, first_name, "
                "last_name, normalized_full_name, position, fantasy_positions, team, active, "
                "status, "
                "injury_status, years_experience, provider_ids, raw_payload "
                "FROM observation_batch",
            ),
        )
        for name, rows, sql in inserts:
            if not rows:
                continue
            connection.register(name, pl.DataFrame(rows))
            try:
                connection.execute(sql)
            finally:
                connection.unregister(name)

    def _insert_player_observation(
        self,
        connection: duckdb.DuckDBPyConnection,
        snapshot: PlayerDirectorySnapshot,
        player: dict[str, Any],
    ) -> None:
        provider_ids = player["provider_ids"]
        canonical_id = None
        for provider, provider_id in provider_ids.items():
            row = connection.execute(
                "SELECT canonical_player_id FROM player_provider_ids "
                "WHERE provider = ? AND provider_player_id = ?",
                [provider, provider_id],
            ).fetchone()
            if row:
                canonical_id = str(row[0])
                break
        canonical_id = canonical_id or str(player["canonical_player_id"])
        connection.execute(
            "INSERT INTO canonical_players VALUES (?, ?) ON CONFLICT DO NOTHING",
            [canonical_id, snapshot.observed_at],
        )
        for provider, provider_id in provider_ids.items():
            connection.execute(
                "INSERT INTO player_provider_ids VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [canonical_id, provider, provider_id, snapshot.observed_at],
            )
        connection.execute(
            "INSERT INTO player_observations ("
            "snapshot_id, canonical_player_id, provider_player_id, observed_at, "
            "first_name, last_name, normalized_full_name, position, fantasy_positions, "
            "team, active, status, injury_status, years_experience, provider_ids, raw_payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                snapshot.snapshot_id,
                canonical_id,
                player["provider_player_id"],
                snapshot.observed_at,
                player["first_name"],
                player["last_name"],
                player["normalized_full_name"],
                player["position"],
                json.dumps(player["fantasy_positions"]),
                player["team"],
                player["active"],
                player["status"],
                player["injury_status"],
                player["years_experience"],
                json.dumps(provider_ids, sort_keys=True),
                json.dumps(player["raw_payload"], sort_keys=True),
            ],
        )

    def search_players(
        self,
        query: str,
        at: datetime,
        position: str | None = None,
        team: str | None = None,
    ) -> list[PlayerSearchResult]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "WITH selected AS (SELECT snapshot_id, observed_at FROM "
                "player_directory_snapshots WHERE provider = 'sleeper' AND sport = 'nfl' "
                "AND observed_at <= ? ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1) "
                "SELECT o.canonical_player_id, o.provider_player_id, o.first_name, o.last_name, "
                "o.normalized_full_name, o.position, o.team, o.active, o.injury_status, "
                "s.snapshot_id, s.observed_at FROM selected s JOIN player_observations o "
                "ON o.snapshot_id = s.snapshot_id WHERE o.normalized_full_name LIKE ? "
                "AND (? IS NULL OR upper(o.position) = upper(?)) "
                "AND (? IS NULL OR upper(o.team) = upper(?)) "
                "ORDER BY o.normalized_full_name, o.canonical_player_id",
                [at, f"%{query}%", position, position, team, team],
            ).fetchall()
        return [PlayerSearchResult.from_row(row) for row in rows]

    def resolve_player(
        self, row: dict[str, Any], at: datetime
    ) -> tuple[str | None, str, str, list[str], str | None]:
        with duckdb.connect(str(self.path)) as connection:
            snapshot = connection.execute(
                "SELECT snapshot_id FROM player_directory_snapshots WHERE observed_at <= ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [at],
            ).fetchone()
            if not snapshot:
                return None, "unresolved", "no player directory exists as of observation", [], None
            explicit = [
                (provider, row.get(f"{provider}_id"))
                for provider in ("sleeper", "gsis", "espn", "yahoo")
                if row.get(f"{provider}_id")
            ]
            for provider, provider_id in explicit:
                match = connection.execute(
                    "SELECT canonical_player_id FROM player_provider_ids "
                    "WHERE provider = ? AND provider_player_id = ? AND first_observed_at <= ?",
                    [provider, provider_id, at],
                ).fetchone()
                if match:
                    return (
                        str(match[0]),
                        "matched",
                        f"explicit {provider} ID",
                        [],
                        f"explicit_{provider}_id",
                    )
            source_name = str(row["player_name"])
            position, team = row.get("position"), row.get("team")
            if not position:
                return None, "unresolved", "position is required for name matching", [], None
            candidates = connection.execute(
                "SELECT canonical_player_id, coalesce("
                "nullif(trim(first_name || ' ' || last_name), ''), "
                "json_extract_string(raw_payload, '$.full_name'), normalized_full_name), "
                "position, team "
                "FROM player_observations WHERE snapshot_id = ? ORDER BY canonical_player_id",
                [snapshot[0]],
            ).fetchall()

            def matching_ids(form: str, target: str) -> list[str]:
                forms = {
                    "strict_identity": strict_name,
                    "punctuation_normalized": normalize_name,
                    "suffix_insensitive": suffix_insensitive_name,
                    "verified_alias": normalize_name,
                }
                matches = {
                    str(candidate[0])
                    for candidate in candidates
                    if str(candidate[2]).upper() == str(position).upper()
                    and forms[form](str(candidate[1])) == target
                }
                if team:
                    team_matches = {
                        str(candidate[0])
                        for candidate in candidates
                        if str(candidate[2]).upper() == str(position).upper()
                        and forms[form](str(candidate[1])) == target
                        and candidate[3]
                        and str(candidate[3]).upper() == str(team).upper()
                    }
                    if team_matches:
                        matches = team_matches
                return sorted(matches)

            attempts = (
                ("strict_identity", strict_name(source_name)),
                ("punctuation_normalized", normalize_name(source_name)),
                ("suffix_insensitive", suffix_insensitive_name(source_name)),
            )
            for method, target in attempts:
                ids = matching_ids(method, target)
                if len(ids) == 1:
                    return ids[0], "matched", method.replace("_", " "), [], method
                if len(ids) > 1:
                    return None, "ambiguous", f"multiple {method} matches", ids, None
            aliases = alias_targets(source_name, position)
            if aliases:
                ids = sorted(
                    {
                        canonical_id
                        for alias in aliases
                        for canonical_id in matching_ids("verified_alias", alias)
                    }
                )
                if len(ids) == 1:
                    return ids[0], "matched", "verified alias", [], "verified_alias"
                if len(ids) > 1:
                    return None, "ambiguous", "multiple verified alias matches", ids, None
            name_matches = {
                str(candidate[0])
                for candidate in candidates
                if normalize_name(str(candidate[1])) == normalize_name(source_name)
            }
            if name_matches:
                return None, "unresolved", "position mismatch", sorted(name_matches), None
            return None, "unresolved", "player absent from current directory", [], None

    def insert_ranking_snapshot(
        self,
        snapshot: RankingSnapshot,
        entries: list[dict[str, Any]],
        issues: list[RankingIssue],
    ) -> bool:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                latest = connection.execute(
                    "SELECT payload_hash FROM ranking_snapshots WHERE source = ? AND season = ? "
                    "AND scoring_format = ? AND league_size = ? "
                    "AND resolver_version = ? "
                    "AND reprocessed_from_snapshot_id IS NOT DISTINCT FROM ? "
                    "ORDER BY observed_at DESC, ranking_snapshot_id DESC LIMIT 1",
                    [
                        snapshot.source,
                        snapshot.season,
                        snapshot.scoring_format,
                        snapshot.league_size,
                        snapshot.resolver_version,
                        snapshot.reprocessed_from_snapshot_id,
                    ],
                ).fetchone()
                if latest and str(latest[0]) == snapshot.payload_hash:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO ranking_snapshots (ranking_snapshot_id, source, source_version, "
                    "season, scoring_format, league_size, observed_at, imported_at, payload_hash, "
                    "original_filename, total_row_count, matched_row_count, unresolved_row_count, "
                    "ambiguous_row_count, schema_version, resolver_version, "
                    "reprocessed_from_snapshot_id) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        snapshot.ranking_snapshot_id,
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                        snapshot.scoring_format,
                        snapshot.league_size,
                        snapshot.observed_at,
                        snapshot.imported_at,
                        snapshot.payload_hash,
                        snapshot.original_filename,
                        snapshot.total_row_count,
                        snapshot.matched_row_count,
                        snapshot.unresolved_row_count,
                        snapshot.ambiguous_row_count,
                        snapshot.schema_version,
                        snapshot.resolver_version,
                        snapshot.reprocessed_from_snapshot_id,
                    ],
                )
                for entry in entries:
                    connection.execute(
                        "INSERT INTO ranking_entries (ranking_snapshot_id, source_row_number, "
                        "canonical_player_id, source_player_name, source_position, source_team, "
                        "overall_rank, positional_rank, adp, adp_sd, projected_points, "
                        "match_status, raw_payload, match_method) VALUES "
                        + "("
                        + ", ".join(["?"] * 14)
                        + ")",
                        [
                            snapshot.ranking_snapshot_id,
                            entry["source_row_number"],
                            entry["canonical_player_id"],
                            entry["player_name"],
                            entry.get("position"),
                            entry.get("team"),
                            entry.get("overall_rank"),
                            entry.get("positional_rank"),
                            entry.get("adp"),
                            entry.get("adp_sd"),
                            entry.get("projected_points"),
                            entry["match_status"],
                            json.dumps(entry["raw_payload"], sort_keys=True),
                            entry.get("match_method"),
                        ],
                    )
                for issue in issues:
                    connection.execute(
                        "INSERT INTO ranking_match_issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            snapshot.ranking_snapshot_id,
                            issue.source_row_number,
                            issue.source_player_name,
                            issue.source_position,
                            issue.source_team,
                            issue.match_status,
                            issue.reason,
                            json.dumps(issue.candidate_player_ids),
                            json.dumps(issue.raw_payload, sort_keys=True),
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def ranking_snapshots(self) -> list[RankingSnapshot]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT * FROM ranking_snapshots ORDER BY observed_at DESC, ranking_snapshot_id"
            ).fetchall()
        return [RankingSnapshot.from_row(row) for row in rows]

    def insert_adp_snapshot(
        self, snapshot: AdpSnapshot, entries: list[dict[str, Any]], issues: list[AdpIssue]
    ) -> bool:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT payload_hash FROM adp_snapshots WHERE source=? AND source_version=? "
                    "AND season=? AND league_size=? AND scoring_format=? AND draft_type=? "
                    "ORDER BY observed_at DESC, imported_at DESC, adp_snapshot_id DESC LIMIT 1",
                    [
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                        snapshot.league_size,
                        snapshot.scoring_format,
                        snapshot.draft_type,
                    ],
                ).fetchone()
                if existing and str(existing[0]) == snapshot.payload_hash:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO adp_snapshots VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        snapshot.adp_snapshot_id,
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                        snapshot.league_size,
                        snapshot.scoring_format,
                        snapshot.draft_type,
                        snapshot.observed_at,
                        snapshot.imported_at,
                        snapshot.payload_hash,
                        snapshot.identity_resolver_version,
                        snapshot.original_filename,
                        snapshot.total_row_count,
                        snapshot.matched_row_count,
                        snapshot.unresolved_row_count,
                        snapshot.ambiguous_row_count,
                        snapshot.schema_version,
                    ],
                )
                for entry in entries:
                    connection.execute(
                        "INSERT INTO adp_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            snapshot.adp_snapshot_id,
                            entry["source_row_number"],
                            entry["canonical_player_id"],
                            entry["player_name"],
                            entry.get("position"),
                            entry.get("team"),
                            entry["overall_adp"],
                            entry.get("adp_sd"),
                            entry.get("sample_size"),
                            entry["match_status"],
                            entry.get("match_method"),
                            json.dumps(entry["raw_payload"], sort_keys=True),
                        ],
                    )
                for issue in issues:
                    connection.execute(
                        "INSERT INTO adp_match_issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            snapshot.adp_snapshot_id,
                            issue.source_row_number,
                            issue.source_player_name,
                            issue.source_position,
                            issue.source_team,
                            issue.match_status,
                            issue.reason,
                            json.dumps(issue.candidate_player_ids),
                            json.dumps(issue.raw_payload, sort_keys=True),
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def adp_snapshots(self) -> list[AdpSnapshot]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT * FROM adp_snapshots ORDER BY observed_at DESC, imported_at DESC, "
                "adp_snapshot_id"
            ).fetchall()
        return [AdpSnapshot.from_row(row) for row in rows]

    def adp_issues(self, snapshot_id: str | None = None) -> list[AdpIssue]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT * FROM adp_match_issues WHERE (? IS NULL OR adp_snapshot_id=?) "
                "ORDER BY adp_snapshot_id, source_row_number",
                [snapshot_id, snapshot_id],
            ).fetchall()
        return [AdpIssue.from_row(row) for row in rows]

    def insert_schedule_snapshot(
        self, snapshot: TeamScheduleSnapshot, entries: list[dict[str, Any]]
    ) -> bool:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT payload_hash FROM team_schedule_snapshots WHERE source=? "
                    "AND source_version=? AND season=? "
                    "ORDER BY observed_at DESC, imported_at DESC, "
                    "schedule_snapshot_id DESC LIMIT 1",
                    [
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                    ],
                ).fetchone()
                if existing and str(existing[0]) == snapshot.payload_hash:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO team_schedule_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        snapshot.schedule_snapshot_id,
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                        snapshot.observed_at,
                        snapshot.imported_at,
                        snapshot.payload_hash,
                        snapshot.original_filename,
                        snapshot.total_row_count,
                        snapshot.schema_version,
                    ],
                )
                for entry in entries:
                    connection.execute(
                        "INSERT INTO team_schedule_entries VALUES (?, ?, ?, ?)",
                        [
                            snapshot.schedule_snapshot_id,
                            entry["team"],
                            entry["bye_week"],
                            json.dumps(entry["raw_payload"], sort_keys=True),
                        ],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def schedule_snapshots(self) -> list[TeamScheduleSnapshot]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT * FROM team_schedule_snapshots "
                "ORDER BY observed_at DESC, imported_at DESC, "
                "schedule_snapshot_id"
            ).fetchall()
        return [TeamScheduleSnapshot.from_row(row) for row in rows]

    def ranking_snapshot_for_reprocessing(
        self, snapshot_id: str
    ) -> tuple[RankingSnapshot, list[tuple[int, dict[str, Any]]]]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            snapshot_row = connection.execute(
                "SELECT * FROM ranking_snapshots WHERE ranking_snapshot_id = ?", [snapshot_id]
            ).fetchone()
            if snapshot_row is None:
                raise NotFoundError(f"Ranking snapshot {snapshot_id!r} was not found")
            rows = connection.execute(
                "SELECT source_row_number, raw_payload FROM ranking_entries "
                "WHERE ranking_snapshot_id = ? ORDER BY source_row_number",
                [snapshot_id],
            ).fetchall()
        return RankingSnapshot.from_row(snapshot_row), [
            (int(row_number), json.loads(raw_payload)) for row_number, raw_payload in rows
        ]

    def ranking_match_method_counts(self, snapshot_id: str) -> dict[str, int]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT coalesce(match_method, match_status), count(*) FROM ranking_entries "
                "WHERE ranking_snapshot_id = ? GROUP BY ALL ORDER BY 1",
                [snapshot_id],
            ).fetchall()
        return {str(method): int(count) for method, count in rows}

    def ranking_issues(self, snapshot_id: str | None = None) -> list[RankingIssue]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT ranking_snapshot_id, source_row_number, source_player_name, "
                "source_position, source_team, match_status, reason, candidate_player_ids, "
                "raw_payload FROM ranking_match_issues "
                "WHERE (? IS NULL OR ranking_snapshot_id = ?) "
                "ORDER BY ranking_snapshot_id, source_row_number",
                [snapshot_id, snapshot_id],
            ).fetchall()
        return [RankingIssue.from_row(row) for row in rows]

    def projection_context(self, at: datetime, league_id: str) -> tuple[str, str, dict[str, float]]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            player = connection.execute(
                "SELECT snapshot_id FROM player_directory_snapshots WHERE observed_at <= ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [at],
            ).fetchone()
            league = connection.execute(
                "SELECT snapshot_id, league_payload FROM draft_snapshots "
                "WHERE league_id = ? AND observed_at <= ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [league_id, at],
            ).fetchone()
        if player is None:
            raise NotFoundError("No player directory exists as of the projection observation")
        if league is None:
            raise NotFoundError(
                "No Sleeper league snapshot exists as of the projection observation"
            )
        league_payload = json.loads(league[1])
        scoring = league_payload.get("scoring_settings")
        if not isinstance(scoring, dict):
            raise NotFoundError("Selected Sleeper league snapshot has no scoring settings")
        return (
            str(player[0]),
            str(league[0]),
            {str(key): float(value) for key, value in scoring.items()},
        )

    def insert_projection_snapshot(
        self,
        snapshot: ProjectionSnapshot,
        sources: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        kicker_rows: list[dict[str, Any]],
        dst_rows: list[dict[str, Any]],
        issues: list[ProjectionIssue],
    ) -> tuple[ProjectionSnapshot, bool]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
                latest = connection.execute(
                    "SELECT * FROM projection_snapshots WHERE source = ? "
                    "AND source_version = ? AND season = ? AND horizon = ? "
                    "AND source_scoring_format = ? AND scoring_settings_hash = ? "
                    "AND player_snapshot_id = ? AND scoring_calculator_version = ? "
                    "AND observed_at <= ? "
                    "ORDER BY observed_at DESC, imported_at DESC, "
                    "projection_snapshot_id DESC LIMIT 1",
                    [
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                        snapshot.horizon,
                        snapshot.source_scoring_format,
                        snapshot.scoring_settings_hash,
                        snapshot.player_snapshot_id,
                        snapshot.scoring_calculator_version,
                        snapshot.observed_at,
                    ],
                ).fetchone()
                if latest and str(latest[8]) == snapshot.payload_hash:
                    persisted = ProjectionSnapshot.from_row(latest)
                    connection.rollback()
                    return persisted, False
                connection.execute(
                    "INSERT INTO projection_snapshots (projection_snapshot_id, source, "
                    "source_version, season, horizon, source_scoring_format, observed_at, "
                    "imported_at, payload_hash, total_row_count, matched_row_count, "
                    "unresolved_row_count, ambiguous_row_count, player_snapshot_id, "
                    "league_snapshot_id, scoring_settings_hash, scoring_settings, "
                    "scoring_calculator_version, schema_version) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        snapshot.projection_snapshot_id,
                        snapshot.source,
                        snapshot.source_version,
                        snapshot.season,
                        snapshot.horizon,
                        snapshot.source_scoring_format,
                        snapshot.observed_at,
                        snapshot.imported_at,
                        snapshot.payload_hash,
                        snapshot.total_row_count,
                        snapshot.matched_row_count,
                        snapshot.unresolved_row_count,
                        snapshot.ambiguous_row_count,
                        snapshot.player_snapshot_id,
                        snapshot.league_snapshot_id,
                        snapshot.scoring_settings_hash,
                        json.dumps(snapshot.scoring_settings, sort_keys=True),
                        snapshot.scoring_calculator_version,
                        snapshot.schema_version,
                    ],
                )
                connection.executemany(
                    "INSERT INTO projection_snapshot_sources (projection_snapshot_id, position, "
                    "original_filename, source_page_hash, row_count, published_columns) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        [
                            snapshot.projection_snapshot_id,
                            row["position"],
                            row["original_filename"],
                            row["source_page_hash"],
                            row["row_count"],
                            json.dumps(row["published_columns"]),
                        ]
                        for row in sources
                    ],
                )
                _insert_projection_entries(connection, snapshot.projection_snapshot_id, entries)
                _insert_kicker_projection_rows(
                    connection, snapshot.projection_snapshot_id, kicker_rows
                )
                _insert_dst_projection_rows(connection, snapshot.projection_snapshot_id, dst_rows)
                connection.executemany(
                    "INSERT INTO projection_match_issues (projection_snapshot_id, "
                    "source_position, source_row_number, source_player_name, source_team, "
                    "match_status, reason, candidate_player_ids, raw_payload, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        [
                            snapshot.projection_snapshot_id,
                            issue.source_position,
                            issue.source_row_number,
                            issue.source_player_name,
                            issue.source_team,
                            issue.match_status,
                            issue.reason,
                            json.dumps(issue.candidate_player_ids),
                            json.dumps(issue.raw_payload, sort_keys=True),
                            issue.schema_version,
                        ]
                        for issue in issues
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return snapshot, True

    def projection_snapshots(self) -> list[ProjectionSnapshot]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT * FROM projection_snapshots "
                "ORDER BY observed_at DESC, projection_snapshot_id"
            ).fetchall()
        return [ProjectionSnapshot.from_row(row) for row in rows]

    def projection_at(self, at: datetime, source: str | None = None) -> ProjectionSnapshot | None:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            row = connection.execute(
                "SELECT * FROM projection_snapshots WHERE observed_at <= ? "
                "AND (? IS NULL OR source = ?) "
                "ORDER BY observed_at DESC, imported_at DESC, "
                "projection_snapshot_id DESC LIMIT 1",
                [at, source, source],
            ).fetchone()
        return ProjectionSnapshot.from_row(row) if row else None

    def projection_issues(self, snapshot_id: str | None = None) -> list[ProjectionIssue]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT projection_snapshot_id, source_position, source_row_number, "
                "source_player_name, source_team, match_status, reason, candidate_player_ids, "
                "raw_payload, schema_version FROM projection_match_issues "
                "WHERE (? IS NULL OR projection_snapshot_id = ?) "
                "ORDER BY projection_snapshot_id, source_position, source_row_number",
                [snapshot_id, snapshot_id],
            ).fetchall()
        return [ProjectionIssue.from_row(row) for row in rows]

    def projection_summary_by_position(self, snapshot_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                "SELECT source_position, count(*), "
                "count(*) FILTER (WHERE match_status = 'matched'), "
                "count(*) FILTER (WHERE match_status = 'unresolved'), "
                "count(*) FILTER (WHERE match_status = 'ambiguous'), "
                "count(*) FILTER (WHERE scoring_completeness = 'complete'), "
                "count(*) FILTER (WHERE scoring_completeness = 'partial') "
                "FROM projection_entries WHERE projection_snapshot_id = ? "
                "GROUP BY source_position ORDER BY source_position",
                [snapshot_id],
            ).fetchall()
        return [
            {
                "position": row[0],
                "row_count": row[1],
                "matched_count": row[2],
                "unresolved_count": row[3],
                "ambiguous_count": row[4],
                "exact_scoring_count": row[5],
                "partial_scoring_count": row[6],
            }
            for row in rows
        ]

    def recommendation_inputs(
        self,
        at: datetime,
        *,
        draft_id: str | None,
        league_id: str | None,
        sleeper_user_id: str | None,
        draft_slot: int | None,
        ranking_source: str | None,
        projection_source: str = "cbs",
    ) -> RecommendationInputs:
        """Build one temporally consistent, provider-free recommendation input."""
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            draft_row = _select_recommendation_draft(connection, at, draft_id, league_id)
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
            except (TypeError, ValueError) as exc:
                raise InputError(
                    "incompatible_scoring_context",
                    "Selected draft scoring settings contain a nonnumeric value",
                    {"draft_id": draft_snapshot.draft_id},
                ) from exc
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
                "AND fetched_at <= ? ORDER BY observed_at DESC, fetched_at DESC, "
                "snapshot_id DESC LIMIT 1",
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
                "AND season = ? AND (? IS NULL OR source = ?) "
                "ORDER BY observed_at DESC, imported_at DESC, ranking_snapshot_id DESC LIMIT 1",
                [at, at, season, ranking_source, ranking_source],
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
                "ORDER BY observed_at DESC, imported_at DESC, "
                "projection_snapshot_id DESC LIMIT 1",
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
                    {
                        "as_of": at.isoformat(),
                        "source": projection_source,
                        "season": season,
                    },
                    code="missing_projection_snapshot",
                )
            projection = ProjectionSnapshot.from_row(projection_row)
            resolved_slot = _resolve_recommendation_draft_slot(
                draft_snapshot, draft_slot, sleeper_user_id
            )
            roster = _recommendation_roster_configuration(scoring_context)
            team_count, rounds, draft_type = _recommendation_draft_settings(draft_snapshot)
            league_type, keeper_status = _recommendation_league_format(scoring_context)
            scoring_format = _recommendation_scoring_format(normalized_scoring)
            provider_rows = connection.execute(
                "SELECT canonical_player_id, provider_player_id FROM player_provider_ids "
                "WHERE provider = 'sleeper' AND first_observed_at <= ? "
                "ORDER BY provider_player_id",
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
        return RecommendationInputs(
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

    def board(
        self,
        at: datetime,
        draft_id: str | None,
        league_id: str | None,
        source: str | None,
        position: str | None,
        limit: int,
    ) -> list[BoardPlayer]:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            rows = connection.execute(
                _BOARD_SQL,
                [
                    at,
                    draft_id,
                    draft_id,
                    league_id,
                    league_id,
                    at,
                    at,
                    source,
                    source,
                    position,
                    position,
                    limit,
                ],
            ).fetchall()
        return [BoardPlayer.from_row(row) for row in rows]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _select_recommendation_draft(
    connection: duckdb.DuckDBPyConnection,
    at: datetime,
    draft_id: str | None,
    league_id: str | None,
) -> tuple[Any, ...]:
    if draft_id is not None:
        row = connection.execute(
            "SELECT * FROM draft_snapshots WHERE draft_id = ? AND observed_at <= ? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            [draft_id, at],
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Draft {draft_id!r} has no snapshot as of the decision time",
                {"draft_id": draft_id, "as_of": at.isoformat()},
                code="draft_not_found",
            )
        return row
    if league_id is None:
        raise NotFoundError(
            "No configured league or explicit draft ID was supplied",
            {"as_of": at.isoformat()},
            code="no_draft_snapshot",
        )
    row = connection.execute(
        "SELECT * FROM draft_snapshots WHERE observed_at <= ? "
        "AND coalesce(source_league_id, league_id) = ? "
        "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
        [at, league_id],
    ).fetchone()
    if row is None:
        raise NotFoundError(
            "No draft snapshot exists for the configured league as of the decision time",
            {"league_id": league_id, "as_of": at.isoformat()},
            code="no_draft_snapshot",
        )
    return row


def _snapshot_from_row(row: tuple[Any, ...]) -> Snapshot:
    return Snapshot(
        snapshot_id=row[0],
        league_id=row[1],
        draft_id=row[2],
        observed_at=row[3],
        source_updated_at=row[4],
        payload_hash=row[5],
        pick_count=row[6],
        league=json.loads(row[7]),
        draft=json.loads(row[8]),
        picks=json.loads(row[9]),
        source_league_id=row[10],
        scoring_context_league_id=row[11],
        scoring_context=json.loads(row[12]) if row[12] is not None else None,
        draft_context_type=row[13],
    )


def _recommendation_draft_settings(snapshot: Snapshot) -> tuple[int, int, str]:
    settings = snapshot.draft.get("settings")
    if not isinstance(settings, dict):
        raise InputError(
            "unsupported_draft_format",
            "Selected draft has no usable settings",
            {"draft_id": snapshot.draft_id},
        )
    try:
        team_count = int(settings["teams"])
        rounds = int(settings["rounds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(
            "unsupported_draft_format",
            "Selected draft is missing team or round counts",
            {"draft_id": snapshot.draft_id},
        ) from exc
    return team_count, rounds, str(snapshot.draft.get("type") or "")


def _recommendation_league_format(
    context: dict[str, Any],
) -> tuple[
    Literal["redraft", "keeper", "dynasty", "unknown"],
    Literal["non_keeper", "keeper", "unknown"],
]:
    settings = context.get("settings")
    if not isinstance(settings, dict) or "type" not in settings:
        return "unknown", "unknown"
    try:
        league_type = int(settings["type"])
    except (TypeError, ValueError):
        return "unknown", "unknown"
    values = {0: "redraft", 1: "keeper", 2: "dynasty"}
    normalized = values.get(league_type)
    if normalized is None:
        return "unknown", "unknown"
    return cast(Literal["redraft", "keeper", "dynasty"], normalized), (
        "non_keeper" if normalized == "redraft" else "keeper"
    )


def _recommendation_scoring_format(
    scoring: dict[str, float],
) -> Literal["full_ppr", "half_ppr", "standard", "custom"]:
    receptions = scoring.get("rec", 0.0)
    if receptions == 1.0:
        return "full_ppr"
    if receptions == 0.5:
        return "half_ppr"
    if receptions == 0.0:
        return "standard"
    return "custom"


def _resolve_recommendation_draft_slot(
    snapshot: Snapshot, explicit_slot: int | None, sleeper_user_id: str | None
) -> int:
    team_count, _, _ = _recommendation_draft_settings(snapshot)
    if explicit_slot is not None:
        if not 1 <= explicit_slot <= team_count:
            raise InputError(
                "invalid_draft_slot",
                "Draft slot is outside the selected draft's team range",
                {"draft_slot": explicit_slot, "team_count": team_count},
            )
        return explicit_slot
    draft_order = snapshot.draft.get("draft_order")
    ordered_slot: int | None = None
    if sleeper_user_id and isinstance(draft_order, dict) and sleeper_user_id in draft_order:
        try:
            ordered_slot = int(draft_order[sleeper_user_id])
        except (TypeError, ValueError) as exc:
            raise InputError(
                "draft_slot_unresolved",
                "Configured user's draft-order slot is invalid",
                {"sleeper_user_id": sleeper_user_id},
            ) from exc
    inferred_slots = {
        int(pick["draft_slot"])
        for pick in snapshot.picks
        if sleeper_user_id
        and str(pick.get("picked_by")) == sleeper_user_id
        and pick.get("draft_slot") is not None
    }
    if len(inferred_slots) > 1 or (
        ordered_slot is not None and inferred_slots and inferred_slots != {ordered_slot}
    ):
        raise InputError(
            "draft_slot_conflict",
            "Configured user is associated with conflicting draft slots",
            {
                "sleeper_user_id": sleeper_user_id,
                "draft_order_slot": ordered_slot,
                "picked_by_slots": sorted(inferred_slots),
            },
        )
    resolved = ordered_slot or (next(iter(inferred_slots)) if inferred_slots else None)
    if resolved is None:
        raise InputError(
            "draft_slot_unresolved",
            "Draft slot could not be resolved; pass --draft-slot",
            {"draft_id": snapshot.draft_id},
        )
    if not 1 <= resolved <= team_count:
        raise InputError(
            "invalid_draft_slot",
            "Resolved draft slot is outside the selected draft's team range",
            {"draft_slot": resolved, "team_count": team_count},
        )
    return resolved


def _recommendation_roster_configuration(context: dict[str, Any]) -> RosterConfiguration:
    positions = context.get("roster_positions")
    if not isinstance(positions, list):
        raise InputError(
            "unsupported_roster_format",
            "Selected scoring context has no roster positions",
        )
    counts = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0, "BN": 0}
    ignored = {"K", "DEF", "DST"}
    for raw_position in positions:
        position = str(raw_position).upper()
        if position in counts:
            counts[position] += 1
        elif position in ignored:
            continue
        else:
            raise InputError(
                "unsupported_roster_format",
                "Selected league contains an unsupported roster slot",
                {"roster_position": position},
            )
    return RosterConfiguration(
        qb=counts["QB"],
        rb=counts["RB"],
        wr=counts["WR"],
        te=counts["TE"],
        flex=counts["FLEX"],
        bench=counts["BN"],
        k=sum(str(item).upper() == "K" for item in positions),
        defense=sum(str(item).upper() in {"DEF", "DST"} for item in positions),
    )


def _recommendation_picks(
    snapshot: Snapshot,
    resolved_slot: int,
    sleeper_to_canonical: dict[str, str],
) -> tuple[tuple[CompletedDraftPick, ...], tuple[str, ...]]:
    slot_to_roster = snapshot.draft.get("slot_to_roster_id")
    expected_roster = None
    if isinstance(slot_to_roster, dict):
        expected_roster = slot_to_roster.get(str(resolved_slot), slot_to_roster.get(resolved_slot))
    completed: list[CompletedDraftPick] = []
    unresolved: list[str] = []
    for index, pick in enumerate(snapshot.picks, start=1):
        player_id = _text(pick.get("player_id"))
        pick_slot = pick.get("draft_slot")
        belongs_to_user = pick_slot is not None and int(pick_slot) == resolved_slot
        if pick_slot is None and expected_roster is not None:
            belongs_to_user = str(pick.get("roster_id")) == str(expected_roster)
        canonical_id = sleeper_to_canonical.get(player_id) if player_id else None
        completed.append(
            CompletedDraftPick(
                pick_no=int(pick.get("pick_no", index)),
                draft_slot=(
                    resolved_slot
                    if belongs_to_user
                    else int(pick_slot)
                    if pick_slot is not None
                    else None
                ),
                canonical_player_id=canonical_id,
                sleeper_player_id=player_id,
                position=_text(pick.get("position"))
                or _text(
                    pick.get("metadata", {}).get("position")
                    if isinstance(pick.get("metadata"), dict)
                    else None
                ),
            )
        )
        if belongs_to_user and player_id and canonical_id is None:
            unresolved.append(player_id)
    return tuple(sorted(completed, key=lambda pick: pick.pick_no)), tuple(sorted(set(unresolved)))


def _recommendation_projection_players(
    connection: duckdb.DuckDBPyConnection,
    snapshot_id: str,
    canonical_to_sleepers: dict[str, list[str]],
) -> tuple[RecommendationPlayerInput, ...]:
    rows = connection.execute(
        "SELECT canonical_player_id, source_player_name, position, team, "
        "cbs_projected_points, league_known_component_points, league_projected_points, "
        "scoring_completeness, unprojected_scoring_keys FROM projection_entries "
        "WHERE projection_snapshot_id = ? AND match_status = 'matched' "
        "AND canonical_player_id IS NOT NULL AND upper(position) IN ('QB','RB','WR','TE') "
        "QUALIFY row_number() OVER (PARTITION BY canonical_player_id "
        "ORDER BY source_position, source_row_number) = 1 ORDER BY canonical_player_id",
        [snapshot_id],
    ).fetchall()
    return tuple(
        RecommendationPlayerInput(
            canonical_player_id=str(row[0]),
            sleeper_player_id=min(canonical_to_sleepers.get(str(row[0]), []), default=None),
            player_name=str(row[1]),
            position=cast(OffensivePosition, str(row[2]).upper()),
            team=_text(row[3]),
            cbs_projected_points=row[4],
            league_known_component_points=row[5],
            league_projected_points=row[6],
            scoring_completeness=cast(Literal["complete", "partial"], str(row[7])),
            unprojected_scoring_keys=tuple(json.loads(row[8])),
        )
        for row in rows
    )


def _recommendation_rankings(
    connection: duckdb.DuckDBPyConnection, snapshot_id: str
) -> tuple[ExpertRankingInput, ...]:
    rows = connection.execute(
        "SELECT canonical_player_id, overall_rank, positional_rank, "
        "json_extract_string(raw_payload, '$.tier') FROM ranking_entries "
        "WHERE ranking_snapshot_id = ? AND match_status = 'matched' "
        "AND canonical_player_id IS NOT NULL QUALIFY row_number() OVER ("
        "PARTITION BY canonical_player_id ORDER BY overall_rank NULLS LAST, source_row_number) = 1 "
        "ORDER BY canonical_player_id",
        [snapshot_id],
    ).fetchall()
    return tuple(
        ExpertRankingInput(
            canonical_player_id=str(row[0]),
            overall_rank=row[1],
            positional_rank=row[2],
            tier=row[3],
        )
        for row in rows
    )


_PROJECTION_ENTRY_FIELDS = (
    "source_position",
    "source_row_number",
    "canonical_player_id",
    "source_player_name",
    "position",
    "team",
    "games",
    "passing_attempts",
    "passing_completions",
    "passing_yards",
    "passing_yards_per_game",
    "passing_touchdowns",
    "interceptions",
    "passer_rating",
    "rushing_attempts",
    "rushing_yards",
    "rushing_yards_per_attempt",
    "rushing_touchdowns",
    "targets",
    "receptions",
    "receiving_yards",
    "receiving_yards_per_game",
    "receiving_yards_per_reception",
    "receiving_touchdowns",
    "fumbles_lost",
    "cbs_projected_points",
    "cbs_projected_points_per_game",
    "league_known_component_points",
    "league_projected_points",
    "scoring_completeness",
    "unprojected_scoring_keys",
    "match_status",
    "match_method",
    "raw_payload",
    "schema_version",
)


def _insert_projection_entries(
    connection: duckdb.DuckDBPyConnection, snapshot_id: str, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    columns = "projection_snapshot_id, " + ", ".join(_PROJECTION_ENTRY_FIELDS)
    placeholders = ", ".join(["?"] * (len(_PROJECTION_ENTRY_FIELDS) + 1))
    connection.executemany(
        f"INSERT INTO projection_entries ({columns}) VALUES ({placeholders})",
        [
            [
                snapshot_id,
                *[
                    json.dumps(row[field], sort_keys=True)
                    if field in {"unprojected_scoring_keys", "raw_payload"}
                    else row.get(field)
                    for field in _PROJECTION_ENTRY_FIELDS
                ],
            ]
            for row in rows
        ],
    )


def _insert_kicker_projection_rows(
    connection: duckdb.DuckDBPyConnection, snapshot_id: str, rows: list[dict[str, Any]]
) -> None:
    fields = (
        "source_row_number",
        "field_goals_made",
        "field_goals_attempted",
        "longest_field_goal",
        "field_goals_made_1_19",
        "field_goals_attempted_1_19",
        "field_goals_made_20_29",
        "field_goals_attempted_20_29",
        "field_goals_made_30_39",
        "field_goals_attempted_30_39",
        "field_goals_made_40_49",
        "field_goals_attempted_40_49",
        "field_goals_made_50_plus",
        "field_goals_attempted_50_plus",
        "extra_points_made",
        "extra_points_attempted",
    )
    if rows:
        connection.executemany(
            "INSERT INTO projection_kicker_stats VALUES (" + ", ".join(["?"] * 17) + ")",
            [[snapshot_id, *[row.get(field) for field in fields]] for row in rows],
        )


def _insert_dst_projection_rows(
    connection: duckdb.DuckDBPyConnection, snapshot_id: str, rows: list[dict[str, Any]]
) -> None:
    fields = (
        "source_row_number",
        "defensive_interceptions",
        "safeties",
        "sacks",
        "tackles",
        "defensive_fumbles_recovered",
        "forced_fumbles",
        "defensive_touchdowns",
        "points_allowed",
        "points_allowed_per_game",
        "passing_yards_allowed",
        "rushing_yards_allowed",
        "total_yards_allowed",
        "yards_allowed_per_game",
    )
    if rows:
        connection.executemany(
            "INSERT INTO projection_dst_stats VALUES (" + ", ".join(["?"] * 15) + ")",
            [[snapshot_id, *[row.get(field) for field in fields]] for row in rows],
        )


_BOARD_SQL = """
WITH draft AS (
 SELECT snapshot_id, observed_at FROM draft_snapshots
 WHERE observed_at <= ? AND (? IS NULL OR draft_id = ?) AND (? IS NULL OR league_id = ?)
 ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1
), players AS (
 SELECT snapshot_id, observed_at FROM player_directory_snapshots WHERE observed_at <= ?
 ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1
), ranking AS (
 SELECT ranking_snapshot_id, source, observed_at FROM ranking_snapshots
 WHERE observed_at <= ? AND (? IS NULL OR source = ?)
 ORDER BY observed_at DESC, ranking_snapshot_id DESC LIMIT 1
), drafted AS (
 SELECT DISTINCT p.canonical_player_id FROM draft d
 JOIN draft_snapshot_picks dp ON dp.snapshot_id = d.snapshot_id
 JOIN player_provider_ids p ON p.provider = 'sleeper' AND p.provider_player_id = dp.player_id
)
SELECT e.canonical_player_id, ids.provider_player_id,
 trim(o.first_name || ' ' || o.last_name), o.position, o.team,
 e.overall_rank, e.positional_rank, e.adp, e.adp_sd, e.projected_points,
 r.source, d.snapshot_id, d.observed_at, p.snapshot_id, p.observed_at,
 r.ranking_snapshot_id, r.observed_at
FROM ranking r JOIN ranking_entries e ON e.ranking_snapshot_id = r.ranking_snapshot_id
JOIN players p ON true
JOIN player_observations o ON o.snapshot_id = p.snapshot_id
 AND o.canonical_player_id = e.canonical_player_id
 AND o.provider_player_id = (SELECT min(o2.provider_player_id) FROM player_observations o2
  WHERE o2.snapshot_id = p.snapshot_id AND o2.canonical_player_id = e.canonical_player_id)
LEFT JOIN player_provider_ids ids ON ids.canonical_player_id = e.canonical_player_id
 AND ids.provider = 'sleeper'
 AND ids.provider_player_id = (SELECT min(ids2.provider_player_id) FROM player_provider_ids ids2
  WHERE ids2.canonical_player_id = e.canonical_player_id AND ids2.provider = 'sleeper')
JOIN draft d ON true
LEFT JOIN drafted x ON x.canonical_player_id = e.canonical_player_id
WHERE e.match_status = 'matched' AND x.canonical_player_id IS NULL
 AND (? IS NULL OR upper(o.position) = upper(?))
ORDER BY e.overall_rank NULLS LAST, e.adp NULLS LAST,
 o.normalized_full_name, e.canonical_player_id LIMIT ?
"""
