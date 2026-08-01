from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from fantasy_war_room.models import Snapshot

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

MIGRATIONS = ((1, MIGRATION_1), (2, MIGRATION_2))


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
                for version, sql in MIGRATIONS:
                    if version not in applied:
                        connection.execute(sql)
                        connection.execute(
                            "INSERT INTO schema_migrations (version) VALUES (?)", [version]
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
