from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from fantasy_war_room.errors import DataIntegrityError, InputError
from fantasy_war_room.intelligence import import_rankings, normalize_name, sync_players
from fantasy_war_room.repository import IntelligenceRepository, MigrationError
from fantasy_war_room.services import sync


class PlayerProvider:
    def __init__(self, payload: dict[str, dict[str, Any]]) -> None:
        self.payload = payload
        self.calls = 0

    def get_nfl_players(self) -> dict[str, dict[str, Any]]:
        self.calls += 1
        return self.payload


class DraftProvider:
    def __init__(self, picks: list[dict[str, Any]]) -> None:
        self.picks = picks

    def get_league(self, league_id: str) -> dict[str, Any]:
        return {"league_id": league_id, "name": "Synthetic"}

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return [{"draft_id": "d1", "created": 1}]

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return {"draft_id": draft_id, "status": "drafting"}

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return self.picks


def player_payload() -> dict[str, dict[str, Any]]:
    return {
        "p1": {
            "first_name": "Alpha",
            "last_name": "Quarterback",
            "position": "QB",
            "fantasy_positions": ["QB"],
            "team": "ARI",
            "active": True,
            "gsis_id": "g1",
        },
        "p2": {
            "first_name": "Bravo",
            "last_name": "Receiver",
            "position": "WR",
            "fantasy_positions": ["WR"],
            "team": "BUF",
            "active": True,
        },
        "p3": {
            "first_name": "Alex",
            "last_name": "Same",
            "position": "RB",
            "fantasy_positions": ["RB"],
            "team": "NYJ",
        },
        "p4": {
            "first_name": "Alex",
            "last_name": "Same",
            "position": "RB",
            "fantasy_positions": ["RB"],
            "team": "NYG",
        },
    }


def test_bulk_player_ingestion_preserves_identity_mapping_and_observations(
    tmp_path: Path,
) -> None:
    repository = IntelligenceRepository(tmp_path / "bulk.duckdb")
    payload = {
        f"p{index}": {
            "first_name": f"First{index}",
            "last_name": f"Last{index}",
            "position": "WR",
            "fantasy_positions": ["WR"],
            "team": "ARI",
            "gsis_id": f"g{index}",
        }
        for index in range(2_000)
    }
    timings: dict[str, float] = {}
    first, created, _ = sync_players(
        PlayerProvider(payload),
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        timings=timings,
    )
    with duckdb.connect(str(repository.path)) as connection:
        identities_before = dict(
            connection.execute(
                "SELECT provider_player_id, canonical_player_id FROM player_provider_ids "
                "WHERE provider = 'sleeper'"
            ).fetchall()
        )
        counts = tuple(
            connection.execute(
                "SELECT (SELECT count(*) FROM canonical_players), "
                "(SELECT count(*) FROM player_provider_ids), "
                "(SELECT count(*) FROM player_observations)"
            ).fetchone()
        )
    payload["p0"] = {**payload["p0"], "team": "ATL"}
    second, changed, _ = sync_players(
        PlayerProvider(payload),
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    with duckdb.connect(str(repository.path)) as connection:
        identities_after = dict(
            connection.execute(
                "SELECT provider_player_id, canonical_player_id FROM player_provider_ids "
                "WHERE provider = 'sleeper'"
            ).fetchall()
        )
        observations = connection.execute("SELECT count(*) FROM player_observations").fetchone()
    assert created is changed is True
    assert first.snapshot_id != second.snapshot_id
    assert counts == (2_000, 4_000, 2_000)
    assert identities_before == identities_after
    assert observations == (4_000,)
    assert set(timings) == {
        "cache_read_or_network_download",
        "parsing_and_normalization",
        "identity_resolution",
        "database_persistence",
        "total",
    }


def test_player_cache_dedup_force_and_a_b_a(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "history.duckdb")
    provider = PlayerProvider(player_payload())
    cache = tmp_path / "cache"
    first_at = datetime(2026, 1, 1, tzinfo=UTC)

    first, created, source = sync_players(provider, repository, cache, observed_at=first_at)
    duplicate, duplicate_created, duplicate_source = sync_players(
        provider, repository, cache, observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC)
    )
    assert (created, source, provider.calls) == (True, "network", 1)
    assert duplicate_created is False and duplicate_source == "cache"
    assert first.payload_hash == duplicate.payload_hash

    state_a = provider.payload
    provider.payload = {**state_a, "p5": {"full_name": "Charlie Runner", "position": "RB"}}
    second, second_created, _ = sync_players(
        provider,
        repository,
        cache,
        force=True,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    provider.payload = state_a
    third, third_created, _ = sync_players(
        provider,
        repository,
        cache,
        force=True,
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    assert second_created is third_created is True
    assert first.payload_hash == third.payload_hash != second.payload_hash
    assert provider.calls == 3


def test_player_metadata_search_is_as_of(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "history.duckdb")
    payload = player_payload()
    provider = PlayerProvider(payload)
    sync_players(
        provider,
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload["p1"] = {**payload["p1"], "team": "ATL", "injury_status": "Questionable"}
    sync_players(
        provider,
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    old = repository.search_players(
        normalize_name("ALPHA quarterback"), datetime(2026, 1, 2, tzinfo=UTC)
    )
    new = repository.search_players(
        normalize_name("alpha"), datetime(2026, 1, 4, tzinfo=UTC), team="ATL"
    )
    assert old[0].team == "ARI"
    assert new[0].team == "ATL" and new[0].injury_status == "Questionable"


def test_source_collisions_are_preserved_and_resolved_safely(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "collisions.duckdb")
    payload = {
        "same-1": {
            "full_name": "Same Player",
            "position": "S",
            "team": None,
            "espn_id": "shared-safe",
        },
        "same-2": {
            "full_name": "Same Player",
            "position": "DB",
            "team": "BUF",
            "espn_id": "shared-safe",
        },
        "unrelated-1": {
            "full_name": "Alpha Different",
            "position": "WR",
            "sportradar_id": "shared-unsafe",
        },
        "unrelated-2": {
            "full_name": "Bravo Other",
            "position": "CB",
            "sportradar_id": "shared-unsafe",
        },
        "duplicate-row-1": {"full_name": "Identical Row", "position": "TE"},
        "duplicate-row-2": {"full_name": "Identical Row", "position": "TE"},
    }
    diagnostics: dict[str, Any] = {}
    sync_players(
        PlayerProvider(payload),
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 6, 1, tzinfo=UTC),
        diagnostics=diagnostics,
    )
    with duckdb.connect(str(repository.path)) as connection:
        observations = connection.execute(
            "SELECT provider_player_id, canonical_player_id FROM player_observations "
            "ORDER BY provider_player_id"
        ).fetchall()
        sleeper_mappings = connection.execute(
            "SELECT provider_player_id, canonical_player_id FROM player_provider_ids "
            "WHERE provider = 'sleeper' ORDER BY provider_player_id"
        ).fetchall()
        unsafe_mapping = connection.execute(
            "SELECT count(*) FROM player_provider_ids WHERE provider = 'sportradar' "
            "AND provider_player_id = 'shared-unsafe'"
        ).fetchone()
    canonical_by_source = dict(observations)
    assert len(observations) == len(sleeper_mappings) == 6
    assert canonical_by_source["same-1"] == canonical_by_source["same-2"]
    assert canonical_by_source["unrelated-1"] != canonical_by_source["unrelated-2"]
    assert canonical_by_source["duplicate-row-1"] != canonical_by_source["duplicate-row-2"]
    assert unsafe_mapping == (0,)
    assert diagnostics["merged_collision_count"] == 1
    assert diagnostics["quarantined_collision_count"] == 1

    rankings = tmp_path / "collision-rankings.csv"
    rankings.write_text(
        "player_name,sleeper_id,position,overall_rank\nSame Player,same-2,DB,1\n",
        encoding="utf-8",
    )
    ranking, _ = import_rankings(
        rankings,
        repository,
        "collision-fixture",
        "2026",
        "ppr",
        10,
        observed_at=datetime(2026, 6, 1, 1, tzinfo=UTC),
    )
    assert ranking.matched_row_count == 1
    sync(
        DraftProvider(
            [
                {
                    "pick_no": 1,
                    "player_id": "same-1",
                    "metadata": {"first_name": "Same", "last_name": "Player"},
                }
            ]
        ),
        repository,
        "l1",
        observed_at=datetime(2026, 6, 1, 2, tzinfo=UTC),
    )
    assert repository.board(datetime(2026, 6, 1, 3, tzinfo=UTC), "d1", "l1", None, None, 100) == []
    sync(
        DraftProvider(
            [
                {
                    "pick_no": 1,
                    "player_id": "same-2",
                    "metadata": {"first_name": "Same", "last_name": "Player"},
                }
            ]
        ),
        repository,
        "l1",
        observed_at=datetime(2026, 6, 1, 4, tzinfo=UTC),
    )
    assert repository.board(datetime(2026, 6, 1, 5, tzinfo=UTC), "d1", "l1", None, None, 100) == []

    payload["same-1"] = {**payload["same-1"], "team": "BUF"}
    sync_players(
        PlayerProvider(payload),
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    with duckdb.connect(str(repository.path)) as connection:
        later = dict(
            connection.execute(
                "SELECT provider_player_id, canonical_player_id FROM player_provider_ids "
                "WHERE provider = 'sleeper'"
            ).fetchall()
        )
    assert later == dict(sleeper_mappings)


def test_conflicting_existing_canonical_mappings_roll_back(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "unsafe.duckdb")
    repository.initialize()
    observed_at = datetime(2026, 6, 1, tzinfo=UTC)
    with duckdb.connect(str(repository.path)) as connection:
        connection.execute(
            "INSERT INTO canonical_players VALUES ('c1', ?), ('c2', ?)",
            [observed_at, observed_at],
        )
        connection.execute(
            "INSERT INTO player_provider_ids VALUES ('c1','gsis','g1',?), ('c2','espn','e1',?)",
            [observed_at, observed_at],
        )
    payload = {
        "p1": {
            "full_name": "Unsafe Player",
            "position": "WR",
            "gsis_id": "g1",
            "espn_id": "e1",
        }
    }
    with pytest.raises(DataIntegrityError, match="multiple existing canonical players"):
        sync_players(PlayerProvider(payload), repository, tmp_path / "cache", force=True)
    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute("SELECT count(*) FROM player_directory_snapshots").fetchone() == (
            0,
        )
        assert connection.execute("SELECT count(*) FROM player_observations").fetchone() == (0,)


def test_ranking_resolution_issues_and_dedup(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "history.duckdb")
    sync_players(
        PlayerProvider(player_payload()),
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    csv_path = tmp_path / "rankings.csv"
    csv_path.write_text(
        "player_name,sleeper_id,gsis_id,position,team,overall_rank\n"
        "Wrong Name,p1,,QB,ARI,1\n"
        "Alpha Quarterback,,g1,QB,ARI,2\n"
        "Bravo Receiver,,,WR,BUF,3\n"
        "Alex Same,,,RB,,4\n"
        "Missing Player,,,TE,DAL,5\n",
        encoding="utf-8",
    )
    at = datetime(2026, 1, 2, tzinfo=UTC)
    first, created = import_rankings(
        csv_path, repository, "synthetic", "2026", "ppr", 10, observed_at=at
    )
    duplicate, duplicate_created = import_rankings(
        csv_path,
        repository,
        "synthetic",
        "2026",
        "ppr",
        10,
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert created is True and duplicate_created is False
    assert first.payload_hash == duplicate.payload_hash
    assert first.matched_row_count == 3
    assert first.ambiguous_row_count == 1 and first.unresolved_row_count == 1
    issues = repository.ranking_issues(first.ranking_snapshot_id)
    assert [issue.match_status for issue in issues] == ["ambiguous", "unresolved"]
    assert len(issues[0].candidate_player_ids) == 2


def test_malformed_numeric_csv_is_rejected(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("player_name,overall_rank\nAlpha Quarterback,first\n", encoding="utf-8")
    with pytest.raises(InputError, match="invalid overall_rank"):
        import_rankings(
            csv_path,
            IntelligenceRepository(tmp_path / "history.duckdb"),
            "synthetic",
            "2026",
            "ppr",
            10,
        )


def test_ranking_a_b_a_and_board_as_of_provenance(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "history.duckdb")
    cache = tmp_path / "cache"
    sync_players(
        PlayerProvider(player_payload()),
        repository,
        cache,
        force=True,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fixture = Path(__file__).parent / "fixtures" / "synthetic_rankings.csv"
    first, _ = import_rankings(
        fixture,
        repository,
        "fixture",
        "2026",
        "ppr",
        10,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    changed = tmp_path / "changed.csv"
    changed.write_text(fixture.read_text(encoding="utf-8").replace("2.5", "1.5"), encoding="utf-8")
    second, _ = import_rankings(
        changed,
        repository,
        "fixture",
        "2026",
        "ppr",
        10,
        observed_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    third, third_created = import_rankings(
        fixture,
        repository,
        "fixture",
        "2026",
        "ppr",
        10,
        observed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    assert third_created is True
    assert first.payload_hash == third.payload_hash != second.payload_hash

    draft_provider = DraftProvider([])
    early_draft, _ = sync(draft_provider, repository, "l1", datetime(2026, 1, 2, 12, tzinfo=UTC))
    before_pick = repository.board(
        datetime(2026, 1, 2, 13, tzinfo=UTC), "d1", None, "fixture", None, 100
    )
    draft_provider.picks = [{"pick_no": 1, "player_id": "p1"}]
    late_draft, _ = sync(draft_provider, repository, "l1", datetime(2026, 1, 5, tzinfo=UTC))
    after_pick = repository.board(
        datetime(2026, 1, 6, tzinfo=UTC), "d1", None, "fixture", None, 100
    )

    assert [player.sleeper_player_id for player in before_pick] == ["p1", "p2"]
    assert [player.sleeper_player_id for player in after_pick] == ["p2"]
    assert before_pick[0].draft_snapshot_id == early_draft.snapshot_id
    assert after_pick[0].draft_snapshot_id == late_draft.snapshot_id
    assert before_pick[0].ranking_snapshot_id == first.ranking_snapshot_id
    assert after_pick[0].ranking_snapshot_id == third.ranking_snapshot_id
    assert before_pick[0].player_snapshot_id


def test_m2_migration_preserves_m1_rows(tmp_path: Path) -> None:
    path = tmp_path / "m1.duckdb"
    from fantasy_war_room.repository import MIGRATION_1, MIGRATION_2

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        connection.execute(MIGRATION_1)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute(MIGRATION_2)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        connection.execute(
            "INSERT INTO draft_snapshots VALUES "
            "('s1','l1','d1','2026-01-01T00:00:00Z',NULL,'h',0,'{}','{}','[]')"
        )
    IntelligenceRepository(path).initialize()
    with duckdb.connect(str(path)) as connection:
        assert connection.execute("SELECT snapshot_id FROM draft_snapshots").fetchall() == [("s1",)]
        assert connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1, "initial_m1_schema"),
            (2, "repeatable_draft_states"),
            (3, "m2_player_intelligence"),
            (4, "m2_observation_schema_alignment"),
            (5, "m2_player_source_observations"),
        ]
        assert connection.execute("SELECT count(*) FROM player_observations").fetchone() == (0,)


def test_migration_four_upgrades_exact_deployed_m2_schema_and_preserves_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployed-m2.duckdb"
    _create_deployed_m2_database(path)
    observed_at = datetime(2026, 2, 1, tzinfo=UTC)
    with duckdb.connect(str(path)) as connection:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info('player_observations')").fetchall()
        ]
        assert columns == _DEPLOYED_PLAYER_OBSERVATION_COLUMNS
        connection.execute(
            "INSERT INTO player_directory_snapshots VALUES "
            "('ps1','sleeper','nfl',?,?, 'hash',1,'/tmp/cache','1.0')",
            [observed_at, observed_at],
        )
        connection.execute("INSERT INTO canonical_players VALUES ('cp1', ?)", [observed_at])
        connection.execute(
            "INSERT INTO player_observations ("
            "snapshot_id, canonical_player_id, provider_player_id, first_name, last_name, "
            "normalized_full_name, position, fantasy_positions, team, active, status, "
            "injury_status, years_experience, provider_ids, raw_payload) VALUES "
            "('ps1','cp1','p1','Alpha','Player','alpha player','QB','[\"QB\"]',"
            "'ARI',true,'Active',NULL,2,'{\"sleeper\":\"p1\"}','{}')"
        )

    IntelligenceRepository(path).initialize()

    with duckdb.connect(str(path)) as connection:
        row = connection.execute(
            "SELECT canonical_player_id, observed_at FROM player_observations"
        ).fetchone()
        assert row == ("cp1", observed_at)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,)]


def test_fresh_and_upgraded_player_observation_schemas_are_equivalent(tmp_path: Path) -> None:
    fresh = IntelligenceRepository(tmp_path / "fresh.duckdb")
    upgraded_path = tmp_path / "upgraded.duckdb"
    _create_deployed_m2_database(upgraded_path)
    upgraded = IntelligenceRepository(upgraded_path)
    fresh.initialize()
    upgraded.initialize()

    assert _table_schema(fresh.path, "player_observations") == _table_schema(
        upgraded.path, "player_observations"
    )
    assert _table_schema(fresh.path, "ranking_match_issues") == _table_schema(
        upgraded.path, "ranking_match_issues"
    )


def test_player_insert_uses_explicit_aligned_columns(tmp_path: Path) -> None:
    repository = IntelligenceRepository(tmp_path / "history.duckdb")
    observed_at = datetime(2026, 3, 1, tzinfo=UTC)
    sync_players(
        PlayerProvider(player_payload()),
        repository,
        tmp_path / "cache",
        force=True,
        observed_at=observed_at,
    )

    with duckdb.connect(str(repository.path)) as connection:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info('player_observations')").fetchall()
        ]
        row = connection.execute(
            "SELECT provider_player_id, observed_at, first_name, last_name, "
            "normalized_full_name, position, team FROM player_observations "
            "WHERE provider_player_id = 'p1'"
        ).fetchone()
    assert columns == _FINAL_PLAYER_OBSERVATION_COLUMNS
    assert row == (
        "p1",
        observed_at,
        "Alpha",
        "Quarterback",
        "alpha quarterback",
        "QB",
        "ARI",
    )


def test_player_persistence_rolls_back_and_retry_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = IntelligenceRepository(tmp_path / "history.duckdb")
    provider = PlayerProvider(player_payload())
    cache = tmp_path / "cache"
    original = repository._bulk_insert_player_rows

    def fail_after_bulk_insert(
        connection: duckdb.DuckDBPyConnection,
        canonical_rows: list[dict[str, Any]],
        mapping_rows: list[dict[str, Any]],
        observation_rows: list[dict[str, Any]],
    ) -> None:
        original(connection, canonical_rows, mapping_rows, observation_rows)
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(repository, "_bulk_insert_player_rows", fail_after_bulk_insert)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        sync_players(
            provider,
            repository,
            cache,
            force=True,
            observed_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
    with duckdb.connect(str(repository.path)) as connection:
        for table in (
            "player_directory_snapshots",
            "canonical_players",
            "player_provider_ids",
            "player_observations",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)

    monkeypatch.setattr(repository, "_bulk_insert_player_rows", original)
    snapshot, created, source = sync_players(
        provider,
        repository,
        cache,
        observed_at=datetime(2026, 4, 1, 1, tzinfo=UTC),
    )
    assert created is True and source == "cache" and snapshot.player_count == 4
    assert provider.calls == 1
    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute("SELECT count(*) FROM player_directory_snapshots").fetchone() == (
            1,
        )
        assert connection.execute("SELECT count(*) FROM player_observations").fetchone() == (4,)


def test_migration_four_rejects_orphaned_player_observation_without_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orphaned-player.duckdb"
    _create_deployed_m2_database(path)
    _insert_orphaned_player_observation(path)
    before = _database_state(path)

    with pytest.raises(MigrationError) as raised:
        IntelligenceRepository(path).initialize()

    message = str(raised.value)
    assert "orphaned player_observations count=1" in message
    assert "missing-player-snapshot" in message and "orphan-player" in message
    assert _database_state(path) == before
    assert _migration_versions(path) == [1, 2, 3]


def test_migration_four_rejects_unmatched_ranking_issue_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "unmatched-ranking.duckdb"
    _create_deployed_m2_database(path)
    _insert_unmatched_ranking_issue(path)
    before = _database_state(path)

    with pytest.raises(MigrationError) as raised:
        IntelligenceRepository(path).initialize()

    message = str(raised.value)
    assert "unmatched ranking_match_issues count=1" in message
    assert "orphan-ranking" in message and "7" in message
    assert _database_state(path) == before
    assert _migration_versions(path) == [1, 2, 3]


def test_migration_four_reports_both_inconsistencies_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "both-inconsistent.duckdb"
    _create_deployed_m2_database(path)
    _insert_orphaned_player_observation(path)
    _insert_unmatched_ranking_issue(path)
    before = _database_state(path)

    with pytest.raises(MigrationError) as raised:
        IntelligenceRepository(path).initialize()

    message = str(raised.value)
    assert "orphaned player_observations count=1" in message
    assert "unmatched ranking_match_issues count=1" in message
    assert _database_state(path) == before
    assert _migration_versions(path) == [1, 2, 3]


def test_migration_four_succeeds_after_inconsistent_fixture_is_corrected(tmp_path: Path) -> None:
    path = tmp_path / "corrected.duckdb"
    _create_deployed_m2_database(path)
    _insert_orphaned_player_observation(path)
    _insert_unmatched_ranking_issue(path)
    with pytest.raises(MigrationError):
        IntelligenceRepository(path).initialize()

    observed_at = datetime(2026, 5, 1, tzinfo=UTC)
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "INSERT INTO player_directory_snapshots ("
            "snapshot_id, provider, sport, observed_at, fetched_at, payload_hash, player_count, "
            "raw_cache_path, schema_version) VALUES "
            "('missing-player-snapshot','sleeper','nfl',?,?,'hash',1,'/tmp/cache','1.0')",
            [observed_at, observed_at],
        )
        connection.execute(
            "INSERT INTO ranking_entries ("
            "ranking_snapshot_id, source_row_number, canonical_player_id, source_player_name, "
            "source_position, source_team, overall_rank, positional_rank, adp, adp_sd, "
            "projected_points, match_status, raw_payload) VALUES "
            "('orphan-ranking',7,NULL,'Missing Player','RB','SEA',7,'RB7',7,NULL,NULL,"
            "'unresolved','{}')"
        )

    IntelligenceRepository(path).initialize()

    assert _migration_versions(path) == [1, 2, 3, 4, 5]
    with duckdb.connect(str(path)) as connection:
        assert connection.execute(
            "SELECT observed_at FROM player_observations "
            "WHERE canonical_player_id = 'orphan-player'"
        ).fetchone() == (observed_at,)
        assert connection.execute(
            "SELECT source_player_name, source_position, source_team "
            "FROM ranking_match_issues WHERE ranking_snapshot_id = 'orphan-ranking' "
            "AND source_row_number = 7"
        ).fetchone() == ("Missing Player", "RB", "SEA")


_DEPLOYED_PLAYER_OBSERVATION_COLUMNS = [
    "snapshot_id",
    "canonical_player_id",
    "provider_player_id",
    "first_name",
    "last_name",
    "normalized_full_name",
    "position",
    "fantasy_positions",
    "team",
    "active",
    "status",
    "injury_status",
    "years_experience",
    "provider_ids",
    "raw_payload",
]
_FINAL_PLAYER_OBSERVATION_COLUMNS = [
    *_DEPLOYED_PLAYER_OBSERVATION_COLUMNS[:2],
    "provider",
    _DEPLOYED_PLAYER_OBSERVATION_COLUMNS[2],
    "observed_at",
    *_DEPLOYED_PLAYER_OBSERVATION_COLUMNS[3:],
]


def _create_deployed_m2_database(path: Path) -> None:
    from fantasy_war_room.repository import MIGRATION_1, MIGRATION_2, MIGRATION_3

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        connection.execute(MIGRATION_1)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute(MIGRATION_2)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        connection.execute(MIGRATION_3)
        connection.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (3, 'm2_player_intelligence')"
        )


def _table_schema(path: Path, table: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(path)) as connection:
        return connection.execute(f"PRAGMA table_info('{table}')").fetchall()


def _insert_orphaned_player_observation(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "INSERT INTO player_observations ("
            "snapshot_id, canonical_player_id, provider_player_id, first_name, last_name, "
            "normalized_full_name, position, fantasy_positions, team, active, status, "
            "injury_status, years_experience, provider_ids, raw_payload) VALUES "
            "('missing-player-snapshot','orphan-player','orphan-provider','Orphan','Player',"
            "'orphan player','RB','[\"RB\"]','SEA',true,'Active',NULL,1,'{}','{}')"
        )


def _insert_unmatched_ranking_issue(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "INSERT INTO ranking_match_issues ("
            "ranking_snapshot_id, source_row_number, match_status, reason, candidate_player_ids, "
            "raw_payload) VALUES "
            "('orphan-ranking',7,'unresolved','no matching entry','[]','{}')"
        )


def _database_state(path: Path) -> dict[str, tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]]:
    with duckdb.connect(str(path)) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        return {
            table: (
                connection.execute(f"PRAGMA table_info('{table}')").fetchall(),
                connection.execute(f'SELECT * FROM "{table}" ORDER BY ALL').fetchall(),
            )
            for table in tables
        }


def _migration_versions(path: Path) -> list[int]:
    with duckdb.connect(str(path)) as connection:
        return [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
