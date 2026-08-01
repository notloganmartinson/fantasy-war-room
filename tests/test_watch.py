from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import respx

from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.services import watch
from fantasy_war_room.sleeper import SleeperClient


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_watch_persists_only_changed_draft_states(
    api: Any, tmp_path: Path, sleeper_payloads: dict[str, Any]
) -> None:
    base = "https://api.sleeper.app/v1"
    pick = sleeper_payloads["picks"][0]
    picks_route = api.get(f"{base}/draft/d1/picks").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[pick]),
            httpx.Response(200, json=[pick]),
        ]
    )
    api.get(f"{base}/league/l1").mock(
        return_value=httpx.Response(200, json=sleeper_payloads["league"])
    )
    repository = SnapshotRepository(tmp_path / "history.duckdb")
    observed: list[Snapshot] = []
    client = SleeperClient(base, 1)

    try:
        watch(
            client,
            repository,
            "l1",
            sleeper_payloads["draft"],
            poll_seconds=0,
            on_snapshot=observed.append,
            max_polls=3,
            sleep=lambda _: None,
        )
    finally:
        client.close()

    assert picks_route.call_count == 3
    assert [snapshot.pick_count for snapshot in observed] == [0, 1]
    assert repository.state_at("d1", observed[0].observed_at) == observed[0]
    assert repository.state_at("d1", observed[1].observed_at) == observed[1]
