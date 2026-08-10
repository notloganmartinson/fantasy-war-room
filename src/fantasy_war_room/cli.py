from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.prompt import Prompt

from fantasy_war_room.config import (
    app_dirs,
    config_file_path,
    ensure_directories,
    load_settings,
    save_settings,
)
from fantasy_war_room.errors import (
    ConfigurationError,
    ExitCode,
    FwrError,
    InputError,
    NotFoundError,
)
from fantasy_war_room.intelligence import (
    import_rankings,
    normalize_name,
    reprocess_rankings,
    sync_players,
)
from fantasy_war_room.projections import import_cbs_projections
from fantasy_war_room.recommendations import build_recommendation
from fantasy_war_room.rendering import (
    diagnostic,
    emit_json,
    render_board,
    render_drafts,
    render_leagues,
    render_players,
    render_projection_issues,
    render_projections,
    render_ranking_issues,
    render_rankings,
    render_recommendation,
    stdout,
)
from fantasy_war_room.repository import IntelligenceRepository, SnapshotRepository
from fantasy_war_room.services import discover as discover_leagues
from fantasy_war_room.services import (
    discover_drafts,
    parse_timestamp,
    select_draft,
    sync_by_draft_id,
    watch_by_draft_id,
)
from fantasy_war_room.services import sync as sync_draft
from fantasy_war_room.services import watch as watch_draft
from fantasy_war_room.sleeper import SleeperClient

app = typer.Typer(
    help="Local-first, time-aware fantasy football decision data.", no_args_is_help=True
)
players_app = typer.Typer(help="Synchronize and search the local player directory.")
rankings_app = typer.Typer(help="Import and inspect ranking snapshots.")
projections_app = typer.Typer(help="Import and inspect statistical projection snapshots.")
drafts_app = typer.Typer(help="Discover Sleeper league and standalone drafts.")
app.add_typer(players_app, name="players")
app.add_typer(rankings_app, name="rankings")
app.add_typer(projections_app, name="projections")
app.add_typer(drafts_app, name="drafts")


def _run[T](
    command: str, json_mode: bool, operation: Callable[[], T], human: Callable[[T], None]
) -> None:
    try:
        result = operation()
        if json_mode:
            emit_json(command, result)
        else:
            human(result)
    except FwrError as exc:
        error: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            error["details"] = exc.details
        if json_mode:
            emit_json(command, error=error)
            diagnostic(exc.message)
        else:
            diagnostic(f"Error: {exc.message}")
        raise typer.Exit(exc.exit_code) from exc
    except ValidationError as exc:
        message = "Invalid configuration"
        if json_mode:
            emit_json(
                command,
                error={
                    "code": "invalid_config",
                    "message": message,
                    "details": {"errors": exc.errors(include_url=False)},
                },
            )
            diagnostic(message)
        else:
            diagnostic(f"Error: {message}: {exc}")
        raise typer.Exit(ExitCode.CONFIGURATION) from exc
    except Exception as exc:
        if json_mode:
            emit_json(command, error={"code": "unexpected_error", "message": str(exc)})
            diagnostic(str(exc))
        else:
            diagnostic(f"Unexpected error: {exc}")
        raise typer.Exit(ExitCode.UNEXPECTED) from exc


def _client(settings: Any) -> SleeperClient:
    return SleeperClient(settings.sleeper_base_url, settings.http_timeout_seconds)


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json"),
    db_path: Path | None = typer.Option(None),
) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        ensure_directories(settings)
        SnapshotRepository(settings.db_path).initialize()
        return {
            "configuration_file": str(config_file_path()),
            "database": str(settings.db_path.expanduser().resolve()),
            "config_directory": app_dirs().user_config_dir,
            "data_directory": app_dirs().user_data_dir,
            "cache_directory": app_dirs().user_cache_dir,
            "checks": {"directories": "ok", "database": "ok"},
        }

    _run(
        "doctor",
        json_output,
        operation,
        lambda result: stdout.print("[green]All checks passed[/green]", result["database"]),
    )


@app.command()
def configure(
    username: str | None = typer.Option(None),
    league_id: str | None = typer.Option(None),
    season: str | None = typer.Option(None),
    db_path: Path | None = typer.Option(None),
    non_interactive: bool = typer.Option(False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings(
            sleeper_username=username, sleeper_league_id=league_id, season=season, db_path=db_path
        )
        client = _client(settings)
        try:
            user, leagues = discover_leagues(client, settings.sleeper_username, settings.season)
        finally:
            client.close()
        chosen = settings.sleeper_league_id
        ids = [league.league_id for league in leagues]
        if chosen and chosen not in ids:
            raise ConfigurationError(
                "league_not_available",
                "League does not belong to this user and season",
                {"available_league_ids": ids},
            )
        if not chosen:
            if len(leagues) == 1:
                chosen = leagues[0].league_id
            elif not leagues:
                raise ConfigurationError(
                    "no_leagues", "No NFL leagues found for the selected season"
                )
            elif non_interactive or not typer.get_text_stream("stdin").isatty():
                raise ConfigurationError(
                    "league_selection_required",
                    "Multiple leagues found; supply --league-id",
                    {"available_league_ids": ids},
                )
            else:
                render_leagues(leagues)
                chosen = Prompt.ask("League ID", choices=ids, console=stdout)
        configured = settings.model_copy(
            update={"sleeper_user_id": str(user["user_id"]), "sleeper_league_id": chosen}
        )
        ensure_directories(configured)
        path = save_settings(configured)
        return {
            "config_file": str(path),
            "username": configured.sleeper_username,
            "user_id": configured.sleeper_user_id,
            "league_id": chosen,
            "season": configured.season,
            "db_path": str(configured.db_path),
        }

    _run(
        "configure",
        json_output,
        operation,
        lambda result: stdout.print(
            f"Configured league {result['league_id']} in {result['config_file']}"
        ),
    )


@app.command()
def discover(
    username: str | None = typer.Option(None),
    season: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings(sleeper_username=username, season=season)
        client = _client(settings)
        try:
            user, leagues = discover_leagues(client, settings.sleeper_username, settings.season)
        finally:
            client.close()
        return {"user_id": str(user["user_id"]), "season": settings.season, "leagues": leagues}

    _run("discover", json_output, operation, lambda result: render_leagues(result["leagues"]))


@app.command()
def sync(
    league_id: str | None = typer.Option(None),
    draft_id: str | None = typer.Option(None, "--draft-id"),
    scoring_context_league_id: str | None = typer.Option(None, "--scoring-context-league-id"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        if league_id and draft_id:
            raise InputError(
                "draft_selection_conflict", "Supply either --league-id or --draft-id, not both"
            )
        settings = load_settings(sleeper_league_id=league_id, db_path=db_path)
        ensure_directories(settings)
        client = _client(settings)
        try:
            repository = SnapshotRepository(settings.db_path)
            if draft_id:
                snapshot, created = sync_by_draft_id(
                    client, repository, draft_id, scoring_context_league_id
                )
            else:
                if scoring_context_league_id:
                    raise InputError(
                        "scoring_context_requires_draft_id",
                        "--scoring-context-league-id requires --draft-id",
                    )
                snapshot, created = sync_draft(client, repository, settings.sleeper_league_id)
        finally:
            client.close()
        return {"created": created, "snapshot": snapshot}

    _run(
        "sync",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} snapshot "
            f"{result['snapshot'].snapshot_id}"
        ),
    )


@app.command("state-at")
def state_at(
    draft_id: str = typer.Option(...),
    at: str = typer.Option(...),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        snapshot = SnapshotRepository(settings.db_path).state_at(draft_id, parse_timestamp(at))
        if snapshot is None:
            raise NotFoundError(
                "No snapshot exists at or before the requested time",
                {"draft_id": draft_id, "at": at},
            )
        return {"snapshot": snapshot}

    _run(
        "state-at",
        json_output,
        operation,
        lambda result: stdout.print_json(data=result["snapshot"].model_dump(mode="json")),
    )


@app.command()
def watch(
    league_id: str | None = typer.Option(None),
    draft_id: str | None = typer.Option(None, "--draft-id"),
    scoring_context_league_id: str | None = typer.Option(None, "--scoring-context-league-id"),
    interval: float | None = typer.Option(None),
    db_path: Path | None = typer.Option(None),
) -> None:
    if league_id and draft_id:
        raise typer.BadParameter("supply either --league-id or --draft-id, not both")
    settings = load_settings(sleeper_league_id=league_id, poll_seconds=interval, db_path=db_path)
    if not draft_id and not settings.sleeper_league_id:
        raise typer.BadParameter("a league ID is required")
    repository = SnapshotRepository(settings.db_path)
    client = _client(settings)
    try:

        def on_snapshot(snapshot: Any) -> None:
            stdout.print(f"{snapshot.pick_count} picks at {snapshot.observed_at.isoformat()}")

        if draft_id:
            watch_by_draft_id(
                client,
                repository,
                draft_id,
                settings.poll_seconds,
                scoring_context_league_id,
                on_snapshot=on_snapshot,
            )
        else:
            if scoring_context_league_id:
                raise typer.BadParameter("--scoring-context-league-id requires --draft-id")
            selected_league_id = settings.sleeper_league_id
            if selected_league_id is None:
                raise typer.BadParameter("a league ID is required")
            selected = select_draft(client.get_league_drafts(selected_league_id))
            draft = client.get_draft(str(selected["draft_id"]))
            watch_draft(
                client,
                repository,
                selected_league_id,
                draft,
                settings.poll_seconds,
                on_snapshot=on_snapshot,
            )
    except KeyboardInterrupt:
        diagnostic("Stopped.")
    finally:
        client.close()


@drafts_app.command("list")
def drafts_list(
    season: str | None = typer.Option(None),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Discover league-associated and standalone Sleeper drafts for the configured user."""

    def operation() -> dict[str, Any]:
        settings = load_settings(season=season, db_path=db_path)
        if not settings.sleeper_user_id and not settings.sleeper_username:
            raise ConfigurationError(
                "missing_user", "A configured Sleeper user is required to list drafts"
            )
        ensure_directories(settings)
        repository = SnapshotRepository(settings.db_path)
        client = _client(settings)
        try:
            user_id = settings.sleeper_user_id
            if not user_id:
                user = client.get_user(str(settings.sleeper_username))
                user_id = str(user["user_id"])
            drafts = discover_drafts(
                client, user_id, settings.season, repository.stored_draft_ids()
            )
        finally:
            client.close()
        return {"user_id": user_id, "season": settings.season, "drafts": drafts}

    _run("drafts list", json_output, operation, lambda result: render_drafts(result["drafts"]))


@players_app.command("sync")
def players_sync(
    force: bool = typer.Option(False, "--force", help="Bypass the 24-hour player cache."),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
    diagnostic_timings: bool = typer.Option(
        False, "--timings", help="Include structured phase timings in the result."
    ),
) -> None:
    """Synchronize the Sleeper NFL player directory."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        ensure_directories(settings)
        client = _client(settings)
        timings: dict[str, float] = {}
        identity_diagnostics: dict[str, Any] = {}
        try:
            snapshot, created, source = sync_players(
                client,
                IntelligenceRepository(settings.db_path),
                Path(app_dirs().user_cache_dir),
                force=force,
                timings=timings,
                diagnostics=identity_diagnostics,
            )
        finally:
            client.close()
        result = {"source": source, "created": created, "snapshot": snapshot}
        if diagnostic_timings:
            result["timings_seconds"] = timings
            result["identity_diagnostics"] = identity_diagnostics
        return result

    _run(
        "players sync",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} player directory "
            f"from {result['source']}"
        ),
    )


@players_app.command("search")
def players_search(
    query: str = typer.Argument(...),
    position: str | None = typer.Option(None),
    team: str | None = typer.Option(None),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Search the local player directory as of a timestamp."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        at = parse_timestamp(as_of) if as_of else datetime.now(UTC)
        results = IntelligenceRepository(settings.db_path).search_players(
            normalize_name(query), at, position, team
        )
        return {"as_of": at, "players": results}

    _run("players search", json_output, operation, lambda result: render_players(result["players"]))


@rankings_app.command("import")
def rankings_import(
    file: Path = typer.Argument(...),
    source: str = typer.Option(...),
    season: str = typer.Option(...),
    scoring: str = typer.Option(...),
    league_size: int = typer.Option(..., "--league-size"),
    source_version: str | None = typer.Option(None, "--source-version"),
    observed_at: str | None = typer.Option(None, "--observed-at"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Import an immutable ranking, ADP, or projection CSV snapshot."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        snapshot, created = import_rankings(
            file,
            IntelligenceRepository(settings.db_path),
            source,
            season,
            scoring,
            league_size,
            source_version,
            parse_timestamp(observed_at) if observed_at else None,
        )
        return {"created": created, "snapshot": snapshot}

    _run(
        "rankings import",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} ranking snapshot "
            f"{result['snapshot'].ranking_snapshot_id}"
        ),
    )


@rankings_app.command("reprocess")
def rankings_reprocess(
    snapshot_id: str = typer.Option(..., "--snapshot-id"),
    observed_at: str | None = typer.Option(None, "--observed-at"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Resolve an immutable ranking snapshot again with the current resolver."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        repository = IntelligenceRepository(settings.db_path)
        snapshot, created = reprocess_rankings(
            repository,
            snapshot_id,
            parse_timestamp(observed_at) if observed_at else None,
        )
        return {
            "created": created,
            "snapshot": snapshot,
            "match_method_counts": repository.ranking_match_method_counts(
                snapshot.ranking_snapshot_id
            ),
            "issues": repository.ranking_issues(snapshot.ranking_snapshot_id),
        }

    _run(
        "rankings reprocess",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} reprocessed ranking snapshot "
            f"{result['snapshot'].ranking_snapshot_id}"
        ),
    )


@rankings_app.command("list")
def rankings_list(
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List ranking snapshots."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        return {"snapshots": IntelligenceRepository(settings.db_path).ranking_snapshots()}

    _run(
        "rankings list",
        json_output,
        operation,
        lambda result: render_rankings(result["snapshots"]),
    )


@rankings_app.command("unresolved")
def rankings_unresolved(
    snapshot_id: str | None = typer.Option(None, "--snapshot-id"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List unresolved and ambiguous ranking rows."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        return {
            "issues": IntelligenceRepository(settings.db_path).ranking_issues(snapshot_id),
            "snapshot_id": snapshot_id,
        }

    _run(
        "rankings unresolved",
        json_output,
        operation,
        lambda result: render_ranking_issues(result["issues"]),
    )


@projections_app.command("import-cbs")
def projections_import_cbs(
    directory: Path = typer.Argument(...),
    source_version: str = typer.Option(..., "--source-version"),
    season: str = typer.Option("2026", "--season"),
    league_id: str | None = typer.Option(None, "--league-id"),
    observed_at: str | None = typer.Option(None, "--observed-at"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Import six locally saved CBS full-season projection pages atomically."""

    def operation() -> dict[str, Any]:
        settings = load_settings(sleeper_league_id=league_id, db_path=db_path)
        if not settings.sleeper_league_id:
            raise ConfigurationError(
                "missing_league_id", "A Sleeper league ID is required for projection scoring"
            )
        repository = IntelligenceRepository(settings.db_path)
        snapshot, created, positions = import_cbs_projections(
            directory,
            repository,
            source_version,
            settings.sleeper_league_id,
            parse_timestamp(observed_at) if observed_at else None,
            season,
        )
        return {"created": created, "snapshot": snapshot, "positions": positions}

    _run(
        "projections import-cbs",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} CBS projection snapshot "
            f"{result['snapshot'].projection_snapshot_id}"
        ),
    )


@projections_app.command("list")
def projections_list(
    as_of: str | None = typer.Option(None, "--as-of"),
    source: str | None = typer.Option(None, "--source"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List immutable statistical projection snapshots."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        repository = IntelligenceRepository(settings.db_path)
        if as_of:
            selected = repository.projection_at(parse_timestamp(as_of), source)
            return {"snapshots": [selected] if selected else [], "as_of": parse_timestamp(as_of)}
        return {"snapshots": repository.projection_snapshots(), "as_of": None}

    _run(
        "projections list",
        json_output,
        operation,
        lambda result: render_projections(result["snapshots"]),
    )


@projections_app.command("issues")
def projections_issues(
    snapshot_id: str | None = typer.Option(None, "--snapshot-id"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List unresolved and ambiguous statistical projection identities."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        return {
            "snapshot_id": snapshot_id,
            "issues": IntelligenceRepository(settings.db_path).projection_issues(snapshot_id),
        }

    _run(
        "projections issues",
        json_output,
        operation,
        lambda result: render_projection_issues(result["issues"]),
    )


@app.command("board")
def board(
    draft_id: str | None = typer.Option(None, "--draft-id"),
    source: str | None = typer.Option(None),
    position: str | None = typer.Option(None),
    limit: int = typer.Option(100, min=1),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Render the available-player board entirely from local snapshots."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        at = parse_timestamp(as_of) if as_of else datetime.now(UTC)
        results = IntelligenceRepository(settings.db_path).board(
            at,
            draft_id,
            None if draft_id else settings.sleeper_league_id,
            source,
            position,
            limit,
        )
        return {"as_of": at, "players": results}

    _run("board", json_output, operation, lambda result: render_board(result["players"]))


@app.command("recommend")
def recommend_command(
    draft_id: str | None = typer.Option(None, "--draft-id"),
    draft_slot: int | None = typer.Option(None, "--draft-slot", min=1),
    source: str | None = typer.Option(None, "--source"),
    limit: int = typer.Option(10, "--limit", min=1),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recommend who to draft using only immutable local snapshots."""

    def operation() -> Any:
        settings = load_settings(db_path=db_path)
        at = parse_timestamp(as_of) if as_of else datetime.now(UTC)
        return build_recommendation(
            IntelligenceRepository(settings.db_path),
            at,
            draft_id=draft_id,
            league_id=None if draft_id else settings.sleeper_league_id,
            sleeper_user_id=settings.sleeper_user_id,
            draft_slot=draft_slot,
            ranking_source=source,
            limit=limit,
        )

    _run("recommend", json_output, operation, render_recommendation)
