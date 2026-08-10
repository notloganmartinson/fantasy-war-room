from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

stdout = Console()
stderr = Console(stderr=True)


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value


def emit_json(command: str, data: Any = None, error: dict[str, Any] | None = None) -> None:
    envelope = {
        "status": "error" if error else "success",
        "command": command,
        "data": jsonable(data),
        "error": error,
    }
    sys.stdout.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")


def render_leagues(leagues: list[Any]) -> None:
    table = Table(title="Sleeper leagues")
    for heading in ("Name", "League ID", "Status", "Draft ID", "Teams"):
        table.add_column(heading)
    for league in leagues:
        table.add_row(
            league.name,
            league.league_id,
            league.status,
            league.draft_id or "-",
            str(league.total_rosters),
        )
    stdout.print(table)


def render_drafts(drafts: list[Any]) -> None:
    table = Table(title="Sleeper drafts")
    for heading in ("Name", "Draft ID", "Status", "Type", "Season", "Teams", "League", "Local"):
        table.add_column(heading)
    for draft in drafts:
        table.add_row(
            draft.name or "-",
            draft.draft_id,
            draft.status,
            draft.draft_type,
            draft.season,
            str(draft.team_count),
            draft.league_id or "standalone",
            "yes" if draft.locally_stored else "no",
        )
    stdout.print(table)


def render_players(players: list[Any]) -> None:
    table = Table(title="Players")
    for heading in ("Name", "Position", "Team", "Sleeper ID"):
        table.add_column(heading)
    for player in players:
        table.add_row(
            f"{player.first_name} {player.last_name}".strip(),
            player.position or "-",
            player.team or "-",
            player.sleeper_player_id,
        )
    stdout.print(table)


def render_rankings(snapshots: list[Any]) -> None:
    table = Table(title="Ranking imports")
    for heading in ("Source", "Season", "Scoring", "League", "Observed", "Matched", "Issues"):
        table.add_column(heading)
    for snapshot in snapshots:
        table.add_row(
            snapshot.source,
            snapshot.season,
            snapshot.scoring_format,
            str(snapshot.league_size),
            snapshot.observed_at.isoformat(),
            str(snapshot.matched_row_count),
            str(snapshot.unresolved_row_count + snapshot.ambiguous_row_count),
        )
    stdout.print(table)


def render_ranking_issues(issues: list[Any]) -> None:
    table = Table(title="Unresolved ranking rows")
    for heading in ("Snapshot", "Row", "Status", "Reason", "Candidates"):
        table.add_column(heading)
    for issue in issues:
        table.add_row(
            issue.ranking_snapshot_id,
            str(issue.source_row_number),
            issue.match_status,
            issue.reason,
            ", ".join(issue.candidate_player_ids) or "-",
        )
    stdout.print(table)


def render_projections(snapshots: list[Any]) -> None:
    table = Table(title="Projection imports")
    for heading in ("Source", "Version", "Season", "Observed", "Rows", "Matched", "Issues"):
        table.add_column(heading)
    for snapshot in snapshots:
        table.add_row(
            snapshot.source,
            snapshot.source_version,
            snapshot.season,
            snapshot.observed_at.isoformat(),
            str(snapshot.total_row_count),
            str(snapshot.matched_row_count),
            str(snapshot.unresolved_row_count + snapshot.ambiguous_row_count),
        )
    stdout.print(table)


def render_projection_issues(issues: list[Any]) -> None:
    table = Table(title="Unresolved projection rows")
    for heading in ("Snapshot", "Position", "Row", "Player", "Status", "Reason"):
        table.add_column(heading)
    for issue in issues:
        table.add_row(
            issue.projection_snapshot_id,
            issue.source_position,
            str(issue.source_row_number),
            issue.source_player_name,
            issue.match_status,
            issue.reason,
        )
    stdout.print(table)


def render_board(players: list[Any]) -> None:
    table = Table(title="Available player board")
    for heading in ("Rank", "Player", "Pos", "Team", "ADP", "Source"):
        table.add_column(heading)
    for player in players:
        table.add_row(
            _display_number(player.overall_rank),
            player.player_name,
            player.position or "-",
            player.team or "-",
            _display_number(player.adp),
            player.ranking_source,
        )
    stdout.print(table)


def render_recommendation(result: Any) -> None:
    context = result.turn_context
    status = "ON THE CLOCK" if context.on_the_clock else "waiting"
    stdout.print(
        f"[bold]Round {context.current_round}, pick {context.next_overall_pick}[/bold] "
        f"({status}); your next pick: {context.user_next_scheduled_pick}"
    )
    lineup = Table(title="Current offensive lineup")
    for heading in ("Slot", "Player", "Pos", "Projection", "Kind"):
        lineup.add_column(heading)
    for assignment in result.current_roster.starters:
        lineup.add_row(
            assignment.slot,
            assignment.player_name,
            assignment.position,
            f"{assignment.projection:.1f}",
            assignment.projection_value_kind,
        )
    if not result.current_roster.starters:
        lineup.add_row("-", "No offensive starters drafted", "-", "0.0", "-")
    stdout.print(lineup)

    table = Table(title="Draft recommendations")
    for heading in (
        "#",
        "Player",
        "Pos",
        "Score",
        "Projection",
        "VORP",
        "Rank",
        "Scarcity",
        "Δ / roster effect",
    ):
        table.add_column(heading)
    for candidate in result.candidates:
        table.add_row(
            str(candidate.recommendation_rank),
            candidate.player_name,
            candidate.position,
            f"{candidate.recommendation_score:.2f}",
            f"{candidate.projection_baseline:.1f} "
            f"({'exact' if candidate.projection_value_kind == 'exact' else 'known'})",
            f"{candidate.vorp:.1f}",
            _display_number(candidate.expert_overall_rank),
            _display_number(candidate.scarcity.scarcity_points),
            f"{candidate.roster_effect.starter_projection_delta:.1f} / "
            f"{candidate.roster_effect.category}",
        )
    stdout.print(table)
    stdout.print("Next-pick probability: unavailable (uncalibrated)")
    for limitation in result.limitations:
        stdout.print(f"[yellow]Limitation:[/yellow] {limitation}")


def _display_number(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def diagnostic(message: str) -> None:
    stderr.print(message)
