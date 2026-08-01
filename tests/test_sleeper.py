from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from fantasy_war_room.errors import ProviderError
from fantasy_war_room.sleeper import SleeperClient


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_all_adapter_methods(api: Any, sleeper_payloads: dict[str, Any]) -> None:
    from conftest import register_sleeper

    register_sleeper(api, sleeper_payloads)
    client = SleeperClient("https://api.sleeper.app/v1", 1)
    assert client.get_user("alice")["user_id"] == "u1"
    assert client.get_user_leagues("u1", "2026")[0]["league_id"] == "l1"
    assert client.get_league("l1")["name"] == "Friends"
    assert client.get_league_drafts("l1")[0]["draft_id"] == "d1"
    assert client.get_draft("d1")["draft_id"] == "d1"
    assert client.get_draft_picks("d1")[0]["pick_no"] == 1
    client.close()


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_network_failure_is_provider_error(api: Any) -> None:
    api.get("https://api.sleeper.app/v1/user/alice").mock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(ProviderError):
        SleeperClient("https://api.sleeper.app/v1", 0.1).get_user("alice")
