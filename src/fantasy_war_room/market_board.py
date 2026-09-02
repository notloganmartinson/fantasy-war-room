from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from fantasy_war_room.models import MarketBoardIssue, MarketBoardSnapshot
from fantasy_war_room.repository import IntelligenceRepository

MARKET_BOARD_SOURCE = "fantasy-football-calculator-market-board"
MARKET_BOARD_TRANSFORMATION_VERSION = "ffc-adp-to-market-board-1.0"


def derive_market_board(
    repository: IntelligenceRepository, adp_snapshot_id: str
) -> tuple[MarketBoardSnapshot, bool]:
    existing = repository.market_board_for_adp(adp_snapshot_id)
    if existing is not None:
        return existing, False
    adp, source_entries, source_issues = repository.adp_snapshot_data(adp_snapshot_id)
    matched = sorted(
        (row for row in source_entries if row["match_status"] == "matched"),
        key=lambda row: (
            float(row["overall_adp"]),
            str(row["player_name"]).casefold(),
            str(row["canonical_player_id"]),
            int(row["source_row_number"]),
        ),
    )
    ranks = {int(row["source_row_number"]): rank for rank, row in enumerate(matched, start=1)}
    entries = [
        {**row, "overall_market_rank": ranks.get(int(row["source_row_number"]))}
        for row in source_entries
    ]
    snapshot_id = str(
        uuid5(NAMESPACE_URL, f"fwr:{MARKET_BOARD_TRANSFORMATION_VERSION}:{adp_snapshot_id}")
    )
    issues = [
        MarketBoardIssue(
            market_board_snapshot_id=snapshot_id,
            source_row_number=issue.source_row_number,
            source_player_name=issue.source_player_name,
            source_position=issue.source_position,
            source_team=issue.source_team,
            match_status=issue.match_status,
            reason=issue.reason,
            candidate_player_ids=issue.candidate_player_ids,
            raw_payload=issue.raw_payload,
        )
        for issue in source_issues
    ]
    content = {
        "derived_from_adp_snapshot_id": adp_snapshot_id,
        "transformation_version": MARKET_BOARD_TRANSFORMATION_VERSION,
        "rows": [
            {
                "source_row_number": row["source_row_number"],
                "canonical_player_id": row["canonical_player_id"],
                "overall_market_rank": row["overall_market_rank"],
                "overall_adp": row["overall_adp"],
                "adp_sd": row.get("adp_sd"),
                "sample_size": row.get("sample_size"),
                "match_status": row["match_status"],
            }
            for row in entries
        ],
    }
    payload_hash = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = MarketBoardSnapshot(
        market_board_snapshot_id=snapshot_id,
        source=MARKET_BOARD_SOURCE,
        source_version=adp.source_version,
        transformation_version=MARKET_BOARD_TRANSFORMATION_VERSION,
        derived_from_adp_snapshot_id=adp.adp_snapshot_id,
        season=adp.season,
        league_size=adp.league_size,
        scoring_format=adp.scoring_format,
        draft_type=adp.draft_type,
        observed_at=adp.observed_at,
        fetched_at=adp.fetched_at,
        imported_at=datetime.now(UTC),
        payload_hash=payload_hash,
        source_uri=adp.source_uri,
        source_payload_hash=adp.source_payload_hash,
        identity_resolver_version=adp.identity_resolver_version,
        total_row_count=adp.total_row_count,
        matched_row_count=adp.matched_row_count,
        unresolved_row_count=adp.unresolved_row_count,
        ambiguous_row_count=adp.ambiguous_row_count,
    )
    return snapshot, repository.insert_market_board_snapshot(snapshot, entries, issues)
