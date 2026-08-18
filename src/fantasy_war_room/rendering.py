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
    if hasattr(result, "raw_recommendation"):
        stdout.print(
            f"[bold]Strategy:[/bold] {result.strategy_provenance.profile_name} "
            f"({result.strategy_provenance.profile_hash[:12]})"
        )
        if result.roster_completion_required:
            directive = result.directive
            stdout.print(
                "[bold red]Roster completion required:[/bold red] "
                + ", ".join(result.unfilled_required_positions)
            )
            if directive is not None:
                stdout.print(
                    f"Remaining selections: {directive.remaining_user_selections}; "
                    f"required slots: {directive.required_position_count}; "
                    f"status={directive.boundary_status}; rule={directive.rule_version}."
                )
                stdout.print(directive.message)
            stdout.print("No offensive strategy recommendation is actionable.")
            return
        raw = result.raw_recommendation
        context = raw.turn_context
        specification = raw.model_specification
        rows = result.candidates
        stdout.print(
            f"[bold]Raw model:[/bold] {specification.recommendation_model_version} "
            f"weights={specification.weights}"
        )
        stdout.print(
            f"[bold]Round {context.current_round}, pick {context.next_overall_pick}[/bold]"
        )
        table = Table(title="Strategy-adjusted draft recommendations")
        for heading in ("Strategy", "Raw", "Player", "Pos", "Raw score", "Class"):
            table.add_column(heading)
        for row in rows:
            candidate = row.raw_candidate
            table.add_row(
                str(row.strategy_rank),
                str(row.raw_rank),
                candidate.player_name,
                candidate.position,
                f"{row.raw_score:.2f}",
                f"{row.target_promotion_class}/{row.positional_utility_class}",
            )
        stdout.print(table)
        return
    context = result.turn_context
    specification = result.model_specification
    stdout.print(
        f"[bold]Model:[/bold] {specification.recommendation_model_version} "
        f"weights={specification.weights}"
    )
    if hasattr(specification, "trusted_rank_transform_version"):
        stdout.print(
            f"Trusted transforms: rank={specification.trusted_rank_transform_version}, "
            f"tier={specification.trusted_tier_transform_version}"
        )
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
    headings = [
        "#",
        "Player",
        "Pos",
        "Score",
        "Projection",
        "VORP",
        "Rank",
        "Scarcity",
        "Δ / roster effect",
    ]
    trusted_tiers = bool(result.candidates) and hasattr(result.candidates[0], "trusted_tier")
    if trusted_tiers:
        headings.insert(7, "Tier")
    for heading in headings:
        table.add_column(heading)
    for candidate in result.candidates:
        cells = [
            str(candidate.recommendation_rank),
            candidate.player_name,
            candidate.position,
            f"{candidate.recommendation_score:.2f}",
            f"{candidate.projection_baseline:.1f} "
            f"({'exact' if candidate.projection_value_kind == 'exact' else 'known'})",
            f"{candidate.vorp:.1f}",
            _display_number(candidate.expert_overall_rank),
        ]
        if trusted_tiers:
            cells.append(candidate.trusted_tier or "-")
        cells.extend(
            [
                _display_number(candidate.scarcity.scarcity_points),
                f"{candidate.roster_effect.starter_projection_delta:.1f} / "
                f"{candidate.roster_effect.category}",
            ]
        )
        table.add_row(*cells)
    stdout.print(table)
    stdout.print("Next-pick probability: unavailable (uncalibrated)")
    for limitation in result.limitations:
        stdout.print(f"[yellow]Limitation:[/yellow] {limitation}")


def render_survival(response: Any) -> None:
    result = response.simulation
    names = response.provenance.get("candidate_names", {})
    stdout.print(
        f"[bold]Survival model:[/bold] {result.model_version}; "
        f"runs={result.simulation_count}; seed={result.seed}"
    )
    stdout.print(
        f"Current pick {result.current_overall_pick}; simulation starts at "
        f"{result.simulation_start_pick}; target user pick {result.target_user_pick}; "
        f"opponent-pick horizon {result.intervening_opponent_pick_count}."
    )
    for candidate in result.candidates:
        name = names.get(candidate.canonical_player_id) or candidate.canonical_player_id
        if candidate.status == "modeled":
            stdout.print(
                f"If you pass on {name} at pick {result.current_overall_pick}, {name} remained "
                f"available before your target pick {result.target_user_pick} in "
                f"{candidate.survived_simulation_count:,} of "
                f"{candidate.simulation_count:,} model runs: "
                f"{candidate.simulated_availability_rate:.1%} simulated availability rate."
            )
        else:
            stdout.print(f"{name}: {candidate.status}; no simulated availability rate.")
    coverage = result.pool_coverage
    stdout.print(
        f"Modeled pool: {coverage.modeled_available_players}/"
        f"{coverage.total_available_relevant_players} available relevant players "
        f"({coverage.coverage_rate:.1%})."
    )
    if coverage.warning_codes:
        stdout.print("Warnings: " + ", ".join(coverage.warning_codes))
    for limitation in result.limitations:
        stdout.print(f"Limitation: {limitation}")


def render_survival_evaluation(report: Any) -> None:
    stdout.print(
        f"[bold]Historical survival evaluation:[/bold] {report.draft_id}; "
        f"policy={report.evaluation_policy.policy_version}"
    )
    stdout.print(
        f"Decision points: {report.evaluated_decision_point_count}/"
        f"{report.eligible_decision_point_count}; common candidate cases: "
        f"{report.candidate_population.modeled_evaluation_candidates}."
    )
    for model in report.models:
        stdout.print(
            f"{model.model_version}: n={model.evaluated_candidate_count}, "
            f"Brier={_display_number(model.brier_score)}, "
            f"log loss={_display_number(model.log_loss)}, "
            f"mean simulated rate={_display_number(model.mean_simulated_availability_rate)}, "
            f"observed survival={_display_number(model.actual_survival_rate)}"
        )
    stdout.print(f"Recommended default: {report.recommended_default}")
    stdout.print(report.recommendation_reason)


def _display_number(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def diagnostic(message: str) -> None:
    stderr.print(message)
