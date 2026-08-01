from __future__ import annotations

from collections.abc import Callable
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
from fantasy_war_room.errors import ConfigurationError, ExitCode, FwrError, NotFoundError
from fantasy_war_room.rendering import diagnostic, emit_json, render_leagues, stdout
from fantasy_war_room.repository import SnapshotRepository
from fantasy_war_room.services import discover as discover_leagues
from fantasy_war_room.services import parse_timestamp, select_draft
from fantasy_war_room.services import sync as sync_draft
from fantasy_war_room.services import watch as watch_draft
from fantasy_war_room.sleeper import SleeperClient

app = typer.Typer(help="Local-first Sleeper draft state history.", no_args_is_help=True)


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
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings(sleeper_league_id=league_id, db_path=db_path)
        ensure_directories(settings)
        client = _client(settings)
        try:
            snapshot, created = sync_draft(
                client, SnapshotRepository(settings.db_path), settings.sleeper_league_id
            )
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
    interval: float | None = typer.Option(None),
    db_path: Path | None = typer.Option(None),
) -> None:
    settings = load_settings(sleeper_league_id=league_id, poll_seconds=interval, db_path=db_path)
    if not settings.sleeper_league_id:
        raise typer.BadParameter("a league ID is required")
    repository = SnapshotRepository(settings.db_path)
    client = _client(settings)
    try:
        draft = client.get_draft(
            str(select_draft(client.get_league_drafts(settings.sleeper_league_id))["draft_id"])
        )
        watch_draft(
            client,
            repository,
            settings.sleeper_league_id,
            draft,
            settings.poll_seconds,
            on_snapshot=lambda snapshot: stdout.print(
                f"{snapshot.pick_count} picks at {snapshot.observed_at.isoformat()}"
            ),
        )
    except KeyboardInterrupt:
        diagnostic("Stopped.")
    finally:
        client.close()
