from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from fantasy_war_room.models import (
    BoardPlayer,
    PlayerDirectorySnapshot,
    PlayerSearchResult,
    RankingIssue,
    RankingSnapshot,
    Snapshot,
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

MIGRATIONS = (
    (1, "initial_m1_schema", MIGRATION_1),
    (2, "repeatable_draft_states", MIGRATION_2),
    (3, "m2_player_intelligence", MIGRATION_3),
    (4, "m2_observation_schema_alignment", MIGRATION_4),
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
                    "INSERT INTO draft_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        )


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
                    return False
                connection.execute(
                    "INSERT INTO player_directory_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                for player in players:
                    self._insert_player_observation(connection, snapshot, player)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

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
    ) -> tuple[str | None, str, str, list[str]]:
        with duckdb.connect(str(self.path)) as connection:
            snapshot = connection.execute(
                "SELECT snapshot_id FROM player_directory_snapshots WHERE observed_at <= ? "
                "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                [at],
            ).fetchone()
            if not snapshot:
                return None, "unresolved", "no player directory exists as of observation", []
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
                    return str(match[0]), "matched", f"explicit {provider} ID", []
            name = row["normalized_name"]
            position, team = row.get("position"), row.get("team")
            if not position:
                return None, "unresolved", "position is required for name matching", []
            if team:
                exact_team = connection.execute(
                    "SELECT canonical_player_id FROM player_observations WHERE snapshot_id = ? "
                    "AND normalized_full_name = ? AND upper(position) = upper(?) "
                    "AND upper(team) = upper(?) ORDER BY canonical_player_id",
                    [snapshot[0], name, position, team],
                ).fetchall()
                team_ids = [str(candidate[0]) for candidate in exact_team]
                if len(team_ids) == 1:
                    return team_ids[0], "matched", "exact name, position, and team", []
                if len(team_ids) > 1:
                    return None, "ambiguous", "multiple exact player matches", team_ids
            candidates = connection.execute(
                "SELECT canonical_player_id FROM player_observations WHERE snapshot_id = ? "
                "AND normalized_full_name = ? AND upper(position) = upper(?) "
                "ORDER BY canonical_player_id",
                [snapshot[0], name, position],
            ).fetchall()
            ids = [str(candidate[0]) for candidate in candidates]
            if len(ids) == 1:
                return ids[0], "matched", "unique exact name and position", []
            if len(ids) > 1:
                return None, "ambiguous", "multiple exact player matches", ids
            return None, "unresolved", "no exact player match", []

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
                    "ORDER BY observed_at DESC, ranking_snapshot_id DESC LIMIT 1",
                    [
                        snapshot.source,
                        snapshot.season,
                        snapshot.scoring_format,
                        snapshot.league_size,
                    ],
                ).fetchone()
                if latest and str(latest[0]) == snapshot.payload_hash:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO ranking_snapshots VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ],
                )
                for entry in entries:
                    connection.execute(
                        "INSERT INTO ranking_entries VALUES " + "(" + ", ".join(["?"] * 13) + ")",
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
LEFT JOIN player_provider_ids ids ON ids.canonical_player_id = e.canonical_player_id
 AND ids.provider = 'sleeper'
JOIN draft d ON true
LEFT JOIN drafted x ON x.canonical_player_id = e.canonical_player_id
WHERE e.match_status = 'matched' AND x.canonical_player_id IS NULL
 AND (? IS NULL OR upper(o.position) = upper(?))
ORDER BY e.overall_rank NULLS LAST, e.adp NULLS LAST,
 o.normalized_full_name, e.canonical_player_id LIMIT ?
"""
