from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import typer
from pydantic import ValidationError
from rich.prompt import Prompt

from fantasy_war_room.bootstrap import (
    choose_league_id,
    context_data,
    generate_codex_config,
    readiness,
    resolve_effective_draft_configuration,
    resolve_mcp_launch_spec,
)
from fantasy_war_room.bootstrap import (
    draft_slot as resolve_setup_draft_slot,
)
from fantasy_war_room.config import (
    IntelligenceMode,
    RecommendationModelSelection,
    app_dirs,
    config_file_path,
    ensure_directories,
    for_resolved_sleeper_user,
    load_settings,
    save_settings,
    with_active_draft_session,
    with_league_context,
    with_scoring_context,
)
from fantasy_war_room.data_bootstrap import bootstrap_data, data_status
from fantasy_war_room.decision.models import RecommendationModelVersion
from fantasy_war_room.decision.survival_models import SurvivalModelVersion
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
from fantasy_war_room.market_imports import import_adp, import_team_schedule
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.service import DraftCopilotService
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
    render_survival,
    render_survival_evaluation,
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
from fantasy_war_room.strategy.load import load_strategy_profile, strategy_directory
from fantasy_war_room.survival import build_survival_response
from fantasy_war_room.survival_evaluation import evaluate_historical_survival

app = typer.Typer(
    help="Local-first, time-aware fantasy football decision data.", no_args_is_help=True
)
players_app = typer.Typer(help="Synchronize and search the local player directory.")
rankings_app = typer.Typer(help="Import and inspect ranking snapshots.")
projections_app = typer.Typer(help="Import and inspect statistical projection snapshots.")
drafts_app = typer.Typer(help="Discover Sleeper league and standalone drafts.")
strategies_app = typer.Typer(help="Inspect and validate strategy profiles.")
adp_app = typer.Typer(help="Import and inspect immutable ADP snapshots.")
schedules_app = typer.Typer(help="Import and inspect immutable team schedule snapshots.")
leagues_app = typer.Typer(help="Inspect and switch saved Sleeper league contexts.")
codex_app = typer.Typer(help="Configure the project-local Codex integration.")
mcp_app = typer.Typer(help="Configure a local read-only MCP client.")
data_app = typer.Typer(help="Acquire and inspect portable football intelligence.")
app.add_typer(players_app, name="players")
app.add_typer(rankings_app, name="rankings")
app.add_typer(projections_app, name="projections")
app.add_typer(drafts_app, name="drafts")
app.add_typer(strategies_app, name="strategies")
app.add_typer(adp_app, name="adp")
app.add_typer(schedules_app, name="schedules")
app.add_typer(leagues_app, name="leagues")
app.add_typer(codex_app, name="codex")
app.add_typer(mcp_app, name="mcp")
app.add_typer(data_app, name="data")

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _agent_status(settings: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1.0",
        "sleeper_account": {
            "username": settings.sleeper_username,
            "user_id": settings.sleeper_user_id,
        },
        "selected_league": settings.active_context,
        "active_session": settings.active_draft_session,
        "intelligence_mode": settings.intelligence_mode,
        "strategy": settings.active_strategy,
        "database": str(settings.db_path.expanduser().resolve()),
    }
    if not settings.sleeper_username:
        return _validated_agent_state(
            {
                **base,
                "state": "needs_input",
                "question": {
                    "id": "sleeper_username",
                    "prompt": "What is your Sleeper username?",
                    "choices": [],
                },
                "readiness": None,
                "next_actions": [],
            }
        )
    if settings.active_draft_session is None and settings.active_context is None:
        return _validated_agent_state(
            {
                **base,
                "state": "needs_action",
                "question": None,
                "readiness": None,
                "next_actions": [
                    {"id": "discover_leagues", "command": "fwr onboard --json", "safe": True}
                ],
            }
        )
    if settings.active_draft_session is None:
        return _validated_agent_state(
            {
                **base,
                "state": "needs_action",
                "question": None,
                "readiness": None,
                "next_actions": [
                    {
                        "id": "sync_draft",
                        "command": "fwr setup --non-interactive --json",
                        "safe": True,
                    }
                ],
            }
        )
    if settings.intelligence_mode is None:
        return _validated_agent_state(
            {
                **base,
                "state": "needs_input",
                "readiness": None,
                "next_actions": [],
                "question": {
                    "id": "intelligence_mode",
                    "prompt": "Which setup mode do you want?",
                    "choices": [
                        {
                            "id": "quick",
                            "name": "Quick",
                            "description": "Automatically acquired portable public market data",
                        },
                        {
                            "id": "personalized",
                            "name": "Personalized",
                            "description": (
                                "Quick mode with strategy customization reserved for the "
                                "next milestone"
                            ),
                        },
                        {
                            "id": "advanced",
                            "name": "Advanced",
                            "description": "User-supplied rankings and exact-scoring projections",
                        },
                    ],
                },
            }
        )
    ready = readiness(settings, repository_root=REPOSITORY_ROOT)
    actions: list[dict[str, Any]] = []
    checks = {item["name"]: item for item in ready["checks"]}
    if checks["player_directory"]["status"] != "pass":
        actions.append({"id": "sync_players", "command": "fwr players sync --json", "safe": True})
    required_missing = [
        item for item in ready["checks"] if item["required"] and item["status"] == "fail"
    ]
    if settings.intelligence_mode == "advanced":
        if checks["compatible_ranking"]["status"] != "pass":
            return _validated_agent_state(
                {
                    **base,
                    "state": "needs_input",
                    "question": {
                        "id": "advanced_rankings",
                        "prompt": (
                            "Which compatible ranking file and source metadata should FWR import?"
                        ),
                        "choices": [
                            {"id": "quick", "name": "Switch to Quick"},
                            {"id": "personalized", "name": "Switch to Personalized"},
                        ],
                        "required_fields": [
                            "path",
                            "source",
                            "source_version",
                            "season",
                            "scoring",
                            "league_size",
                        ],
                        "command": (
                            "fwr rankings import PATH --source SOURCE --source-version VERSION "
                            "--season SEASON --scoring SCORING --league-size TEAMS --json"
                        ),
                    },
                    "readiness": ready,
                    "next_actions": [],
                }
            )
        if checks["compatible_projection"]["status"] != "pass":
            return _validated_agent_state(
                {
                    **base,
                    "state": "needs_input",
                    "question": {
                        "id": "advanced_projections",
                        "prompt": "Which projection file and source metadata should FWR import?",
                        "choices": [
                            {"id": "quick", "name": "Switch to Quick"},
                            {"id": "personalized", "name": "Switch to Personalized"},
                        ],
                        "required_fields": ["path", "season", "source_version"],
                        "command": (
                            "fwr projections import-cbs PATH --season SEASON "
                            "--source-version VERSION --json"
                        ),
                    },
                    "readiness": ready,
                    "next_actions": [],
                }
            )
    if settings.intelligence_mode in {"quick", "personalized"} and any(
        item["name"] in {"compatible_adp", "compatible_market_board"} for item in required_missing
    ):
        actions.append(
            {
                "id": "refresh_public_intelligence",
                "command": "fwr data refresh --json",
                "safe": True,
            }
        )
    if ready["ready"] and checks["codex_mcp_configuration"]["status"] != "pass":
        actions.append(
            {
                "id": "configure_mcp",
                "command": "fwr mcp configure --client CLIENT --json",
                "safe": True,
            }
        )
    if not ready["ready"] and not actions:
        session = settings.active_draft_session
        command = (
            f"fwr sync --draft-id {session.draft_id} --json"
            if session is not None
            else "fwr onboard --json"
        )
        actions.append({"id": "refresh_active_draft", "command": command, "safe": True})
    return _validated_agent_state(
        {
            **base,
            "state": "ready" if ready["ready"] else "needs_action",
            "question": None,
            "readiness": ready,
            "next_actions": actions,
        }
    )


def _validated_agent_state(result: dict[str, Any]) -> dict[str, Any]:
    state = result.get("state")
    if state == "needs_input" and not isinstance(result.get("question"), dict):
        raise RuntimeError("needs_input onboarding state requires a structured question")
    if state == "needs_action" and not result.get("next_actions"):
        raise RuntimeError("needs_action onboarding state requires at least one next action")
    if state == "ready":
        readiness_value = result.get("readiness")
        if not isinstance(readiness_value, dict) or not readiness_value.get("ready"):
            raise RuntimeError("ready onboarding state requires complete readiness")
    return result


def _with_intelligence_mode(settings: Any, mode: IntelligenceMode) -> Any:
    contexts = dict(settings.league_contexts)
    context = settings.draft_configuration_context
    if context is not None:
        contexts[context.league_id] = context.model_copy(
            update={
                "recommendation_model": (
                    "baseline-1.0" if mode == "advanced" else "portable-market-1.0"
                ),
                "ranking_source": context.ranking_source if mode == "advanced" else None,
                "strategy": None if mode in {"quick", "personalized"} else context.strategy,
            }
        )
    return settings.model_copy(
        update={"intelligence_mode": mode, "league_contexts": contexts, "strategy": None}
    )


@app.command("status")
def status_command(json_output: bool = typer.Option(False, "--json")) -> None:
    """Report unified account, draft-session, intelligence, strategy, and MCP status."""
    _run(
        "status",
        json_output,
        lambda: _agent_status(load_settings()),
        lambda value: stdout.print(value),
    )


@app.command("onboard")
def onboard_command(
    username: str | None = typer.Option(None, "--username"),
    league_id: str | None = typer.Option(None, "--league-id"),
    mode: str | None = typer.Option(None, "--mode"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Advance or inspect the deterministic agent-oriented setup state machine."""

    def operation() -> dict[str, Any]:
        settings = load_settings(sleeper_username=username)
        if mode is not None:
            if mode not in {"quick", "personalized", "advanced"}:
                raise InputError(
                    "invalid_intelligence_mode", "--mode must be quick, personalized, or advanced"
                )
            settings = _with_intelligence_mode(settings, cast(IntelligenceMode, mode))
            save_settings(settings)
        if not settings.sleeper_username:
            return _agent_status(settings)
        if (
            settings.active_draft_session is None and settings.active_context is None
        ) or league_id is not None:
            client = _client(settings)
            try:
                user, leagues = discover_leagues(client, settings.sleeper_username, settings.season)
                account = for_resolved_sleeper_user(
                    settings,
                    user_id=str(user["user_id"]),
                    username=str(user.get("username") or settings.sleeper_username),
                )
                choices = [league.model_dump(mode="json") for league in leagues]
                selected = (
                    choose_league_id(
                        [league.league_id for league in leagues],
                        league_id,
                        non_interactive=True,
                    )
                    if league_id or len(leagues) <= 1
                    else None
                )
                if selected is None:
                    save_settings(account)
                    return _validated_agent_state(
                        {
                            **_agent_status(account),
                            "state": "needs_input",
                            "next_actions": [],
                            "question": {
                                "id": "league",
                                "prompt": "Which Sleeper league do you want to use?",
                                "choices": choices,
                            },
                        }
                    )
                configured = with_league_context(
                    account, league_id=selected, season=settings.season
                )
                if configured.intelligence_mode is not None:
                    configured = _with_intelligence_mode(configured, configured.intelligence_mode)
                ensure_directories(configured)
                repository = IntelligenceRepository(configured.db_path)
                snapshot, _ = sync_draft(client, repository, selected)
                sync_players(client, repository, Path(app_dirs().user_cache_dir))
                slot = resolve_setup_draft_slot(snapshot, configured.sleeper_user_id)
                configured = with_active_draft_session(
                    configured,
                    draft_id=snapshot.draft_id,
                    context_type="league",
                    season=configured.season,
                    source_league_id=selected,
                    scoring_context_league_id=selected,
                    draft_slot=slot,
                )
                save_settings(configured)
                settings = configured
            finally:
                client.close()
        return _agent_status(settings)

    _run("onboard", json_output, operation, lambda value: stdout.print(value))


@data_app.command("bootstrap")
def data_bootstrap_command(
    force: bool = typer.Option(False, "--force"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Acquire compatible public intelligence for the active league."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        ensure_directories(settings)
        return bootstrap_data(
            settings,
            cache_dir=Path(app_dirs().user_cache_dir),
            repository_root=REPOSITORY_ROOT,
            force=force,
        )

    def render(result: dict[str, Any]) -> None:
        stdout.print(
            f"Active league {result['active_league_id']}: "
            f"{result['format']['league_size']}-team "
            f"{result['format']['scoring_format']} {result['format']['draft_type']}"
        )
        for name, source in result["sources"].items():
            stdout.print(
                f"{name}: {source['status']} — {source.get('provider', source.get('message'))}"
            )
        stdout.print(
            "Recommendation readiness: "
            + ("READY" if result["recommendation_ready"] else "NOT READY")
        )

    _run("data bootstrap", json_output, operation, render)


@data_app.command("refresh")
def data_refresh_command(
    force: bool = typer.Option(False, "--force"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Refresh portable public intelligence for the active league."""

    def operation() -> dict[str, Any]:
        settings = load_settings(db_path=db_path)
        ensure_directories(settings)
        client = _client(settings)
        try:
            player, created, source = sync_players(
                client,
                IntelligenceRepository(settings.db_path),
                Path(app_dirs().user_cache_dir),
                force=force,
            )
        finally:
            client.close()
        result = bootstrap_data(
            settings,
            cache_dir=Path(app_dirs().user_cache_dir),
            repository_root=REPOSITORY_ROOT,
            force=force,
        )
        result["sources"]["player_directory"] = {
            "status": "acquired" if created else "unchanged",
            "provider": "sleeper",
            "snapshot_id": player.snapshot_id,
            "source": source,
        }
        return result

    _run("data refresh", json_output, operation, _render_data_refresh)


def _render_data_refresh(result: dict[str, Any]) -> None:
    sources = result["sources"]

    def state(source: dict[str, Any]) -> str:
        status = str(source["status"])
        if status in {"acquired", "unchanged", "current", "derived"}:
            return "ready"
        error = source.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"FAILED — {error['message']}"
        return status

    player = sources["player_directory"]
    adp = sources["adp"]
    schedule = sources["team_schedule"]
    board = adp.get("market_board")
    stdout.print(f"Sleeper players: {state(player)}")
    stdout.print(f"FFC ADP: {state(adp)}")
    stdout.print(
        "Portable market board: " + (state(board) if isinstance(board, dict) else "unavailable")
    )
    stdout.print(f"nflverse schedule: {state(schedule)}")
    stdout.print(
        "Recommendation readiness: " + ("READY" if result["recommendation_ready"] else "NOT READY")
    )


@data_app.command("status")
def data_status_command(
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Report compatible local intelligence and recommendation readiness."""
    _run(
        "data status",
        json_output,
        lambda: data_status(load_settings(db_path=db_path), repository_root=REPOSITORY_ROOT),
        lambda result: stdout.print(result),
    )


@adp_app.command("import")
def adp_import(
    path: Path,
    source: str = typer.Option(...),
    source_version: str = typer.Option(..., "--source-version"),
    season: str = typer.Option("2026"),
    scoring: str = typer.Option("ppr"),
    league_size: int = typer.Option(10, "--league-size"),
    draft_type: str = typer.Option("snake", "--draft-type"),
    observed_at: str | None = typer.Option(None, "--observed-at"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        repository = IntelligenceRepository(load_settings(db_path=db_path).db_path)
        snapshot, created = import_adp(
            path,
            repository,
            source=source,
            source_version=source_version,
            season=season,
            scoring_format=scoring,
            league_size=league_size,
            draft_type=draft_type,
            observed_at=parse_timestamp(observed_at) if observed_at else None,
        )
        return {
            "created": created,
            "snapshot": snapshot,
            "issues": repository.adp_issues(snapshot.adp_snapshot_id),
        }

    _run(
        "adp import",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} ADP snapshot "
            f"{result['snapshot'].adp_snapshot_id}"
        ),
    )


@adp_app.command("list")
def adp_list(
    db_path: Path | None = typer.Option(None), json_output: bool = typer.Option(False, "--json")
) -> None:
    _run(
        "adp list",
        json_output,
        lambda: {
            "snapshots": IntelligenceRepository(
                load_settings(db_path=db_path).db_path
            ).adp_snapshots()
        },
        lambda result: stdout.print(result),
    )


@adp_app.command("unresolved")
def adp_unresolved(
    snapshot_id: str | None = typer.Option(None, "--snapshot-id"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _run(
        "adp unresolved",
        json_output,
        lambda: {
            "snapshot_id": snapshot_id,
            "issues": IntelligenceRepository(load_settings(db_path=db_path).db_path).adp_issues(
                snapshot_id
            ),
        },
        lambda result: stdout.print(result),
    )


@schedules_app.command("import")
def schedules_import(
    path: Path,
    source: str = typer.Option(...),
    source_version: str = typer.Option(..., "--source-version"),
    season: str = typer.Option("2026"),
    observed_at: str | None = typer.Option(None, "--observed-at"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> dict[str, Any]:
        snapshot, created = import_team_schedule(
            path,
            IntelligenceRepository(load_settings(db_path=db_path).db_path),
            source=source,
            source_version=source_version,
            season=season,
            observed_at=parse_timestamp(observed_at) if observed_at else None,
        )
        return {"created": created, "snapshot": snapshot}

    _run(
        "schedules import",
        json_output,
        operation,
        lambda result: stdout.print(
            f"{'Stored' if result['created'] else 'Unchanged'} schedule snapshot "
            f"{result['snapshot'].schedule_snapshot_id}"
        ),
    )


@schedules_app.command("list")
def schedules_list(
    db_path: Path | None = typer.Option(None), json_output: bool = typer.Option(False, "--json")
) -> None:
    _run(
        "schedules list",
        json_output,
        lambda: {
            "snapshots": IntelligenceRepository(
                load_settings(db_path=db_path).db_path
            ).schedule_snapshots()
        },
        lambda result: stdout.print(result),
    )


def _local_copilot(
    draft_id: str, draft_slot: int | None, db_path: Path | None
) -> DraftCopilotService:
    settings = load_settings(db_path=db_path)
    effective = resolve_effective_draft_configuration(settings)
    if effective.ranking_source is None:
        raise ConfigurationError(
            "ranking_source_required",
            "Select a ranking source for the active league before using copilot helpers",
        )
    return DraftCopilotService(
        McpReadRepository(settings.db_path),
        draft_id=draft_id,
        sleeper_user_id=settings.sleeper_user_id,
        draft_slot=draft_slot,
        default_source=effective.ranking_source,
        default_model=effective.recommendation_model,
        strategy_profile=effective.strategy_profile,
    )


@app.command("market-context")
def market_context_command(
    draft_id: str = typer.Option(..., "--draft-id"),
    draft_slot: int | None = typer.Option(None, "--draft-slot"),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _run(
        "market-context",
        json_output,
        lambda: _local_copilot(draft_id, draft_slot, db_path).get_market_context(as_of=as_of)[0],
        lambda result: stdout.print(result),
    )


@app.command("opponent-demand")
def opponent_demand_command(
    draft_id: str = typer.Option(..., "--draft-id"),
    draft_slot: int | None = typer.Option(None, "--draft-slot"),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _run(
        "opponent-demand",
        json_output,
        lambda: _local_copilot(draft_id, draft_slot, db_path).get_opponent_demand(as_of=as_of)[0],
        lambda result: stdout.print(result),
    )


@strategies_app.command("list")
def strategies_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List built-in and installed strategy profiles."""

    def operation() -> dict[str, Any]:
        names = {"logan-ppr-2flex-1.0"}
        directory = strategy_directory()
        if directory.is_dir():
            names.update(path.stem for path in directory.glob("*.json"))
        return {"strategies": sorted(names)}

    _run("strategies list", json_output, operation, lambda value: stdout.print(value))


@strategies_app.command("show")
def strategies_show(
    strategy: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the normalized effective strategy profile."""
    _run(
        "strategies show",
        json_output,
        lambda: load_strategy_profile(strategy),
        lambda value: stdout.print_json(value.model_dump_json()),
    )


@strategies_app.command("validate")
def strategies_validate(
    path: Path,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate and normalize a strategy profile file without installing it."""
    _run(
        "strategies validate",
        json_output,
        lambda: load_strategy_profile(path),
        lambda value: stdout.print(f"Valid strategy: {value.profile_name}"),
    )


@strategies_app.command("install")
def strategies_install(
    path: Path,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Install a validated strategy profile in the XDG configuration directory."""

    def operation() -> dict[str, Any]:
        profile = load_strategy_profile(path)
        directory = strategy_directory()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{profile.profile_name}.json"
        destination.write_text(
            json.dumps(profile.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"profile": profile, "path": destination}

    _run("strategies install", json_output, operation, lambda value: stdout.print(value["path"]))


@strategies_app.command("active")
def strategies_active(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the active strategy selected by environment or user configuration."""

    def operation() -> dict[str, Any]:
        settings = load_settings()
        return {
            "strategy": settings.active_strategy,
            "profile": (
                load_strategy_profile(settings.active_strategy)
                if settings.active_strategy is not None
                else None
            ),
        }

    _run("strategies active", json_output, operation, lambda value: stdout.print(value))


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
        account_settings = for_resolved_sleeper_user(
            settings,
            user_id=str(user["user_id"]),
            username=str(user.get("username") or settings.sleeper_username),
        )
        ids = [league.league_id for league in leagues]
        requested = league_id or account_settings.sleeper_league_id
        chosen = choose_league_id(
            ids,
            requested,
            non_interactive=non_interactive or not typer.get_text_stream("stdin").isatty(),
        )
        if chosen is None:
            render_leagues(leagues)
            chosen = Prompt.ask("League ID", choices=ids, console=stdout)
        configured = with_league_context(
            account_settings,
            league_id=chosen,
            season=settings.season,
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


@app.command("setup")
def setup_command(
    username: str | None = typer.Option(None),
    season: str | None = typer.Option(None),
    league_id: str | None = typer.Option(None, "--league-id"),
    ranking_source: str | None = typer.Option(None, "--ranking-source"),
    recommendation_model: str | None = typer.Option(None, "--recommendation-model"),
    strategy: str | None = typer.Option(None, "--strategy"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Configure, synchronize, and report the selected league's bootstrap state."""

    def operation() -> dict[str, Any]:
        settings = load_settings(sleeper_username=username, season=season, db_path=db_path)
        client = _client(settings)
        try:
            user, leagues = discover_leagues(client, settings.sleeper_username, settings.season)
            account_settings = for_resolved_sleeper_user(
                settings,
                user_id=str(user["user_id"]),
                username=str(user.get("username") or settings.sleeper_username),
            )
            ids = [league.league_id for league in leagues]
            requested = league_id or account_settings.active_league_id
            selected = choose_league_id(
                ids,
                requested,
                non_interactive=non_interactive or not typer.get_text_stream("stdin").isatty(),
            )
            if selected is None:
                render_leagues(leagues)
                selected = Prompt.ask("League ID", choices=ids, console=stdout)
            configured = with_league_context(
                account_settings,
                league_id=selected,
                season=settings.season,
                ranking_source=ranking_source,
                recommendation_model=cast(
                    RecommendationModelSelection | None,
                    recommendation_model,
                ),
                strategy=strategy,
            )
            ensure_directories(configured)
            save_settings(configured)
            repository = IntelligenceRepository(configured.db_path)
            snapshot, draft_created = sync_draft(client, repository, selected)
            player_snapshot, player_created, player_source = sync_players(
                client,
                repository,
                Path(app_dirs().user_cache_dir),
            )
        finally:
            client.close()
        slot = resolve_setup_draft_slot(snapshot, configured.sleeper_user_id)
        configured = with_active_draft_session(
            configured,
            draft_id=snapshot.draft_id,
            context_type="league",
            season=configured.season,
            source_league_id=selected,
            scoring_context_league_id=selected,
            draft_slot=slot,
        )
        save_settings(configured)
        ready = readiness(configured, repository_root=REPOSITORY_ROOT)
        return {
            "schema_version": "1.0",
            "config_file": str(config_file_path()),
            "username": configured.sleeper_username,
            "user_id": configured.sleeper_user_id,
            "league_id": selected,
            "season": configured.season,
            "draft_id": snapshot.draft_id,
            "draft_slot": slot,
            "draft_slot_status": "pending" if slot is None else "resolved",
            "draft_snapshot_created": draft_created,
            "player_snapshot_id": player_snapshot.snapshot_id,
            "player_snapshot_created": player_created,
            "player_source": player_source,
            "draft_ready": ready["ready"],
            "readiness_checks": ready["checks"],
        }

    _run(
        "setup",
        json_output,
        operation,
        lambda result: stdout.print(
            f"Configured league {result['league_id']} and synchronized draft "
            f"{result['draft_id']} (slot {result['draft_slot_status']}). "
            "Run fwr draft-ready for intelligence checks."
        ),
    )


@leagues_app.command("list")
def leagues_list(json_output: bool = typer.Option(False, "--json")) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings()
        return {
            "schema_version": "1.0",
            "active_league_id": settings.active_league_id,
            "leagues": [
                {
                    **context.model_dump(mode="json"),
                    "active": league_id == settings.active_league_id,
                }
                for league_id, context in sorted(settings.league_contexts.items())
            ],
        }

    _run("leagues list", json_output, operation, lambda result: stdout.print(result))


@leagues_app.command("use")
def leagues_use(league_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    def operation() -> dict[str, Any]:
        settings = load_settings()
        context = settings.league_contexts.get(league_id)
        if context is None:
            raise ConfigurationError(
                "league_context_not_found",
                "League context is not saved; run fwr setup --league-id first",
                {"league_id": league_id, "available_league_ids": sorted(settings.league_contexts)},
            )
        selected = settings.model_copy(
            update={
                "active_league_id": league_id,
                "sleeper_league_id": league_id,
                "season": context.season,
                "strategy": None,
                "active_draft_session": None,
                "active_draft_session_invalidated": True,
            }
        )
        save_settings(selected)
        return context_data(selected)

    _run(
        "leagues use",
        json_output,
        operation,
        lambda result: stdout.print(f"Active league: {result['active_league_id']}"),
    )


@app.command("context")
def context_command(json_output: bool = typer.Option(False, "--json")) -> None:
    _run(
        "context",
        json_output,
        lambda: context_data(load_settings()),
        lambda result: stdout.print(result),
    )


@app.command("draft-ready")
def draft_ready_command(
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _run(
        "draft-ready",
        json_output,
        lambda: readiness(load_settings(db_path=db_path), repository_root=REPOSITORY_ROOT),
        lambda result: stdout.print("READY" if result["ready"] else "NOT READY", result),
    )


@codex_app.command("configure")
def codex_configure(json_output: bool = typer.Option(False, "--json")) -> None:
    _run(
        "codex configure",
        json_output,
        lambda: generate_codex_config(load_settings(), repository_root=REPOSITORY_ROOT),
        lambda result: stdout.print(f"Wrote {result['path']}. Restart Codex in this repository."),
    )


@mcp_app.command("configure")
def mcp_configure(
    client: str = typer.Option(..., "--client"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Configure Codex or Claude Code from the same resolved launch specification."""

    def operation() -> dict[str, Any]:
        settings = load_settings()
        if client == "codex":
            return {
                "client": client,
                **generate_codex_config(settings, repository_root=REPOSITORY_ROOT),
            }
        if client != "claude":
            raise InputError("unsupported_mcp_client", "--client must be codex or claude")
        spec = resolve_mcp_launch_spec(settings, repository_root=REPOSITORY_ROOT)
        command = [
            "claude",
            "mcp",
            "add",
            "--scope",
            "local",
            "--transport",
            "stdio",
            "fantasy-war-room",
            "--",
            spec.executable,
            *spec.arguments,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=spec.working_directory,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConfigurationError(
                "claude_cli_not_found",
                "Claude Code CLI is not installed or not on PATH",
                {"command": command},
            ) from exc
        if completed.returncode != 0:
            raise ConfigurationError(
                "claude_mcp_configuration_failed",
                "Claude Code could not register the FWR MCP server",
                {"stderr": completed.stderr.strip(), "command": command},
            )
        return {"client": client, **spec.__dict__, "configured": True, "command": command}

    _run(
        "mcp configure",
        json_output,
        operation,
        lambda result: stdout.print(f"Configured {result['client']}. Restart the client."),
    )


def _draft_id_from_selector(value: str) -> str:
    candidate = value.strip().rstrip("/")
    if "://" in candidate:
        match = re.search(r"/draft/(?:nfl/)?([A-Za-z0-9_-]+)(?:/|$)", candidate)
        if match is None:
            raise InputError("invalid_draft_url", "Sleeper draft URL does not contain a draft ID")
        return match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
        raise InputError("invalid_draft_id", "Draft ID contains unsupported characters")
    return candidate


@drafts_app.command("connect")
def drafts_connect(
    draft_or_url: str,
    scoring_context_league_id: str | None = typer.Option(None, "--scoring-context-league-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Synchronize and select one exact Sleeper draft or mock."""

    def operation() -> dict[str, Any]:
        settings = load_settings()
        draft_id = _draft_id_from_selector(draft_or_url)
        ensure_directories(settings)
        client = _client(settings)
        discovered_contexts: list[dict[str, Any]] = []
        try:
            snapshot, created = sync_by_draft_id(
                client, SnapshotRepository(settings.db_path), draft_id, scoring_context_league_id
            )
            draft_season = str(snapshot.draft.get("season") or settings.season)
            if (
                snapshot.draft_context_type == "standalone"
                and snapshot.scoring_context is None
                and settings.sleeper_user_id is not None
            ):
                discovered_contexts = client.get_user_leagues(
                    settings.sleeper_user_id, draft_season
                )
        finally:
            client.close()
        slot = resolve_setup_draft_slot(snapshot, settings.sleeper_user_id)
        season = draft_season
        if snapshot.scoring_context_league_id is not None:
            settings = with_scoring_context(
                settings,
                league_id=snapshot.scoring_context_league_id,
                season=season,
            )
            if settings.intelligence_mode is not None:
                settings = _with_intelligence_mode(settings, settings.intelligence_mode)
        selected = with_active_draft_session(
            settings,
            draft_id=draft_id,
            context_type=cast(Any, snapshot.draft_context_type),
            season=season,
            source_league_id=snapshot.source_league_id,
            scoring_context_league_id=snapshot.scoring_context_league_id,
            draft_slot=slot,
        )
        save_settings(selected)
        if snapshot.draft_context_type == "standalone" and snapshot.scoring_context is None:
            choices_by_id: dict[str, dict[str, Any]] = {
                item.league_id: {
                    "id": item.league_id,
                    "name": item.league_id,
                    "season": item.season,
                }
                for item in settings.league_contexts.values()
                if item.season == season
            }
            for league in discovered_contexts:
                choice_id = str(league["league_id"])
                choices_by_id[choice_id] = {
                    "id": choice_id,
                    "name": str(league.get("name") or choice_id),
                    "teams": int(league.get("total_rosters") or 0),
                    "season": str(league.get("season") or season),
                }
            choices = [choices_by_id[key] for key in sorted(choices_by_id)]
            return {
                "schema_version": "1.0",
                "state": "needs_input",
                "active_session": selected.active_draft_session,
                "question": {
                    "id": "scoring_context_league",
                    "prompt": (
                        "Which saved league should provide scoring context for this "
                        "standalone mock?"
                    ),
                    "choices": choices,
                },
                "created": created,
            }
        return {
            "schema_version": "1.0",
            "state": "connected",
            "created": created,
            "active_session": selected.active_draft_session,
        }

    _run("drafts connect", json_output, operation, lambda result: stdout.print(result))


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
    session = settings.active_draft_session
    if draft_id is None and league_id is None and settings.active_draft_session_invalidated:
        raise typer.BadParameter(
            "the previous draft session was invalidated; run fwr onboard before watching"
        )
    if draft_id is None and league_id is None and session is not None:
        draft_id = session.draft_id
        scoring_context_league_id = session.scoring_context_league_id
    if not draft_id and not settings.sleeper_league_id:
        raise typer.BadParameter("an active draft session, --draft-id, or --league-id is required")
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


@app.command("survival")
def survival_command(
    player_ids: list[str] = typer.Option(..., "--player-id"),
    draft_id: str | None = typer.Option(None, "--draft-id"),
    draft_slot: int | None = typer.Option(None, "--draft-slot", min=1),
    simulations: int = typer.Option(5_000, "--simulations", min=1, max=100_000),
    seed: int = typer.Option(0, "--seed", min=0, max=2**64 - 1),
    model: SurvivalModelVersion = typer.Option("adp-only-1.0", "--survival-model"),
    adp_source: str | None = typer.Option(None, "--adp-source"),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Simulate whether passed candidates remain available at the target user pick."""

    def operation() -> Any:
        if not 1 <= len(player_ids) <= 20:
            raise InputError(
                "invalid_player_count", "survival requires between 1 and 20 --player-id values"
            )
        settings = load_settings(db_path=db_path)
        at = parse_timestamp(as_of) if as_of else datetime.now(UTC)
        return build_survival_response(
            IntelligenceRepository(settings.db_path),
            at,
            draft_id=draft_id,
            league_id=None if draft_id else settings.sleeper_league_id,
            sleeper_user_id=settings.sleeper_user_id,
            draft_slot=draft_slot,
            candidate_player_ids=tuple(player_ids),
            simulation_count=simulations,
            seed=seed,
            model_version=model,
            adp_source=adp_source,
        )

    _run("survival", json_output, operation, render_survival)


@app.command("survival-evaluate")
def survival_evaluate_command(
    draft_id: str = typer.Option(..., "--draft-id"),
    draft_slot: int = typer.Option(..., "--draft-slot", min=1),
    simulations: int = typer.Option(5_000, "--simulations", min=1, max=100_000),
    seed: int = typer.Option(0, "--seed", min=0, max=2**64 - 1),
    adp_source: str | None = typer.Option(None, "--adp-source"),
    ranking_source: str | None = typer.Option(None, "--source"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Compare existing survival models against completed local draft history."""

    def operation() -> Any:
        settings = load_settings(db_path=db_path)
        effective = resolve_effective_draft_configuration(settings)
        selected_model = effective.recommendation_model or "baseline-1.0"
        return evaluate_historical_survival(
            IntelligenceRepository(settings.db_path),
            draft_id=draft_id,
            draft_slot=draft_slot,
            seed=seed,
            simulation_count=simulations,
            adp_source=adp_source,
            ranking_source=ranking_source or effective.ranking_source,
            recommendation_model=selected_model,
        )

    _run("survival-evaluate", json_output, operation, render_survival_evaluation)


@app.command("recommend")
def recommend_command(
    draft_id: str | None = typer.Option(None, "--draft-id"),
    draft_slot: int | None = typer.Option(None, "--draft-slot", min=1),
    source: str | None = typer.Option(None, "--source"),
    model: RecommendationModelVersion | None = typer.Option(None, "--model"),
    strategy: str | None = typer.Option(None, "--strategy"),
    limit: int = typer.Option(10, "--limit", min=1),
    as_of: str | None = typer.Option(None, "--as-of"),
    db_path: Path | None = typer.Option(None, "--db-path"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recommend who to draft using only immutable local snapshots."""

    def operation() -> Any:
        settings = load_settings(db_path=db_path)
        effective = resolve_effective_draft_configuration(settings)
        profile = (
            load_strategy_profile(strategy) if strategy is not None else effective.strategy_profile
        )
        selected_model = (
            profile.required_raw_model
            if strategy is not None and profile is not None
            else (
                model or ("baseline-1.0" if source is not None else effective.recommendation_model)
            )
        )
        selected_source = source or (
            profile.required_ranking_source
            if strategy is not None and profile is not None
            else effective.ranking_source
        )
        if profile is not None and model is not None and model != profile.required_raw_model:
            raise InputError(
                "strategy_model_conflict",
                "Explicit recommendation model conflicts with the strategy profile",
                {"requested": model, "required": profile.required_raw_model},
            )
        if profile is not None and source is not None and source != profile.required_ranking_source:
            raise InputError(
                "strategy_ranking_source_conflict",
                "Explicit ranking source conflicts with the strategy profile",
                {"requested": source, "required": profile.required_ranking_source},
            )
        at = parse_timestamp(as_of) if as_of else datetime.now(UTC)
        return build_recommendation(
            IntelligenceRepository(settings.db_path),
            at,
            draft_id=draft_id,
            league_id=None if draft_id else settings.sleeper_league_id,
            sleeper_user_id=settings.sleeper_user_id,
            draft_slot=draft_slot,
            ranking_source=selected_source,
            model_version=selected_model,
            limit=limit,
            strategy_profile=profile,
        )

    _run("recommend", json_output, operation, render_recommendation)
