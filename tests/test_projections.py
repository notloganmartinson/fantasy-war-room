from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

import fantasy_war_room.projections as projections_module
from fantasy_war_room.intelligence import sync_players
from fantasy_war_room.models import Snapshot
from fantasy_war_room.projections import (
    CBS_FILES,
    COMMON_SCHEMAS,
    DST_SCHEMA,
    KICKER_SCHEMA,
    _number,
    calculate_league_points,
    cbs_files,
    import_cbs_projections,
)
from fantasy_war_room.repository import IntelligenceRepository


class PlayerProvider:
    def __init__(self, payload: dict[str, dict[str, Any]]) -> None:
        self.payload = payload

    def get_nfl_players(self) -> dict[str, dict[str, Any]]:
        return self.payload


def test_cbs_dash_is_missing_not_zero() -> None:
    assert _number("—") is None
    assert _number("–") is None


def test_projection_import_uses_requested_season_filenames(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    _change_fixture_season(directory, "2026", "2027")

    snapshot, created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-2027",
        "l1",
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
        season="2027",
    )

    assert created is True
    assert snapshot.season == "2027"
    with duckdb.connect(str(repository.path)) as connection:
        filenames = connection.execute(
            "SELECT original_filename FROM projection_snapshot_sources "
            "WHERE projection_snapshot_id = ? ORDER BY position",
            [snapshot.projection_snapshot_id],
        ).fetchall()
    assert {row[0] for row in filenames} == set(cbs_files("2027").values())


def test_projection_import_does_not_fall_back_to_another_season(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)

    with pytest.raises(Exception, match="qb-2027-ppr.html"):
        import_cbs_projections(directory, repository, "missing-2027", "l1", season="2027")


def test_projection_import_rejects_mislabeled_season_page(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    for position, old_filename in CBS_FILES.items():
        (directory / old_filename).rename(directory / cbs_files("2027")[position])

    with pytest.raises(Exception, match="not a 2027 projection page"):
        import_cbs_projections(directory, repository, "mislabeled-2027", "l1", season="2027")


def test_projection_import_is_atomic_historical_and_scoring_aware(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    first, created, positions = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    duplicate, duplicate_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    original_qb = (directory / CBS_FILES["QB"]).read_text(encoding="utf-8")
    (directory / CBS_FILES["QB"]).write_text(original_qb.replace(">20<", ">21<"), encoding="utf-8")
    changed, changed_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    (directory / CBS_FILES["QB"]).write_text(original_qb, encoding="utf-8")
    repeated, repeated_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 6, tzinfo=UTC),
    )

    assert created is True and duplicate_created is False
    assert duplicate.projection_snapshot_id == first.projection_snapshot_id
    assert changed_created is repeated_created is True
    assert first.payload_hash == duplicate.payload_hash == repeated.payload_hash
    assert first.payload_hash != changed.payload_hash
    assert first.total_row_count == 6
    assert first.matched_row_count == 5
    assert first.unresolved_row_count == 1 and first.ambiguous_row_count == 0
    assert {row["position"] for row in positions} == set(CBS_FILES)
    assert repository.projection_at(datetime(2026, 1, 4, tzinfo=UTC)) == first
    assert repository.projection_at(datetime(2026, 1, 5, 12, tzinfo=UTC)) == changed
    assert repository.projection_at(datetime(2026, 1, 6, tzinfo=UTC)) == repeated

    with duckdb.connect(str(repository.path)) as connection:
        qb = connection.execute(
            "SELECT league_known_component_points, league_projected_points, "
            "scoring_completeness, cbs_projected_points FROM projection_entries "
            "WHERE projection_snapshot_id = ? AND source_position = 'QB'",
            [first.projection_snapshot_id],
        ).fetchone()
        kicker = connection.execute(
            "SELECT field_goals_made_50_plus, extra_points_made "
            "FROM projection_kicker_stats WHERE projection_snapshot_id = ?",
            [first.projection_snapshot_id],
        ).fetchone()
        dst = connection.execute(
            "SELECT sacks, points_allowed FROM projection_dst_stats "
            "WHERE projection_snapshot_id = ?",
            [first.projection_snapshot_id],
        ).fetchone()
        sources = connection.execute(
            "SELECT position, row_count, length(source_page_hash) "
            "FROM projection_snapshot_sources WHERE projection_snapshot_id = ?",
            [first.projection_snapshot_id],
        ).fetchall()
    assert qb == (16.0, None, "partial", 20.0)
    assert kicker == (2.0, 30.0)
    assert dst == (40.0, 300.0)
    assert len(sources) == 6 and all(row[1:] == (1, 64) for row in sources)
    issues = repository.projection_issues(first.projection_snapshot_id)
    assert [(issue.source_position, issue.source_player_name) for issue in issues] == [
        ("WR", "Missing Receiver")
    ]


def test_projection_out_of_order_dedup_preserves_as_of_timeline(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    january_1 = datetime(2026, 1, 1, tzinfo=UTC)
    january_2 = datetime(2026, 1, 2, tzinfo=UTC)
    january_5 = datetime(2026, 1, 5, tzinfo=UTC)
    original_qb = (directory / CBS_FILES["QB"]).read_text(encoding="utf-8")

    state_a, a_created, _ = import_cbs_projections(
        directory, repository, "fixture-v1", "l1", observed_at=january_1
    )
    (directory / CBS_FILES["QB"]).write_text(original_qb.replace(">20<", ">21<"), encoding="utf-8")
    state_b_late, late_created, _ = import_cbs_projections(
        directory, repository, "fixture-v1", "l1", observed_at=january_5
    )
    state_b_backfill, backfill_created, _ = import_cbs_projections(
        directory, repository, "fixture-v1", "l1", observed_at=january_2
    )

    assert a_created and late_created and backfill_created
    assert state_a.payload_hash != state_b_backfill.payload_hash
    assert state_b_backfill.payload_hash == state_b_late.payload_hash
    assert repository.projection_at(january_1) == state_a
    assert repository.projection_at(january_2) == state_b_backfill
    assert repository.projection_at(datetime(2026, 1, 4, tzinfo=UTC)) == state_b_backfill
    assert repository.projection_at(january_5) == state_b_late


def test_projection_out_of_order_duplicate_uses_preceding_state(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    january_1 = datetime(2026, 1, 1, tzinfo=UTC)
    january_4 = datetime(2026, 1, 4, tzinfo=UTC)
    january_5 = datetime(2026, 1, 5, tzinfo=UTC)
    original_qb = (directory / CBS_FILES["QB"]).read_text(encoding="utf-8")

    state_a, _, _ = import_cbs_projections(
        directory, repository, "fixture-v1", "l1", observed_at=january_1
    )
    duplicate_a, duplicate_created, duplicate_positions = import_cbs_projections(
        directory, repository, "fixture-v1", "l1", observed_at=january_4
    )
    (directory / CBS_FILES["QB"]).write_text(original_qb.replace(">20<", ">21<"), encoding="utf-8")
    state_b, _, _ = import_cbs_projections(
        directory, repository, "fixture-v1", "l1", observed_at=january_5
    )

    assert duplicate_created is False
    assert duplicate_a.projection_snapshot_id == state_a.projection_snapshot_id
    assert {row["position"] for row in duplicate_positions} == set(CBS_FILES)
    assert repository.projection_at(january_4) == state_a
    assert repository.projection_at(january_5) == state_b
    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute("SELECT count(*) FROM projection_snapshots").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM projection_entries WHERE projection_snapshot_id = ?",
            [duplicate_a.projection_snapshot_id],
        ).fetchone() == (6,)


def test_projection_calculator_version_creates_recalculated_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, directory = _projection_context(tmp_path)
    original_calculator = projections_module.calculate_league_points
    original, original_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    same, same_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )

    def upgraded_calculator(
        row: dict[str, Any], scoring: dict[str, float]
    ) -> tuple[float, float | None, str, list[str]]:
        known, exact, completeness, missing = original_calculator(row, scoring)
        return known + 1.0, exact + 1.0 if exact is not None else None, completeness, missing

    monkeypatch.setattr(projections_module, "SCORING_CALCULATOR_VERSION", "2.0")
    monkeypatch.setattr(projections_module, "calculate_league_points", upgraded_calculator)
    upgraded, upgraded_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    upgraded_duplicate, duplicate_created, _ = import_cbs_projections(
        directory,
        repository,
        "fixture-v1",
        "l1",
        observed_at=datetime(2026, 1, 6, tzinfo=UTC),
    )

    assert original_created is True and same_created is False
    assert same.projection_snapshot_id == original.projection_snapshot_id
    assert upgraded_created is True and duplicate_created is False
    assert upgraded.projection_snapshot_id != original.projection_snapshot_id
    assert upgraded_duplicate.projection_snapshot_id == upgraded.projection_snapshot_id
    assert upgraded.scoring_calculator_version == "2.0"
    assert repository.projection_at(datetime(2026, 1, 4, tzinfo=UTC)) == original
    assert repository.projection_at(datetime(2026, 1, 5, tzinfo=UTC)) == upgraded
    with duckdb.connect(str(repository.path)) as connection:
        values = connection.execute(
            "SELECT projection_snapshot_id, league_known_component_points "
            "FROM projection_entries WHERE projection_snapshot_id IN (?, ?) "
            "AND source_position = 'QB' ORDER BY league_known_component_points",
            [original.projection_snapshot_id, upgraded.projection_snapshot_id],
        ).fetchall()
    assert values == [
        (original.projection_snapshot_id, 16.0),
        (upgraded.projection_snapshot_id, 17.0),
    ]


@pytest.mark.parametrize(
    ("position", "statistic", "base_key", "bonus_keys"),
    [
        (
            "QB",
            "passing_yards",
            "pass_yd",
            ["bonus_pass_yd_300", "bonus_pass_yd_400", "bonus_pass_cmp_25"],
        ),
        (
            "RB",
            "rushing_yards",
            "rush_yd",
            ["bonus_rush_yd_100", "bonus_rush_yd_200", "bonus_rush_att_20"],
        ),
        (
            "WR",
            "receiving_yards",
            "rec_yd",
            ["bonus_rec_yd_100", "bonus_rec_yd_200", "bonus_rec_10"],
        ),
        ("TE", "receiving_yards", "rec_yd", ["bonus_rec_yd_100", "bonus_rec_te"]),
    ],
)
def test_offensive_bonus_scoring_is_explicitly_partial(
    position: str, statistic: str, base_key: str, bonus_keys: list[str]
) -> None:
    row = {"source_position": position, statistic: 100.0}
    scoring = {base_key: 0.1, **dict.fromkeys(bonus_keys, 1.0)}

    known, exact, completeness, missing = calculate_league_points(row, scoring)

    assert known == 10.0
    assert exact is None
    assert completeness == "partial"
    assert missing == sorted(bonus_keys)


def test_projection_import_rolls_back_when_one_page_is_invalid(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    (directory / CBS_FILES["TE"]).write_text("<html>invalid</html>", encoding="utf-8")
    with pytest.raises(Exception, match="not a 2026 projection page"):
        import_cbs_projections(directory, repository, "broken", "l1")
    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute("SELECT count(*) FROM projection_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM projection_entries").fetchone() == (0,)


def test_projection_json_list_and_issues_commands(tmp_path: Path) -> None:
    repository, directory = _projection_context(tmp_path)
    from conftest import parse_output

    from fantasy_war_room.cli import app

    runner = CliRunner()
    imported = runner.invoke(
        app,
        [
            "projections",
            "import-cbs",
            str(directory),
            "--source-version",
            "fixture-v1",
            "--league-id",
            "l1",
            "--observed-at",
            datetime(2026, 1, 3, tzinfo=UTC).isoformat(),
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert imported.exit_code == 0, imported.output
    imported_body = parse_output(imported)
    assert imported_body["command"] == "projections import-cbs"
    assert imported_body["data"]["snapshot"]["total_row_count"] == 6
    snapshot_id = imported_body["data"]["snapshot"]["projection_snapshot_id"]
    listed = runner.invoke(
        app, ["projections", "list", "--db-path", str(repository.path), "--json"]
    )
    issues = runner.invoke(
        app,
        [
            "projections",
            "issues",
            "--snapshot-id",
            snapshot_id,
            "--db-path",
            str(repository.path),
            "--json",
        ],
    )
    assert listed.exit_code == issues.exit_code == 0
    assert parse_output(listed)["command"] == "projections list"
    assert parse_output(listed)["data"]["snapshots"][0]["source"] == "cbs"
    assert parse_output(issues)["data"]["issues"][0]["source_player_name"] == "Missing Receiver"


def _projection_context(tmp_path: Path) -> tuple[IntelligenceRepository, Path]:
    repository = IntelligenceRepository(tmp_path / "projections.duckdb")
    observed = datetime(2025, 12, 30, tzinfo=UTC)
    payload = {
        "qb1": {"full_name": "Quarter Back", "position": "QB", "team": "BUF"},
        "rb1": {"full_name": "Running Back", "position": "RB", "team": "JAX"},
        "te1": {"full_name": "Tight End", "position": "TE", "team": "SEA"},
        "k1": {"full_name": "Place Kicker", "position": "K", "team": "DAL"},
        "BUF": {
            "first_name": "Buffalo",
            "last_name": "Bills",
            "full_name": "Buffalo Bills",
            "position": "DEF",
            "team": "BUF",
        },
    }
    sync_players(
        PlayerProvider(payload), repository, tmp_path / "cache", force=True, observed_at=observed
    )
    repository.insert(
        Snapshot(
            snapshot_id="league-snapshot",
            league_id="l1",
            draft_id="d1",
            observed_at=datetime(2025, 12, 31, tzinfo=UTC),
            source_updated_at=None,
            payload_hash="league-hash",
            pick_count=0,
            league={"league_id": "l1", "scoring_settings": _scoring_settings()},
            draft={"draft_id": "d1"},
            picks=[],
        )
    )
    directory = tmp_path / "cbs"
    directory.mkdir()
    rows = {
        "QB": ("Quarter Back", "BUF", [17, 10, 8, 100, 5.9, 2, 1, 90, 2, 10, 5, 1, 1, 20, 1.2]),
        "RB": ("Running Back", "JAC", [17, 100, 500, 5, 5, 50, 40, 300, 17.6, 7.5, 2, 1, 100, 5.9]),
        "WR": ("Missing Receiver", "BUF", [17, 80, 50, 700, 41.2, 14, 5, 2, 10, 5, 1, 1, 120, 7.1]),
        "TE": ("Tight End", "SEA", [17, 70, 50, 600, 35.3, 12, 4, 1, 100, 5.9]),
        "K": (
            "Place Kicker",
            "DAL",
            [17, 30, 32, 55, 1, 1, 5, 5, 8, 8, 14, 15, 2, 3, 30, 31, 120, 7.1],
        ),
        "DST": (
            "Buffalo",
            "BUF",
            [10, 1, 40, 500, 8, 10, 3, 300, 17.6, 3500, 1800, 5300, 311.8, 100, 5.9],
        ),
    }
    for position, filename in CBS_FILES.items():
        name, team, values = rows[position]
        schema = (
            DST_SCHEMA
            if position == "DST"
            else KICKER_SCHEMA
            if position == "K"
            else COMMON_SCHEMAS[position]
        )
        assert len(values) == len(schema)
        (directory / filename).write_text(_page(position, name, team, values), encoding="utf-8")
    return repository, directory


def _page(position: str, name: str, team: str, values: list[float]) -> str:
    schema = (
        DST_SCHEMA
        if position == "DST"
        else KICKER_SCHEMA
        if position == "K"
        else COMMON_SCHEMAS[position]
    )
    identity = (
        f'<a href="/nfl/teams/{team}/team/">{name}</a>'
        if position == "DST"
        else '<span class="CellPlayerName--long"><a>'
        + name
        + f'</a><span class="CellPlayerName-position">{position}</span>'
        + f'<span class="CellPlayerName-team">{team}</span></span>'
    )
    cells = "<td>" + identity + "</td>" + "".join(f"<td>{value:g}</td>" for value in values)
    headers = "<th><a>Player</a></th>" + "".join(f"<th><a>{field}</a></th>" for field in schema)
    return (
        f"<html><head><title>2026 Projections Fantasy Football Stats - {position} Points"
        " - CBS Sports</title></head><body>"
        f"<option selected>{position}</option><option selected>2026 Projections</option>"
        '<option selected>PPR</option><table class="TableBase-table"><thead>'
        f'<tr class="TableBase-headTr">{headers}</tr></thead>'
        f"<tbody><tr>{cells}</tr></tbody></table></body></html>"
    )


def _change_fixture_season(directory: Path, old_season: str, new_season: str) -> None:
    for position, old_filename in cbs_files(old_season).items():
        old_path = directory / old_filename
        new_path = directory / cbs_files(new_season)[position]
        new_path.write_text(
            old_path.read_text(encoding="utf-8").replace(old_season, new_season),
            encoding="utf-8",
        )
        old_path.unlink()


def _scoring_settings() -> dict[str, float]:
    return {
        "pass_yd": 0.04,
        "pass_td": 4.0,
        "pass_int": -1.0,
        "pass_2pt": 2.0,
        "rush_yd": 0.1,
        "rush_td": 6.0,
        "rush_2pt": 2.0,
        "rec": 1.0,
        "rec_yd": 0.1,
        "rec_td": 6.0,
        "rec_2pt": 2.0,
        "fum_lost": -2.0,
        "fgm_0_19": 3.0,
        "fgm_20_29": 3.0,
        "fgm_30_39": 3.0,
        "fgm_40_49": 4.0,
        "fgm_50_59": 5.0,
        "fgm_60p": 6.0,
        "fgmiss": -1.0,
        "xpm": 1.0,
        "xpmiss": -1.0,
        "int": 2.0,
        "safe": 2.0,
        "sack": 1.0,
        "fum_rec": 2.0,
        "ff": 1.0,
        "def_td": 6.0,
        "pts_allow_0": 10.0,
        "st_td": 6.0,
        "blk_kick": 2.0,
    }
