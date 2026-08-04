from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx
from typer.testing import CliRunner

from fantasy_war_room.config import config_file_path


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_configure_and_discover_json(
    api: Any, runner: CliRunner, xdg: Path, sleeper_payloads: dict[str, Any]
) -> None:
    from conftest import parse_output, register_sleeper

    register_sleeper(api, sleeper_payloads)
    from fantasy_war_room.cli import app

    configured = runner.invoke(
        app, ["configure", "--username", "alice", "--non-interactive", "--json"]
    )
    assert configured.exit_code == 0
    assert parse_output(configured)["data"]["league_id"] == "l1"
    assert config_file_path().is_file()
    discovered = runner.invoke(app, ["discover", "--json"])
    assert discovered.exit_code == 0
    body = parse_output(discovered)
    assert body["status"] == "success" and body["command"] == "discover"
    assert body["error"] is None and body["data"]["leagues"][0]["league_id"] == "l1"


def test_all_local_json_commands_and_errors(runner: CliRunner, xdg: Path, tmp_path: Path) -> None:
    from conftest import parse_output

    from fantasy_war_room.cli import app

    doctor = runner.invoke(app, ["doctor", "--json"])
    assert doctor.exit_code == 0 and parse_output(doctor)["command"] == "doctor"
    malformed = runner.invoke(app, ["state-at", "--draft-id", "d1", "--at", "yesterday", "--json"])
    assert malformed.exit_code == 2
    assert parse_output(malformed)["error"]["code"] == "invalid_timestamp"
    missing = runner.invoke(
        app, ["state-at", "--draft-id", "d1", "--at", "2026-01-01T00:00:00Z", "--json"]
    )
    assert missing.exit_code == 5 and parse_output(missing)["status"] == "error"
    assert json.loads(malformed.stdout)  # diagnostics did not corrupt stdout JSON


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_sync_json_deduplicates(
    api: Any, runner: CliRunner, xdg: Path, sleeper_payloads: dict[str, Any]
) -> None:
    from conftest import parse_output, register_sleeper

    from fantasy_war_room.cli import app

    register_sleeper(api, sleeper_payloads)
    first = runner.invoke(app, ["sync", "--league-id", "l1", "--json"])
    second = runner.invoke(app, ["sync", "--league-id", "l1", "--json"])
    assert first.exit_code == second.exit_code == 0
    assert parse_output(first)["data"]["created"] is True
    assert parse_output(second)["data"]["created"] is False


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_network_and_configuration_exit_codes(api: Any, runner: CliRunner, xdg: Path) -> None:
    from conftest import parse_output

    from fantasy_war_room.cli import app

    config = runner.invoke(app, ["discover", "--json"])
    assert config.exit_code == 3 and parse_output(config)["error"]["code"] == "missing_username"
    api.get("https://api.sleeper.app/v1/user/alice").mock(side_effect=httpx.ConnectError("offline"))
    network = runner.invoke(app, ["discover", "--username", "alice", "--json"])
    assert network.exit_code == 4 and parse_output(network)["error"]["code"] == "provider_error"


def test_cli_runs_outside_repository(runner: CliRunner, xdg: Path, tmp_path: Path) -> None:
    from conftest import parse_output

    from fantasy_war_room.cli import app

    result = runner.invoke(app, ["doctor", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    database = Path(parse_output(result)["data"]["database"])
    assert database.is_absolute()
    assert str(database).startswith(str(xdg.parent / "data"))


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_m2_json_commands_and_cache_work_outside_repository(
    api: Any, runner: CliRunner, xdg: Path, sleeper_payloads: dict[str, Any]
) -> None:
    from conftest import parse_output, register_sleeper

    from fantasy_war_room.cli import app

    register_sleeper(api, sleeper_payloads)
    player_route = api.routes[-1]
    first = runner.invoke(app, ["players", "sync", "--timings", "--json"])
    cached = runner.invoke(app, ["players", "sync", "--json"])
    forced = runner.invoke(app, ["players", "sync", "--force", "--json"])

    assert first.exit_code == cached.exit_code == forced.exit_code == 0
    assert parse_output(first)["command"] == "players sync"
    assert parse_output(first)["data"]["source"] == "network"
    assert set(parse_output(first)["data"]["timings_seconds"]) == {
        "cache_read_or_network_download",
        "parsing_and_normalization",
        "identity_resolution",
        "database_persistence",
        "total",
    }
    assert parse_output(cached)["data"] == {
        **parse_output(cached)["data"],
        "source": "cache",
    }
    assert parse_output(forced)["data"]["source"] == "network"
    assert player_route.call_count == 2

    search = runner.invoke(app, ["players", "search", "a player", "--json"])
    assert search.exit_code == 0
    search_body = parse_output(search)
    assert search_body["status"] == "success" and search_body["error"] is None
    assert search_body["data"]["players"][0]["sleeper_player_id"] == "p1"

    fixture = Path(__file__).parent / "fixtures" / "synthetic_rankings.csv"
    imported = runner.invoke(
        app,
        [
            "rankings",
            "import",
            str(fixture),
            "--source",
            "fixture",
            "--season",
            "2026",
            "--scoring",
            "ppr",
            "--league-size",
            "10",
            "--json",
        ],
    )
    assert imported.exit_code == 0
    assert parse_output(imported)["command"] == "rankings import"
    listed = runner.invoke(app, ["rankings", "list", "--json"])
    unresolved = runner.invoke(app, ["rankings", "unresolved", "--json"])
    assert parse_output(listed)["data"]["snapshots"][0]["source"] == "fixture"
    assert parse_output(unresolved)["data"]["issues"]


def test_board_json_is_stable_and_offline(runner: CliRunner, xdg: Path) -> None:
    from conftest import parse_output

    from fantasy_war_room.cli import app

    result = runner.invoke(app, ["board", "--json"])
    assert result.exit_code == 0
    assert parse_output(result) == {
        "command": "board",
        "data": {"as_of": parse_output(result)["data"]["as_of"], "players": []},
        "error": None,
        "status": "success",
    }
