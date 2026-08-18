from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.services import sync


class FakeProvider:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads

    def get_league(self, league_id: str) -> dict[str, Any]:
        return self.payloads["league"]

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return [self.payloads["draft"]]

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self.payloads["draft"]

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return self.payloads["picks"]


def test_dedup_changed_picks_and_as_of(tmp_path: Path, sleeper_payloads: dict[str, Any]) -> None:
    repository = SnapshotRepository(tmp_path / "history.duckdb")
    provider = FakeProvider(sleeper_payloads)
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    second_time = datetime(2026, 1, 2, tzinfo=UTC)

    first, created = sync(provider, repository, "l1", first_time)
    duplicate, duplicate_created = sync(provider, repository, "l1", second_time)
    assert created is True
    assert duplicate_created is False
    assert first.payload_hash == duplicate.payload_hash

    sleeper_payloads["picks"] = [*sleeper_payloads["picks"], {"pick_no": 2, "player_id": "p2"}]
    second, second_created = sync(provider, repository, "l1", second_time)
    assert second_created is True
    assert second.pick_count == 2
    state = repository.state_at("d1", datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert state is not None
    assert state.snapshot_id == first.snapshot_id
    assert repository.state_at("d1", datetime(2025, 12, 31, tzinfo=UTC)) is None


def test_dedup_allows_state_a_b_a(tmp_path: Path, sleeper_payloads: dict[str, Any]) -> None:
    repository = SnapshotRepository(tmp_path / "history.duckdb")
    provider = FakeProvider(sleeper_payloads)
    state_a = list(sleeper_payloads["picks"])

    first, first_created = sync(provider, repository, "l1", datetime(2026, 1, 1, tzinfo=UTC))
    sleeper_payloads["picks"] = [*state_a, {"pick_no": 2, "player_id": "p2"}]
    second, second_created = sync(provider, repository, "l1", datetime(2026, 1, 2, tzinfo=UTC))
    sleeper_payloads["picks"] = state_a
    third, third_created = sync(provider, repository, "l1", datetime(2026, 1, 3, tzinfo=UTC))

    assert (first_created, second_created, third_created) == (True, True, True)
    assert first.payload_hash == third.payload_hash != second.payload_hash
    assert repository.state_at("d1", datetime(2026, 1, 3, tzinfo=UTC)) == third
    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute("SELECT count(*) FROM draft_snapshots").fetchone() == (3,)


def test_initialize_is_idempotent_and_records_ordered_migrations(tmp_path: Path) -> None:
    repository = SnapshotRepository(tmp_path / "new.duckdb")

    repository.initialize()
    repository.initialize()

    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,)]
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'draft_snapshots' ORDER BY ordinal_position"
        ).fetchall()
    assert columns == [(name,) for name in _SNAPSHOT_COLUMNS]


def test_legacy_m1_database_upgrades_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(_LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO draft_snapshots VALUES "
            "('s1', 'l1', 'd1', '2026-01-01T00:00:00Z', NULL, 'hash-a', 1, '{}', '{}', '[]')"
        )
        connection.execute(
            "INSERT INTO draft_snapshot_picks VALUES "
            "('s1', 'd1', 1, 1, 1, '1', 'u1', 'p1', 'A Player', 'QB', 'BUF', '{}')"
        )

    repository = SnapshotRepository(path)
    repository.initialize()

    with duckdb.connect(str(path)) as connection:
        assert connection.execute("SELECT snapshot_id FROM draft_snapshots").fetchall() == [("s1",)]
        assert connection.execute(
            "SELECT snapshot_id, player_id FROM draft_snapshot_picks"
        ).fetchall() == [("s1", "p1")]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,)]
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'draft_snapshots' ORDER BY ordinal_position"
        ).fetchall()
    assert columns == [(name,) for name in _SNAPSHOT_COLUMNS]


_SNAPSHOT_COLUMNS = [
    "snapshot_id",
    "league_id",
    "draft_id",
    "observed_at",
    "source_updated_at",
    "payload_hash",
    "pick_count",
    "league_payload",
    "draft_payload",
    "picks_payload",
    "source_league_id",
    "scoring_context_league_id",
    "scoring_context_payload",
    "draft_context_type",
]

_LEGACY_SCHEMA = """
CREATE TABLE draft_snapshots (
 snapshot_id VARCHAR PRIMARY KEY, league_id VARCHAR NOT NULL, draft_id VARCHAR NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL, source_updated_at TIMESTAMPTZ,
 payload_hash VARCHAR NOT NULL, pick_count INTEGER NOT NULL,
 league_payload JSON NOT NULL, draft_payload JSON NOT NULL, picks_payload JSON NOT NULL,
 UNIQUE(draft_id, payload_hash)
);
CREATE TABLE draft_snapshot_picks (
 snapshot_id VARCHAR NOT NULL, draft_id VARCHAR NOT NULL, pick_no INTEGER NOT NULL,
 round INTEGER, draft_slot INTEGER, roster_id VARCHAR, picked_by VARCHAR,
 player_id VARCHAR, player_name VARCHAR, position VARCHAR, team VARCHAR,
 raw_payload JSON NOT NULL, PRIMARY KEY(snapshot_id, pick_no)
);
"""
