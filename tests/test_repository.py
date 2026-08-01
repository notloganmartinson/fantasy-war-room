from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
