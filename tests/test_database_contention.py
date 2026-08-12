from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from fantasy_war_room.database import with_database_lock_retry
from fantasy_war_room.errors import DatabaseBusyError
from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.services import _persist_watched_snapshot


def test_real_subprocess_writer_contention_retries_to_one_committed_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "contended.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE state (left_value INTEGER, right_value INTEGER)")
        connection.execute("INSERT INTO state VALUES (0, 0)")

    process = _locking_process(
        path,
        "c=duckdb.connect(path); c.begin(); "
        "c.execute('UPDATE state SET left_value=1'); print('READY', flush=True); "
        "time.sleep(0.3); c.execute('UPDATE state SET right_value=1'); c.commit(); c.close()",
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "READY"

    def read_state() -> tuple[int, int]:
        with duckdb.connect(str(path), read_only=True) as connection:
            row = connection.execute("SELECT left_value, right_value FROM state").fetchone()
            assert row is not None
            return int(row[0]), int(row[1])

    assert with_database_lock_retry(read_state) == (1, 1)
    assert process.wait(timeout=5) == 0


def test_database_lock_retry_exhaustion_is_stable_database_busy() -> None:
    def locked() -> None:
        raise duckdb.IOException("IO Error: Could not set lock: Conflicting lock")

    with pytest.raises(DatabaseBusyError) as raised:
        with_database_lock_retry(locked, delays=(), sleep=lambda _: None)

    assert raised.value.code == "database_busy"
    assert raised.value.details == {"attempt_count": 1}


def test_real_subprocess_reader_contention_does_not_drop_watcher_a_b_a(
    tmp_path: Path,
) -> None:
    repository = SnapshotRepository(tmp_path / "watch-contended.duckdb")
    repository.initialize()
    process = _locking_process(
        repository.path,
        "c=duckdb.connect(path, read_only=True); c.begin(); "
        "c.execute('SELECT count(*) FROM draft_snapshots').fetchone(); "
        "print('READY', flush=True); time.sleep(0.3); c.commit(); c.close()",
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "READY"

    base = datetime(2026, 8, 1, tzinfo=UTC)
    states = [
        _snapshot("a-1", "A", base),
        _snapshot("b", "B", base + timedelta(seconds=1)),
        _snapshot("a-2", "A", base + timedelta(seconds=2)),
    ]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        time.sleep(delay)

    for snapshot in states:
        assert _persist_watched_snapshot(repository, snapshot, 0.01, sleep)

    assert process.wait(timeout=5) == 0
    with duckdb.connect(str(repository.path)) as connection:
        rows = connection.execute(
            "SELECT payload_hash FROM draft_snapshots ORDER BY observed_at, snapshot_id"
        ).fetchall()
    assert [row[0] for row in rows] == ["A", "B", "A"]
    assert sleeps


def _locking_process(path: Path, body: str) -> subprocess.Popen[str]:
    code = "import duckdb,sys,time\npath=sys.argv[1]\n" + body
    return subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _snapshot(snapshot_id: str, payload_hash: str, observed_at: datetime) -> Snapshot:
    draft = {
        "draft_id": "draft-1",
        "league_id": "league-1",
        "season": "2026",
        "type": "snake",
        "status": payload_hash,
        "settings": {"teams": 2, "rounds": 2},
    }
    league = {
        "league_id": "league-1",
        "season": "2026",
        "scoring_settings": {},
        "roster_positions": ["QB", "BN"],
    }
    return Snapshot(
        snapshot_id=snapshot_id,
        league_id="league-1",
        draft_id="draft-1",
        observed_at=observed_at,
        source_updated_at=None,
        payload_hash=payload_hash,
        pick_count=0,
        league=league,
        draft=draft,
        picks=[],
        source_league_id="league-1",
        scoring_context_league_id="league-1",
        scoring_context=league,
        draft_context_type="league",
    )
