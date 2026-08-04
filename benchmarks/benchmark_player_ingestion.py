from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb

from fantasy_war_room.intelligence import sync_players
from fantasy_war_room.models import PlayerDirectorySnapshot
from fantasy_war_room.repository import IntelligenceRepository


class SyntheticProvider:
    def __init__(self, player_count: int) -> None:
        self.payload = {
            str(index): {
                "first_name": f"First{index}",
                "last_name": f"Last{index}",
                "full_name": f"First{index} Last{index}",
                "position": ("QB", "RB", "WR", "TE")[index % 4],
                "fantasy_positions": [("QB", "RB", "WR", "TE")[index % 4]],
                "team": "ARI",
                "active": True,
                "gsis_id": f"gsis-{index}",
                "metadata": {"synthetic": True, "index": index},
            }
            for index in range(player_count)
        }

    def get_nfl_players(self) -> dict[str, dict[str, Any]]:
        return self.payload


class LegacyRepository(IntelligenceRepository):
    """Pre-optimization persistence loop retained only for comparative benchmarking."""

    def insert_player_directory(
        self,
        snapshot: PlayerDirectorySnapshot,
        players: list[dict[str, Any]],
        timings: dict[str, float] | None = None,
    ) -> bool:
        self.initialize()
        with duckdb.connect(str(self.path)) as connection:
            connection.begin()
            try:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark synthetic M2 player ingestion")
    parser.add_argument("--players", type=int, default=10_000)
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        provider = SyntheticProvider(args.players)
        repository_type = LegacyRepository if args.legacy else IntelligenceRepository
        repository = repository_type(root / "benchmark.duckdb")
        timings: dict[str, float] = {}
        started = time.perf_counter()
        snapshot, created, source = sync_players(
            provider, repository, root / "cache", force=True, timings=timings
        )
        elapsed = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "players": snapshot.player_count,
                    "mode": "legacy" if args.legacy else "bulk",
                    "created": created,
                    "source": source,
                    "elapsed_seconds": elapsed,
                    "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "timings_seconds": timings,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
