from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fantasy_war_room.database import with_database_lock_retry
from fantasy_war_room.errors import (
    ConfigurationError,
    DatabaseBusyError,
    InputError,
    NotFoundError,
)
from fantasy_war_room.models import DraftSummary, LeagueSummary, Snapshot
from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.sleeper import SleeperProvider

LOGGER = logging.getLogger(__name__)


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


def discover_drafts(
    provider: SleeperProvider, user_id: str, season: str, stored_draft_ids: set[str]
) -> list[DraftSummary]:
    drafts = provider.get_user_drafts(user_id, season)
    return sorted(
        [_draft_summary(draft, season, stored_draft_ids) for draft in drafts],
        key=lambda draft: (draft.status, draft.draft_id),
    )


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
    return _persist_draft_state(
        repository,
        league,
        league_id,
        league_id,
        league,
        "league",
        draft,
        picks,
        observed_at,
    )


def sync_by_draft_id(
    provider: SleeperProvider,
    repository: SnapshotRepository,
    draft_id: str,
    scoring_context_league_id: str | None = None,
    observed_at: datetime | None = None,
) -> tuple[Snapshot, bool]:
    draft = provider.get_draft(draft_id)
    context = _resolve_draft_context(provider, draft, scoring_context_league_id)
    picks = provider.get_draft_picks(draft_id)
    return _persist_draft_state(repository, *context, draft, picks, observed_at)


def _persist_draft_state(
    repository: SnapshotRepository,
    league: dict[str, Any],
    source_league_id: str | None,
    scoring_context_league_id: str | None,
    scoring_context: dict[str, Any] | None,
    draft_context_type: str,
    draft: dict[str, Any],
    picks: list[dict[str, Any]],
    observed_at: datetime | None = None,
) -> tuple[Snapshot, bool]:
    snapshot = _build_draft_snapshot(
        league,
        source_league_id,
        scoring_context_league_id,
        scoring_context,
        draft_context_type,
        draft,
        picks,
        observed_at,
    )
    return snapshot, repository.insert(snapshot)


def _build_draft_snapshot(
    league: dict[str, Any],
    source_league_id: str | None,
    scoring_context_league_id: str | None,
    scoring_context: dict[str, Any] | None,
    draft_context_type: str,
    draft: dict[str, Any],
    picks: list[dict[str, Any]],
    observed_at: datetime | None = None,
) -> Snapshot:
    canonical_picks = sorted(
        picks,
        key=lambda pick: (
            int(pick.get("pick_no") or 0),
            json.dumps(pick, sort_keys=True, separators=(",", ":")),
        ),
    )
    payload: dict[str, Any] = {"league": league, "draft": draft, "picks": picks}
    payload["picks"] = canonical_picks
    if source_league_id is None:
        payload["scoring_context"] = scoring_context
    source_updated_at = _sleeper_timestamp(draft.get("last_picked") or draft.get("status_updated"))
    return Snapshot(
        snapshot_id=str(uuid4()),
        league_id=source_league_id,
        draft_id=str(draft["draft_id"]),
        observed_at=observed_at or datetime.now(UTC),
        source_updated_at=source_updated_at,
        payload_hash=canonical_hash(payload),
        pick_count=len(canonical_picks),
        league=league,
        draft=draft,
        picks=canonical_picks,
        source_league_id=source_league_id,
        scoring_context_league_id=scoring_context_league_id,
        scoring_context=scoring_context,
        draft_context_type=draft_context_type,
    )


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
    _watch_exact_draft(
        provider,
        repository,
        str(draft["draft_id"]),
        poll_seconds,
        league_id,
        on_snapshot,
        max_polls,
        sleep,
    )


def watch_by_draft_id(
    provider: SleeperProvider,
    repository: SnapshotRepository,
    draft_id: str,
    poll_seconds: float,
    scoring_context_league_id: str | None = None,
    on_snapshot: Callable[[Snapshot], None] | None = None,
    max_polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    _watch_exact_draft(
        provider,
        repository,
        draft_id,
        poll_seconds,
        scoring_context_league_id,
        on_snapshot,
        max_polls,
        sleep,
    )


def _watch_exact_draft(
    provider: SleeperProvider,
    repository: SnapshotRepository,
    draft_id: str,
    poll_seconds: float,
    scoring_context_league_id: str | None,
    on_snapshot: Callable[[Snapshot], None] | None,
    max_polls: int | None,
    sleep: Callable[[float], None],
) -> None:
    last_hash: str | None = None
    polls = 0
    while max_polls is None or polls < max_polls:
        draft = provider.get_draft(draft_id)
        context = _resolve_draft_context(provider, draft, scoring_context_league_id)
        picks = provider.get_draft_picks(draft_id)
        polls += 1
        snapshot = _build_draft_snapshot(*context, draft, picks)
        if snapshot.payload_hash != last_hash:
            created = _persist_watched_snapshot(repository, snapshot, poll_seconds, sleep)
            if created and on_snapshot is not None:
                on_snapshot(snapshot)
            last_hash = snapshot.payload_hash
        if max_polls is None or polls < max_polls:
            sleep(poll_seconds)


def _persist_watched_snapshot(
    repository: SnapshotRepository,
    snapshot: Snapshot,
    poll_seconds: float,
    sleep: Callable[[float], None],
) -> bool:
    while True:
        try:
            return with_database_lock_retry(lambda: repository.insert(snapshot), sleep=sleep)
        except DatabaseBusyError:
            LOGGER.warning(
                "DuckDB remains busy; retaining draft %s state %s for retry",
                snapshot.draft_id,
                snapshot.payload_hash,
            )
            sleep(poll_seconds)


def require_draft_scoring_context(snapshot: Snapshot) -> dict[str, Any]:
    if snapshot.scoring_context_league_id is None or snapshot.scoring_context is None:
        raise ConfigurationError(
            "mock_scoring_context_required",
            "Standalone draft has no explicit league scoring context",
            {"draft_id": snapshot.draft_id},
        )
    return snapshot.scoring_context


def _resolve_draft_context(
    provider: SleeperProvider,
    draft: dict[str, Any],
    scoring_context_league_id: str | None,
) -> tuple[dict[str, Any], str | None, str | None, dict[str, Any] | None, str]:
    source_value = draft.get("league_id")
    source_league_id = str(source_value) if source_value not in {None, ""} else None
    if source_league_id is not None:
        if scoring_context_league_id not in {None, source_league_id}:
            raise InputError(
                "scoring_context_mismatch",
                "League-associated draft must use its own league scoring context",
                {
                    "draft_id": str(draft.get("draft_id")),
                    "source_league_id": source_league_id,
                    "requested_scoring_context_league_id": scoring_context_league_id,
                },
            )
        league = provider.get_league(source_league_id)
        return league, source_league_id, source_league_id, league, "league"
    if scoring_context_league_id is None:
        return {}, None, None, None, "standalone"
    scoring_context = provider.get_league(scoring_context_league_id)
    return {}, None, scoring_context_league_id, scoring_context, "standalone"


def _draft_summary(
    draft: dict[str, Any], fallback_season: str, stored_draft_ids: set[str]
) -> DraftSummary:
    raw_settings = draft.get("settings")
    raw_metadata = draft.get("metadata")
    settings: dict[str, Any] = raw_settings if isinstance(raw_settings, dict) else {}
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    league_value = draft.get("league_id")
    league_id = str(league_value) if league_value not in {None, ""} else None
    draft_id = str(draft["draft_id"])
    rounds_value = settings.get("rounds")
    return DraftSummary(
        draft_id=draft_id,
        status=str(draft.get("status") or "unknown"),
        draft_type=str(draft.get("type") or "unknown"),
        season=str(draft.get("season") or fallback_season),
        team_count=int(settings.get("teams") or 0),
        rounds=int(rounds_value) if rounds_value is not None else None,
        league_id=league_id,
        is_mock=league_id is None,
        name=str(metadata.get("name") or metadata.get("title") or "") or None,
        locally_stored=draft_id in stored_draft_ids,
    )


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
