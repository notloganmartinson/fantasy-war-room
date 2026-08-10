from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
import respx
from typer.testing import CliRunner

from fantasy_war_room.errors import ConfigurationError, NotFoundError
from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.services import (
    discover_drafts,
    require_draft_scoring_context,
    sync_by_draft_id,
    watch_by_draft_id,
)


class DirectDraftProvider:
    def __init__(self) -> None:
        self.drafts = {
            "league-draft": {
                "draft_id": "league-draft",
                "league_id": "league-1",
                "status": "drafting",
                "type": "snake",
                "season": "2026",
                "settings": {"teams": 10, "rounds": 15},
            },
            "mock-draft": {
                "draft_id": "mock-draft",
                "league_id": None,
                "status": "pre_draft",
                "type": "snake",
                "season": "2026",
                "settings": {"teams": 10, "rounds": 15},
                "metadata": {"name": "Practice room"},
            },
            "other-draft": {
                "draft_id": "other-draft",
                "league_id": "league-2",
                "status": "complete",
                "type": "snake",
                "season": "2026",
                "settings": {"teams": 12, "rounds": 16},
            },
        }
        self.leagues = {
            "league-1": {
                "league_id": "league-1",
                "name": "Primary",
                "scoring_settings": {"rec": 1.0},
            },
            "league-2": {
                "league_id": "league-2",
                "name": "Other",
                "scoring_settings": {"rec": 0.5},
            },
        }
        self.picks: list[dict[str, Any]] = []
        self.pick_sequence: list[list[dict[str, Any]]] = []
        self.draft_sequence: list[dict[str, Any]] = []
        self.requested_drafts: list[str] = []
        self.requested_leagues: list[str] = []

    def get_user(self, username_or_id: str) -> dict[str, Any]:
        return {"user_id": "user-1"}

    def get_user_leagues(self, user_id: str, season: str) -> list[dict[str, Any]]:
        raise AssertionError("direct draft flow must not discover leagues")

    def get_user_drafts(self, user_id: str, season: str) -> list[dict[str, Any]]:
        return list(self.drafts.values())

    def get_league(self, league_id: str) -> dict[str, Any]:
        self.requested_leagues.append(league_id)
        return self.leagues[league_id]

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        raise AssertionError("direct draft flow must not select a league draft")

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        self.requested_drafts.append(draft_id)
        if self.draft_sequence:
            return self.draft_sequence.pop(0)
        if draft_id not in self.drafts:
            raise NotFoundError(f"Sleeper draft {draft_id!r} was not found")
        return self.drafts[draft_id]

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        if self.pick_sequence:
            return self.pick_sequence.pop(0)
        return self.picks


def test_draft_listing_includes_league_and_standalone_metadata() -> None:
    provider = DirectDraftProvider()

    drafts = discover_drafts(provider, "user-1", "2026", {"mock-draft"})

    assert {draft.draft_id for draft in drafts} == {
        "league-draft",
        "mock-draft",
        "other-draft",
    }
    mock = next(draft for draft in drafts if draft.draft_id == "mock-draft")
    assert mock.is_mock is True
    assert mock.league_id is None
    assert mock.name == "Practice room"
    assert mock.team_count == 10 and mock.rounds == 15
    assert mock.locally_stored is True


def test_direct_sync_uses_exact_draft_and_preserves_league_context(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    repository = SnapshotRepository(tmp_path / "direct.duckdb")

    snapshot, created = sync_by_draft_id(provider, repository, "other-draft")

    assert created is True
    assert provider.requested_drafts == ["other-draft"]
    assert provider.requested_leagues == ["league-2"]
    assert snapshot.draft_id == "other-draft"
    assert snapshot.source_league_id == "league-2"
    assert snapshot.scoring_context_league_id == "league-2"
    assert snapshot.draft_context_type == "league"
    assert require_draft_scoring_context(snapshot)["scoring_settings"] == {"rec": 0.5}


def test_direct_mock_sync_has_explicit_optional_scoring_context(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    repository = SnapshotRepository(tmp_path / "mock.duckdb")

    without_context, created = sync_by_draft_id(provider, repository, "mock-draft")
    assert created is True
    assert without_context.league_id is None
    assert without_context.source_league_id is None
    assert without_context.scoring_context_league_id is None
    assert without_context.draft_context_type == "standalone"
    with pytest.raises(ConfigurationError) as error:
        require_draft_scoring_context(without_context)
    assert error.value.code == "mock_scoring_context_required"

    with_context, context_created = sync_by_draft_id(
        provider,
        repository,
        "mock-draft",
        scoring_context_league_id="league-1",
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert context_created is True
    assert with_context.source_league_id is None
    assert with_context.scoring_context_league_id == "league-1"
    assert with_context.scoring_context == provider.leagues["league-1"]


def test_direct_sync_deduplicates_and_preserves_a_b_a(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    repository = SnapshotRepository(tmp_path / "history.duckdb")
    state_a: list[dict[str, Any]] = []
    state_b = [{"pick_no": 1, "draft_slot": 1, "player_id": "p1"}]

    provider.picks = state_a
    first, first_created = sync_by_draft_id(provider, repository, "mock-draft")
    duplicate, duplicate_created = sync_by_draft_id(provider, repository, "mock-draft")
    provider.picks = state_b
    second, second_created = sync_by_draft_id(provider, repository, "mock-draft")
    provider.picks = state_a
    third, third_created = sync_by_draft_id(provider, repository, "mock-draft")

    assert (first_created, duplicate_created, second_created, third_created) == (
        True,
        False,
        True,
        True,
    )
    assert first.payload_hash == duplicate.payload_hash == third.payload_hash
    assert first.payload_hash != second.payload_hash
    assert repository.state_at("mock-draft", third.observed_at) == third


def test_direct_watch_uses_exact_draft_and_shared_history_path(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    pick = {"pick_no": 1, "draft_slot": 1, "player_id": "p1"}
    provider.pick_sequence = [[], [pick], [pick], []]
    repository = SnapshotRepository(tmp_path / "watch.duckdb")
    observed: list[Snapshot] = []

    watch_by_draft_id(
        provider,
        repository,
        "mock-draft",
        poll_seconds=0,
        on_snapshot=observed.append,
        max_polls=4,
        sleep=lambda _: None,
    )

    assert provider.requested_drafts == ["mock-draft"] * 4
    assert [snapshot.pick_count for snapshot in observed] == [0, 1, 0]
    assert repository.state_at("mock-draft", observed[-1].observed_at) == observed[-1]


def test_direct_watch_persists_status_only_transition_and_suppresses_duplicate(
    tmp_path: Path,
) -> None:
    provider = DirectDraftProvider()
    state_a = {**provider.drafts["mock-draft"], "status": "pre_draft"}
    state_b = {**provider.drafts["mock-draft"], "status": "drafting"}
    provider.draft_sequence = [state_a, state_b, state_b]
    provider.pick_sequence = [[], [], []]
    repository = SnapshotRepository(tmp_path / "status.duckdb")
    observed: list[Snapshot] = []

    watch_by_draft_id(
        provider,
        repository,
        "mock-draft",
        poll_seconds=0,
        on_snapshot=observed.append,
        max_polls=3,
        sleep=lambda _: None,
    )

    assert [snapshot.draft["status"] for snapshot in observed] == ["pre_draft", "drafting"]
    assert observed[0].payload_hash != observed[1].payload_hash


def test_direct_watch_persists_last_picked_metadata_transition(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    state_a = {**provider.drafts["mock-draft"], "last_picked": 1_700_000_000_000}
    state_b = {**provider.drafts["mock-draft"], "last_picked": 1_700_000_001_000}
    provider.draft_sequence = [state_a, state_b]
    provider.pick_sequence = [[], []]
    repository = SnapshotRepository(tmp_path / "last-picked.duckdb")
    observed: list[Snapshot] = []

    watch_by_draft_id(
        provider,
        repository,
        "mock-draft",
        poll_seconds=0,
        on_snapshot=observed.append,
        max_polls=2,
        sleep=lambda _: None,
    )

    assert len(observed) == 2
    assert observed[0].source_updated_at != observed[1].source_updated_at
    assert [snapshot.draft["last_picked"] for snapshot in observed] == [
        1_700_000_000_000,
        1_700_000_001_000,
    ]


def test_direct_watch_metadata_a_b_a_history(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    state_a = {**provider.drafts["mock-draft"], "status": "pre_draft"}
    state_b = {**provider.drafts["mock-draft"], "status": "drafting"}
    provider.draft_sequence = [state_a, state_b, state_a]
    provider.pick_sequence = [[], [], []]
    repository = SnapshotRepository(tmp_path / "metadata-history.duckdb")
    observed: list[Snapshot] = []

    watch_by_draft_id(
        provider,
        repository,
        "mock-draft",
        poll_seconds=0,
        on_snapshot=observed.append,
        max_polls=3,
        sleep=lambda _: None,
    )

    assert [snapshot.draft["status"] for snapshot in observed] == [
        "pre_draft",
        "drafting",
        "pre_draft",
    ]
    assert observed[0].payload_hash == observed[2].payload_hash != observed[1].payload_hash
    with duckdb.connect(str(repository.path)) as connection:
        assert connection.execute("SELECT count(*) FROM draft_snapshots").fetchone() == (3,)


def test_direct_watch_and_sync_have_canonical_state_parity(tmp_path: Path) -> None:
    provider = DirectDraftProvider()
    final_draft = {
        **provider.drafts["mock-draft"],
        "status": "drafting",
        "status_updated": 1_700_000_002_000,
    }
    reversed_picks = [
        {"pick_no": 2, "draft_slot": 2, "player_id": "p2"},
        {"pick_no": 1, "draft_slot": 1, "player_id": "p1"},
    ]
    provider.draft_sequence = [final_draft]
    provider.pick_sequence = [reversed_picks]
    provider.drafts["mock-draft"] = final_draft
    provider.picks = list(reversed(reversed_picks))
    repository = SnapshotRepository(tmp_path / "parity.duckdb")
    observed: list[Snapshot] = []

    watch_by_draft_id(
        provider,
        repository,
        "mock-draft",
        poll_seconds=0,
        on_snapshot=observed.append,
        max_polls=1,
        sleep=lambda _: None,
    )
    synced, created = sync_by_draft_id(provider, repository, "mock-draft")

    assert created is False
    watched = observed[0]
    assert watched.payload_hash == synced.payload_hash
    assert watched.draft == synced.draft == final_draft
    assert watched.picks == synced.picks
    assert watched.source_updated_at == synced.source_updated_at
    assert watched.draft_context_type == synced.draft_context_type


def test_direct_sync_missing_draft_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        sync_by_draft_id(
            DirectDraftProvider(), SnapshotRepository(tmp_path / "missing.duckdb"), "missing"
        )


@respx.mock(base_url="https://api.sleeper.app/v1")
def test_drafts_list_and_direct_mock_sync_json(
    api: Any,
    runner: CliRunner,
    xdg: Path,
    sleeper_payloads: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import parse_output, register_sleeper

    from fantasy_war_room.cli import app

    monkeypatch.setenv("FWR_SLEEPER_USER_ID", "u1")
    register_sleeper(api, sleeper_payloads)
    synced = runner.invoke(app, ["sync", "--draft-id", "mock1", "--json"])
    listed = runner.invoke(app, ["drafts", "list", "--json"])

    assert synced.exit_code == listed.exit_code == 0
    sync_body = parse_output(synced)
    assert sync_body["data"]["snapshot"]["draft_id"] == "mock1"
    assert sync_body["data"]["snapshot"]["source_league_id"] is None
    list_body = parse_output(listed)
    assert list_body["command"] == "drafts list"
    assert {draft["draft_id"] for draft in list_body["data"]["drafts"]} == {"d1", "mock1"}
    mock = next(draft for draft in list_body["data"]["drafts"] if draft["draft_id"] == "mock1")
    assert mock["is_mock"] is True and mock["locally_stored"] is True


def test_watch_cli_selects_explicit_draft_id(
    runner: CliRunner,
    xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fantasy_war_room.cli import app

    selected: dict[str, Any] = {}

    def fake_watch(
        provider: Any,
        repository: SnapshotRepository,
        draft_id: str,
        poll_seconds: float,
        scoring_context_league_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        selected.update(
            draft_id=draft_id,
            poll_seconds=poll_seconds,
            scoring_context_league_id=scoring_context_league_id,
        )

    monkeypatch.setattr("fantasy_war_room.cli.watch_by_draft_id", fake_watch)
    result = runner.invoke(
        app,
        [
            "watch",
            "--draft-id",
            "mock-77",
            "--scoring-context-league-id",
            "league-1",
            "--interval",
            "0.25",
        ],
    )

    assert result.exit_code == 0
    assert selected == {
        "draft_id": "mock-77",
        "poll_seconds": 0.25,
        "scoring_context_league_id": "league-1",
    }
