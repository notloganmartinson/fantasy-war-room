from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from fantasy_war_room.config import LeagueContext, Settings
from fantasy_war_room.errors import InputError, ProviderError
from fantasy_war_room.external_sources import (
    FFC_BASE_URL,
    NFL_TEAMS,
    NFLVERSE_SCHEDULE_URL,
    PortableSourceClient,
    acquire_ffc_adp,
    acquire_nflverse_byes,
    classify_ffc_scoring,
    scoring_to_ffc,
)


def _ffc_payload(*, teams: int = 10, scoring: str = "PPR", year: int = 2026) -> dict[str, Any]:
    return {
        "status": "Success",
        "meta": {
            "count": 1,
            "total_count": 1,
            "page": 1,
            "total_pages": 1,
        },
        "players": [
            {
                "player_id": 7,
                "name": "Portable Player",
                "position": "RB",
                "team": "MIN",
                "adp": 12.4,
                "stdev": 2.1,
                "times_drafted": 456,
            }
        ],
    }


def _valid_schedule_csv() -> bytes:
    teams = sorted(NFL_TEAMS)
    rows = ["season,week,game_type,home_team,away_team"]
    # Two teams are on bye in each of weeks 3-18. Every team therefore plays 17 games.
    for week in range(1, 19):
        bye = set(teams[(week - 3) * 2 : (week - 2) * 2]) if week >= 3 else set()
        playing = [team for team in teams if team not in bye]
        for index in range(0, len(playing), 2):
            rows.append(f"2026,{week},REG,{playing[index]},{playing[index + 1]}")
    return ("\n".join(rows) + "\n").encode()


@pytest.mark.parametrize(
    ("internal", "external"),
    [("full_ppr", "ppr"), ("half_ppr", "half-ppr"), ("standard", "standard")],
)
def test_ffc_scoring_mapping_is_explicit(internal: str, external: str) -> None:
    assert scoring_to_ffc(internal) == external


def test_ffc_rejects_custom_scoring() -> None:
    with pytest.raises(InputError) as raised:
        scoring_to_ffc("custom")
    assert raised.value.code == "unsupported_adp_scoring_format"


@pytest.mark.parametrize(
    "settings",
    [
        {"rec": 1.0, "bonus_rec_te": 0.5},
        {"rec": 0.5, "pass_td": 6.0},
        {"rec": 0.0, "rush_fd": 0.5},
    ],
)
def test_ffc_rejects_non_generic_offensive_scoring(settings: dict[str, float]) -> None:
    with pytest.raises(InputError) as raised:
        classify_ffc_scoring(settings)
    assert raised.value.code == "unsupported_adp_scoring_format"


@pytest.mark.parametrize("teams", [10, 12])
def test_ffc_acquisition_preserves_compatibility_and_dispersion(
    respx_mock: Any, tmp_path: Path, teams: int
) -> None:
    url = f"{FFC_BASE_URL}/api/v1/adp/ppr?teams={teams}&year=2026"
    route = respx_mock.get(url).mock(
        return_value=httpx.Response(200, json=_ffc_payload(teams=teams))
    )
    client = PortableSourceClient(1, tmp_path)
    try:
        result = acquire_ffc_adp(
            client,
            season="2026",
            league_size=teams,
            scoring_format="full_ppr",
            draft_type="snake",
        )
        cached = acquire_ffc_adp(
            client,
            season="2026",
            league_size=teams,
            scoring_format="full_ppr",
            draft_type="snake",
        )
    finally:
        client.close()
    row = result.frame.to_dicts()[0]
    assert (row["overall_adp"], row["adp_sd"], row["sample_size"]) == (12.4, 2.1, 456)
    assert json.loads(row["provider_payload"])["player_id"] == 7
    assert cached.from_cache is True
    assert route.call_count == 1


def test_ffc_rejects_inconsistent_pagination(respx_mock: Any, tmp_path: Path) -> None:
    url = f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=10&year=2026"
    payload = _ffc_payload()
    payload["meta"]["count"] = 2
    respx_mock.get(url).mock(return_value=httpx.Response(200, json=payload))
    client = PortableSourceClient(1, tmp_path)
    try:
        with pytest.raises(ProviderError, match="inconsistent"):
            acquire_ffc_adp(
                client,
                season="2026",
                league_size=10,
                scoring_format="full_ppr",
                draft_type="snake",
            )
    finally:
        client.close()


def test_ffc_fetches_and_combines_every_page(respx_mock: Any, tmp_path: Path) -> None:
    url = f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=10&year=2026"
    first = _ffc_payload()
    first["meta"] = {"count": 1, "total_count": 2, "page": 1, "total_pages": 2}
    second = _ffc_payload()
    second["meta"] = {"count": 1, "total_count": 2, "page": 2, "total_pages": 2}
    second["players"][0] = {**second["players"][0], "player_id": 8, "name": "Second Player"}
    respx_mock.get(url).mock(return_value=httpx.Response(200, json=first))
    second_route = respx_mock.get(f"{url}&page=2").mock(
        return_value=httpx.Response(200, json=second)
    )
    client = PortableSourceClient(1, tmp_path)
    try:
        result = acquire_ffc_adp(
            client,
            season="2026",
            league_size=10,
            scoring_format="full_ppr",
            draft_type="snake",
        )
    finally:
        client.close()
    assert result.frame["player_name"].to_list() == ["Portable Player", "Second Player"]
    assert second_route.call_count == 1


def test_ffc_rejects_malformed_payload(respx_mock: Any, tmp_path: Path) -> None:
    url = f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=10&year=2026"
    respx_mock.get(url).mock(return_value=httpx.Response(200, content=b"not-json"))
    client = PortableSourceClient(1, tmp_path)
    try:
        with pytest.raises(ProviderError, match="malformed JSON"):
            acquire_ffc_adp(
                client,
                season="2026",
                league_size=10,
                scoring_format="full_ppr",
                draft_type="snake",
            )
    finally:
        client.close()


@pytest.mark.parametrize(("field", "value"), [("stdev", "bad"), ("times_drafted", 1.5)])
def test_ffc_rejects_malformed_optional_numeric_fields(
    respx_mock: Any, tmp_path: Path, field: str, value: Any
) -> None:
    payload = _ffc_payload()
    payload["players"][0][field] = value
    url = f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=10&year=2026"
    respx_mock.get(url).mock(return_value=httpx.Response(200, json=payload))
    client = PortableSourceClient(1, tmp_path)
    try:
        with pytest.raises(ProviderError) as raised:
            acquire_ffc_adp(
                client,
                season="2026",
                league_size=10,
                scoring_format="full_ppr",
                draft_type="snake",
            )
    finally:
        client.close()
    assert raised.value.details == {"row": 1, "field": field}


def test_provider_timeout_retries_and_fails_safely(respx_mock: Any, tmp_path: Path) -> None:
    route = respx_mock.get(f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=10&year=2026").mock(
        side_effect=httpx.ReadTimeout("secret-token-must-not-leak")
    )
    client = PortableSourceClient(0.01, tmp_path)
    try:
        with pytest.raises(ProviderError) as raised:
            acquire_ffc_adp(
                client,
                season="2026",
                league_size=10,
                scoring_format="full_ppr",
                draft_type="snake",
            )
    finally:
        client.close()
    assert route.call_count == 3
    assert "secret-token" not in raised.value.message


def test_nflverse_schedule_derives_one_bye_per_team(respx_mock: Any, tmp_path: Path) -> None:
    route = respx_mock.get(NFLVERSE_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, content=_valid_schedule_csv(), headers={"etag": "v2026"})
    )
    client = PortableSourceClient(1, tmp_path)
    try:
        result = acquire_nflverse_byes(client, season="2026")
        cached = acquire_nflverse_byes(client, season="2026")
    finally:
        client.close()
    rows = result.frame.to_dicts()
    assert len(rows) == 32
    assert {row["team"] for row in rows} == NFL_TEAMS
    assert sorted(row["bye_week"] for row in rows) == [
        week for week in range(3, 19) for _ in range(2)
    ]
    assert result.source_version == result.payload_hash
    assert cached.from_cache is True
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("2026,1,REG,ARI,XXX", "unknown_nfl_team"),
        ("2026,1,REG,ARI,ATL\n2026,1,REG,ARI,BUF", "ambiguous_schedule"),
    ],
)
def test_nflverse_schedule_rejects_unknown_and_ambiguous_data(
    respx_mock: Any, tmp_path: Path, replacement: str, code: str
) -> None:
    content = ("season,week,game_type,home_team,away_team\n" + replacement + "\n").encode()
    respx_mock.get(NFLVERSE_SCHEDULE_URL).mock(return_value=httpx.Response(200, content=content))
    client = PortableSourceClient(1, tmp_path)
    try:
        with pytest.raises(InputError) as raised:
            acquire_nflverse_byes(client, season="2026")
    finally:
        client.close()
    assert raised.value.code == code


def test_nflverse_schedule_rejects_incomplete_season(respx_mock: Any, tmp_path: Path) -> None:
    content = b"season,week,game_type,home_team,away_team\n2026,1,REG,ARI,ATL\n"
    respx_mock.get(NFLVERSE_SCHEDULE_URL).mock(return_value=httpx.Response(200, content=content))
    client = PortableSourceClient(1, tmp_path)
    try:
        with pytest.raises(InputError) as raised:
            acquire_nflverse_byes(client, season="2026")
    finally:
        client.close()
    assert raised.value.code == "incomplete_schedule"


def test_active_context_is_required_before_bootstrap(tmp_path: Path) -> None:
    from fantasy_war_room.data_bootstrap import active_intelligence_context
    from fantasy_war_room.repository import IntelligenceRepository

    with pytest.raises(Exception) as raised:
        active_intelligence_context(
            Settings(db_path=tmp_path / "empty.duckdb"),
            IntelligenceRepository(tmp_path / "empty.duckdb"),
        )
    assert raised.value.code == "active_league_required"


def test_credentials_are_reported_only_as_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FANTASYPROS_API_KEY", "super-secret-value")
    settings = Settings(
        active_league_id="l1",
        league_contexts={"l1": LeagueContext(league_id="l1", season="2026")},
    )
    serialized = json.dumps(settings.model_dump(mode="json"))
    assert "super-secret-value" not in serialized


def test_active_league_drives_bootstrap_and_unchanged_imports_are_idempotent(
    respx_mock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_recommend_integration import _fixture

    from fantasy_war_room.data_bootstrap import bootstrap_data
    from fantasy_war_room.repository import IntelligenceRepository

    repository = _fixture(tmp_path)
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        league_contexts={
            "league-1": LeagueContext(
                league_id="league-1",
                season="2026",
                ranking_source="rotoworld",
                recommendation_model="baseline-1.0",
            ),
            "other": LeagueContext(
                league_id="other",
                season="2025",
                ranking_source="must-not-leak",
                recommendation_model="trusted-board-1.1",
            ),
        },
    )
    monkeypatch.delenv("FANTASYPROS_API_KEY", raising=False)
    adp_url = f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=2&year=2026"
    adp_route = respx_mock.get(adp_url).mock(
        return_value=httpx.Response(200, json=_ffc_payload(teams=2))
    )
    schedule_route = respx_mock.get(NFLVERSE_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, content=_valid_schedule_csv())
    )

    first = bootstrap_data(
        settings, cache_dir=tmp_path / "cache", repository_root=tmp_path / "project"
    )
    second = bootstrap_data(
        settings, cache_dir=tmp_path / "cache", repository_root=tmp_path / "project"
    )

    assert first["format"] == {
        "league_size": 2,
        "scoring_format": "full_ppr",
        "draft_type": "snake",
    }
    assert first["sources"]["adp"]["status"] == "acquired"
    assert first["sources"]["adp"]["unresolved"] == 1
    assert first["sources"]["team_schedule"]["status"] == "acquired"
    assert first["sources"]["rankings"]["credential"] == "absent"
    assert second["sources"]["adp"]["status"] == "unchanged", second["sources"]["adp"]
    assert second["sources"]["team_schedule"]["status"] == "unchanged"
    assert adp_route.call_count == schedule_route.call_count == 1
    assert first["recommendation_ready"] is True
    assert "must-not-leak" not in json.dumps(first)
    assert len(IntelligenceRepository(repository.path).adp_snapshots()) == 1
    assert len(IntelligenceRepository(repository.path).schedule_snapshots()) == 1
    adp_snapshot = IntelligenceRepository(repository.path).adp_snapshots()[0]
    schedule_snapshot = IntelligenceRepository(repository.path).schedule_snapshots()[0]
    for snapshot in (adp_snapshot, schedule_snapshot):
        assert snapshot.source_uri
        assert snapshot.fetched_at
        assert snapshot.source_payload_hash
        assert snapshot.transformation_version


def test_bootstrap_custom_scoring_still_acquires_schedule(respx_mock: Any, tmp_path: Path) -> None:
    from test_recommend_integration import SCORING, _fixture

    from fantasy_war_room.data_bootstrap import bootstrap_data

    repository = _fixture(tmp_path)
    with duckdb.connect(str(repository.path)) as connection:
        row = connection.execute(
            "SELECT snapshot_id, scoring_context_payload FROM draft_snapshots "
            "ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        context = json.loads(row[1])
        context["scoring_settings"] = {**SCORING, "rec": 0.25}
        connection.execute(
            "UPDATE draft_snapshots SET scoring_context_payload=? WHERE snapshot_id=?",
            [json.dumps(context), row[0]],
        )
    settings = Settings(
        sleeper_username="alice",
        sleeper_user_id="user-1",
        db_path=repository.path,
        active_league_id="league-1",
        league_contexts={"league-1": LeagueContext(league_id="league-1", season="2026")},
    )
    respx_mock.get(NFLVERSE_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, content=_valid_schedule_csv())
    )
    result = bootstrap_data(
        settings, cache_dir=tmp_path / "cache", repository_root=tmp_path / "project"
    )
    assert result["sources"]["adp"]["status"] == "unsupported"
    assert result["sources"]["adp"]["error"]["code"] == "unsupported_adp_scoring_format"
    assert result["sources"]["team_schedule"]["status"] == "acquired"


def test_clean_clone_cli_flow_bootstraps_data_and_generates_codex_config(
    respx_mock: Any,
    runner: Any,
    xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import parse_output
    from test_portability import _register_account
    from test_recommend_integration import ROSTER, SCORING, _fixture

    from fantasy_war_room import cli

    repository = _fixture(tmp_path)
    project = tmp_path / "project"
    monkeypatch.setattr(cli, "REPOSITORY_ROOT", project)
    _register_account(
        respx_mock,
        username="alice",
        user_id="user-1",
        league_ids=["league-1"],
        with_sync=True,
        scoring_settings=SCORING,
        roster_positions=ROSTER,
        team_count=2,
        draft_slot_value=1,
    )
    respx_mock.get(f"{FFC_BASE_URL}/api/v1/adp/ppr?teams=2&year=2026").mock(
        return_value=httpx.Response(200, json=_ffc_payload(teams=2))
    )
    respx_mock.get(NFLVERSE_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, content=_valid_schedule_csv())
    )

    setup = runner.invoke(
        cli.app,
        [
            "setup",
            "--username",
            "alice",
            "--league-id",
            "league-1",
            "--ranking-source",
            "rotoworld",
            "--recommendation-model",
            "baseline-1.0",
            "--db-path",
            str(repository.path),
            "--non-interactive",
            "--json",
        ],
    )
    acquired = runner.invoke(cli.app, ["data", "bootstrap", "--json"])
    ready = runner.invoke(cli.app, ["draft-ready", "--json"])
    configured = runner.invoke(cli.app, ["codex", "configure", "--json"])

    assert setup.exit_code == acquired.exit_code == ready.exit_code == configured.exit_code == 0
    acquired_data = parse_output(acquired)["data"]
    assert acquired_data["sources"]["adp"]["provider"] == "fantasy-football-calculator"
    assert acquired_data["sources"]["team_schedule"]["provider"] == "nflverse"
    assert parse_output(ready)["data"]["ready"] is True
    generated = parse_output(configured)["data"]
    parsed = tomllib.loads(Path(generated["path"]).read_text(encoding="utf-8"))
    args = parsed["mcp_servers"]["fantasy-war-room"]["args"]
    assert args[args.index("--draft-id") + 1] == "draft-league-1"
    assert args[args.index("--draft-slot") + 1] == "1"
    assert args[args.index("--model") + 1] == "baseline-1.0"
    assert args[args.index("--source") + 1] == "rotoworld"
