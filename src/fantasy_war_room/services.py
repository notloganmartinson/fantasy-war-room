from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fantasy_war_room.errors import ConfigurationError, InputError, NotFoundError
from fantasy_war_room.models import LeagueSummary, Snapshot
from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.sleeper import SleeperProvider


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def discover(
    provider: SleeperProvider, username: str | None, season: str
) -> tuple[dict[str, Any], list[LeagueSummary]]:
    if not username:
        raise ConfigurationError("missing_username", "A Sleeper username is required")
    user = provider.get_user(username)
    leagues = provider.get_user_leagues(str(user["user_id"]), season)
    summaries = [
        LeagueSummary(
            league_id=str(item["league_id"]),
            name=item.get("name", ""),
            status=item.get("status", "unknown"),
            draft_id=item.get("draft_id"),
            total_rosters=int(item.get("total_rosters", 0)),
        )
        for item in leagues
    ]
    return user, summaries


def select_draft(drafts: list[dict[str, Any]]) -> dict[str, Any]:
    if not drafts:
        raise NotFoundError("No draft exists for this league")
    return max(drafts, key=lambda item: int(item.get("created", 0)))


def sync(
    provider: SleeperProvider,
    repository: SnapshotRepository,
    league_id: str | None,
    observed_at: datetime | None = None,
) -> tuple[Snapshot, bool]:
    if not league_id:
        raise ConfigurationError("missing_league_id", "A Sleeper league ID is required")
    league = provider.get_league(league_id)
    selected = select_draft(provider.get_league_drafts(league_id))
    draft_id = str(selected["draft_id"])
    draft = provider.get_draft(draft_id)
    picks = provider.get_draft_picks(draft_id)
    payload = {"league": league, "draft": draft, "picks": picks}
    source_updated_at = _sleeper_timestamp(draft.get("last_picked") or draft.get("status_updated"))
    snapshot = Snapshot(
        snapshot_id=str(uuid4()),
        league_id=league_id,
        draft_id=draft_id,
        observed_at=observed_at or datetime.now(UTC),
        source_updated_at=source_updated_at,
        payload_hash=canonical_hash(payload),
        pick_count=len(picks),
        league=league,
        draft=draft,
        picks=picks,
    )
    return snapshot, repository.insert(snapshot)


def watch(
    provider: SleeperProvider,
    repository: SnapshotRepository,
    league_id: str,
    draft: dict[str, Any],
    poll_seconds: float,
    on_snapshot: Callable[[Snapshot], None] | None = None,
    max_polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    league = provider.get_league(league_id)
    draft_id = str(draft["draft_id"])
    last_hash: str | None = None
    polls = 0
    while max_polls is None or polls < max_polls:
        picks = provider.get_draft_picks(draft_id)
        polls += 1
        current_hash = canonical_hash(picks)
        if current_hash != last_hash:
            snapshot, created = sync(_StaticProvider(league, draft, picks), repository, league_id)
            if created and on_snapshot is not None:
                on_snapshot(snapshot)
            last_hash = current_hash
        if max_polls is None or polls < max_polls:
            sleep(poll_seconds)


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError("invalid_timestamp", "--at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise InputError("timezone_required", "--at must include a timezone")
    return parsed.astimezone(UTC)


def _sleeper_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    numeric = float(value)
    if numeric > 10_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, UTC)


class _StaticProvider:
    def __init__(
        self, league: dict[str, Any], draft: dict[str, Any], picks: list[dict[str, Any]]
    ) -> None:
        self.league, self.draft, self.picks = league, draft, picks

    def get_user(self, username_or_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_user_leagues(self, user_id: str, season: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_league(self, league_id: str) -> dict[str, Any]:
        return self.league

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return [self.draft]

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self.draft

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return self.picks
