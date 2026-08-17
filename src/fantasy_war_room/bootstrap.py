from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb

from fantasy_war_room.config import Settings
from fantasy_war_room.decision.models import RecommendationModelVersion
from fantasy_war_room.errors import ConfigurationError, InputError
from fantasy_war_room.models import Snapshot
from fantasy_war_room.repository import (
    IntelligenceRepository,
    _canonical_hash,
    _recommendation_draft_settings,
    _recommendation_league_format,
    _recommendation_roster_configuration,
    _recommendation_scoring_format,
)
from fantasy_war_room.strategy.adjust import validate_strategy_compatibility
from fantasy_war_room.strategy.load import load_strategy_profile
from fantasy_war_room.strategy.models import StrategyProfile

RECOMMENDATION_DEFAULT = "baseline-1.0"
PROJECTION_DEFAULT = "cbs"
MANAGED_START = "# BEGIN FWR MANAGED MCP"
MANAGED_END = "# END FWR MANAGED MCP"


@dataclass(frozen=True)
class EffectiveDraftConfiguration:
    recommendation_model: RecommendationModelVersion
    ranking_source: str | None
    strategy: str | None
    strategy_profile: StrategyProfile | None


def resolve_effective_draft_configuration(settings: Settings) -> EffectiveDraftConfiguration:
    """Resolve explicit context choices, strategy requirements, then portable defaults."""
    context = settings.active_context
    if context is None:
        return EffectiveDraftConfiguration(
            recommendation_model="baseline-1.0",
            ranking_source=None,
            strategy=None,
            strategy_profile=None,
        )
    profile = load_strategy_profile(context.strategy) if context.strategy else None
    model = context.recommendation_model
    source = context.ranking_source
    conflicts: dict[str, dict[str, str]] = {}
    if profile is not None:
        if model is not None and model != profile.required_raw_model:
            conflicts["recommendation_model"] = {
                "configured": model,
                "required": profile.required_raw_model,
            }
        if source is not None and source != profile.required_ranking_source:
            conflicts["ranking_source"] = {
                "configured": source,
                "required": profile.required_ranking_source,
            }
        if conflicts:
            raise ConfigurationError(
                "strategy_configuration_conflict",
                "Active league choices conflict with the selected strategy profile",
                {"strategy": context.strategy, "conflicts": conflicts},
            )
        model = profile.required_raw_model
        source = profile.required_ranking_source
    return EffectiveDraftConfiguration(
        recommendation_model=cast(RecommendationModelVersion, model or RECOMMENDATION_DEFAULT),
        ranking_source=source,
        strategy=context.strategy,
        strategy_profile=profile,
    )


def choose_league_id(
    league_ids: list[str], requested: str | None, *, non_interactive: bool
) -> str | None:
    """Apply the deterministic portion of setup league selection."""
    if requested is not None:
        if requested not in league_ids:
            raise ConfigurationError(
                "league_not_available",
                "League does not belong to this user and season",
                {"available_league_ids": league_ids},
            )
        return requested
    if not league_ids:
        raise ConfigurationError("no_leagues", "No NFL leagues found for the selected season")
    if len(league_ids) == 1:
        return league_ids[0]
    if non_interactive:
        raise ConfigurationError(
            "league_selection_required",
            "Multiple leagues found; supply --league-id",
            {"available_league_ids": league_ids},
        )
    return None


def draft_slot(snapshot: Snapshot, sleeper_user_id: str | None) -> int | None:
    order = snapshot.draft.get("draft_order")
    if sleeper_user_id and isinstance(order, dict) and sleeper_user_id in order:
        try:
            return int(order[sleeper_user_id])
        except (TypeError, ValueError):
            return None
    slots = {
        int(pick["draft_slot"])
        for pick in snapshot.picks
        if sleeper_user_id
        and str(pick.get("picked_by")) == sleeper_user_id
        and pick.get("draft_slot") is not None
    }
    return next(iter(slots)) if len(slots) == 1 else None


def context_data(settings: Settings) -> dict[str, Any]:
    context = settings.active_context
    return {
        "schema_version": "1.0",
        "username": settings.sleeper_username,
        "user_id": settings.sleeper_user_id,
        "active_league_id": settings.active_league_id,
        "active_context": context,
        "database": str(settings.db_path.expanduser().resolve()),
    }


def readiness(settings: Settings, *, repository_root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, required: bool, status: str, message: str, **details: Any) -> None:
        checks.append(
            {
                "name": name,
                "required": required,
                "status": status,
                "message": message,
                "details": details,
            }
        )

    context = settings.active_context
    configured = bool(settings.sleeper_username and settings.sleeper_user_id)
    check(
        "user_configuration",
        True,
        "pass" if configured else "fail",
        "Sleeper user configured" if configured else "Run fwr setup --username USERNAME",
    )
    check(
        "active_league",
        True,
        "pass" if context else "fail",
        "Active league selected" if context else "Run fwr setup and select a league",
    )
    effective = resolve_effective_draft_configuration(settings) if context is not None else None
    if context is None or not settings.db_path.expanduser().exists():
        check(
            "synchronized_draft_snapshot",
            True,
            "fail",
            "No local database or active league snapshot",
        )
        return _readiness_result(
            settings,
            checks,
            None,
            None,
            strategy_selected=bool(effective and effective.strategy),
        )

    repository = IntelligenceRepository(settings.db_path)
    repository.initialize()
    now = datetime.now(UTC)
    with duckdb.connect(str(repository.path)) as connection:
        row = connection.execute(
            "SELECT * FROM draft_snapshots WHERE league_id=? "
            "ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
            [context.league_id],
        ).fetchone()
        if row is None:
            check("synchronized_draft_snapshot", True, "fail", "Run fwr setup or fwr sync")
            return _readiness_result(
                settings,
                checks,
                None,
                None,
                strategy_selected=bool(effective and effective.strategy),
            )
        snapshot = repository.state_at(str(row[2]), now)
        assert snapshot is not None
        check(
            "synchronized_draft_snapshot",
            True,
            "pass",
            "Draft snapshot is available",
            snapshot_id=snapshot.snapshot_id,
        )
        check(
            "current_draft_id", True, "pass", "Current draft identified", draft_id=snapshot.draft_id
        )

        format_error: str | None = None
        scoring_format: str | None = None
        team_count: int | None = None
        scoring_hash: str | None = None
        try:
            team_count, _, draft_type = _recommendation_draft_settings(snapshot)
            league_type, keeper_status = _recommendation_league_format(
                snapshot.scoring_context or {}
            )
            roster = _recommendation_roster_configuration(snapshot.scoring_context or {})
            if roster.qb != 1:
                raise InputError(
                    "unsupported_roster_format",
                    "Only single-quarterback leagues are currently supported",
                )
            scoring = (snapshot.scoring_context or {}).get("scoring_settings")
            if not isinstance(scoring, dict):
                raise InputError(
                    "incompatible_scoring_context",
                    "Selected league has no numeric scoring settings",
                )
            normalized = {str(key): float(value) for key, value in scoring.items()}
            scoring_format = _recommendation_scoring_format(normalized)
            scoring_hash = _canonical_hash(normalized)
            if draft_type != "snake" or league_type != "redraft" or keeper_status != "non_keeper":
                raise InputError(
                    "unsupported_league_format", "League is outside the supported format"
                )
        except Exception as exc:
            format_error = getattr(exc, "message", str(exc))
        check(
            "supported_format",
            True,
            "fail" if format_error else "pass",
            format_error or "NFL redraft snake single-QB non-keeper format is supported",
        )

        slot = draft_slot(snapshot, settings.sleeper_user_id)
        check(
            "draft_slot",
            True,
            "pending" if slot is None else "pass",
            "Sleeper has not published the user's draft slot yet"
            if slot is None
            else "Draft slot resolved",
            draft_slot=slot,
        )

        player = connection.execute(
            "SELECT snapshot_id FROM player_directory_snapshots "
            "WHERE provider='sleeper' AND sport='nfl' "
            "ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        check(
            "player_directory",
            True,
            "pass" if player else "fail",
            "Player directory is available" if player else "Run fwr players sync",
            snapshot_id=str(player[0]) if player else None,
        )

        ranking_rows: list[tuple[Any, ...]] = []
        ranking_scoring = {
            "full_ppr": "ppr",
            "half_ppr": "half_ppr",
            "standard": "standard",
            "custom": "custom",
        }.get(scoring_format or "")
        if ranking_scoring is not None and team_count is not None:
            ranking_rows = connection.execute(
                "SELECT source, ranking_snapshot_id FROM ranking_snapshots "
                "WHERE season=? AND scoring_format=? AND league_size=? "
                "ORDER BY observed_at DESC",
                [context.season, ranking_scoring, team_count],
            ).fetchall()
        sources = sorted({str(item[0]) for item in ranking_rows})
        assert effective is not None
        selected_source = effective.ranking_source
        source_policy = (
            "strategy_requirement"
            if effective.strategy_profile and context.ranking_source is None
            else "configured"
        )
        if selected_source is None and len(sources) == 1:
            selected_source, source_policy = sources[0], "only_compatible_source"
        ranking = next((item for item in ranking_rows if item[0] == selected_source), None)
        check(
            "compatible_ranking",
            True,
            "pass" if ranking else "fail",
            "Compatible ranking snapshot selected"
            if ranking
            else "Import compatible rankings and select ranking_source",
            source=selected_source,
            resolution_policy=source_policy,
            compatible_sources=sources,
            acquisition="user_supplied",
            optional_provider="FantasyPros (credentials required; adapter not implemented)",
            snapshot_id=str(ranking[1]) if ranking else None,
        )

        projection = None
        if scoring_hash is not None:
            projection = connection.execute(
                "SELECT projection_snapshot_id FROM projection_snapshots "
                "WHERE season=? AND source=? AND scoring_settings_hash=? "
                "ORDER BY observed_at DESC LIMIT 1",
                [context.season, PROJECTION_DEFAULT, scoring_hash],
            ).fetchone()
        check(
            "compatible_projection",
            True,
            "pass" if projection else "fail",
            "Compatible projection snapshot is available"
            if projection
            else "Import projections for this league's exact scoring settings",
            source=PROJECTION_DEFAULT,
            snapshot_id=str(projection[0]) if projection else None,
            acquisition="user_supplied",
            optional_provider="FantasyPros (credentials required; adapter not implemented)",
        )

        scoring_key = ranking_scoring if ranking_scoring != "custom" else None
        adp = None
        if scoring_key and team_count is not None:
            adp = connection.execute(
                "SELECT adp_snapshot_id, source FROM adp_snapshots "
                "WHERE season=? AND league_size=? AND scoring_format=? "
                "AND draft_type='snake' ORDER BY observed_at DESC LIMIT 1",
                [context.season, team_count, scoring_key],
            ).fetchone()
        check(
            "compatible_adp",
            False,
            "pass" if adp else "missing",
            "Compatible ADP is available"
            if adp
            else "Optional: import compatible ADP for market context",
            snapshot_id=str(adp[0]) if adp else None,
            source=str(adp[1]) if adp else None,
            acquisition="automatic",
            command="fwr data bootstrap",
        )
        schedule = connection.execute(
            "SELECT schedule_snapshot_id, source FROM team_schedule_snapshots "
            "WHERE season=? ORDER BY observed_at DESC LIMIT 1",
            [context.season],
        ).fetchone()
        check(
            "team_schedule",
            False,
            "pass" if schedule else "missing",
            "Team schedule/bye data is available"
            if schedule
            else "Optional: import team schedule/bye data",
            snapshot_id=str(schedule[0]) if schedule else None,
            source=str(schedule[1]) if schedule else None,
            acquisition="automatic",
            command="fwr data bootstrap",
        )

    model = effective.recommendation_model
    check(
        "recommendation_model",
        True,
        "pass",
        "Recommendation model resolved",
        model=model,
        resolution_policy=(
            "strategy_requirement"
            if effective.strategy_profile and context.recommendation_model is None
            else ("configured" if context.recommendation_model else "portable_default")
        ),
    )
    if effective.strategy_profile is not None:
        try:
            inputs = repository.recommendation_inputs(
                now,
                draft_id=snapshot.draft_id,
                league_id=None,
                sleeper_user_id=settings.sleeper_user_id,
                draft_slot=slot,
                ranking_source=selected_source,
            )
            validate_strategy_compatibility(
                inputs,
                effective.strategy_profile,
                raw_model=model,
            )
            check(
                "strategy",
                True,
                "pass",
                "Selected strategy and its dependencies are compatible",
                strategy=effective.strategy,
            )
        except Exception as exc:
            check(
                "strategy",
                True,
                "fail",
                getattr(exc, "message", str(exc)),
                strategy=effective.strategy,
            )
    else:
        check("strategy", False, "skipped", "No personalized strategy selected")
    codex_path = repository_root / ".codex" / "config.toml"
    configured_codex = (
        codex_path.exists()
        and "[mcp_servers.fantasy-war-room]" in codex_path.read_text(encoding="utf-8")
    )
    check(
        "codex_mcp_configuration",
        False,
        "pass" if configured_codex else "missing",
        "Project FWR MCP configuration exists" if configured_codex else "Run fwr codex configure",
        path=str(codex_path),
    )
    return _readiness_result(
        settings,
        checks,
        snapshot.draft_id,
        slot,
        ranking_source=selected_source,
        recommendation_model=model,
        strategy=effective.strategy,
        strategy_selected=effective.strategy is not None,
    )


def _readiness_result(
    settings: Settings,
    checks: list[dict[str, Any]],
    draft_id: str | None,
    slot: int | None,
    strategy_selected: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    definitions = {
        "user_configuration": (True, "Configure a Sleeper user"),
        "active_league": (True, "Select an active league"),
        "supported_format": (True, "Synchronize a supported league format"),
        "synchronized_draft_snapshot": (True, "Synchronize the current draft"),
        "current_draft_id": (True, "Identify the current draft"),
        "draft_slot": (True, "Draft slot is pending or unavailable"),
        "player_directory": (True, "Synchronize the player directory"),
        "compatible_ranking": (True, "Import compatible ranking data"),
        "compatible_projection": (True, "Import compatible projection data"),
        "compatible_adp": (False, "Optional compatible ADP is unavailable"),
        "team_schedule": (False, "Optional team schedule/bye data is unavailable"),
        "recommendation_model": (True, "Resolve a recommendation model"),
        "strategy": (
            strategy_selected,
            "Validate the selected strategy and its required intelligence"
            if strategy_selected
            else "No personalized strategy selected",
        ),
        "codex_mcp_configuration": (False, "Run fwr codex configure"),
    }
    present = {item["name"] for item in checks}
    for name, (required, message) in definitions.items():
        if name not in present:
            checks.append(
                {
                    "name": name,
                    "required": required,
                    "status": "fail"
                    if required
                    else ("skipped" if name == "strategy" else "missing"),
                    "message": message,
                    "details": {},
                }
            )
    ready = all(item["status"] == "pass" for item in checks if item["required"])
    return {
        "schema_version": "1.0",
        "ready": ready,
        "active_league_id": settings.active_league_id,
        "draft_id": draft_id,
        "draft_slot": slot,
        "checks": checks,
        **extra,
    }


def generate_codex_config(settings: Settings, *, repository_root: Path) -> dict[str, Any]:
    effective = resolve_effective_draft_configuration(settings)
    state = readiness(settings, repository_root=repository_root)
    failures = [item for item in state["checks"] if item["required"] and item["status"] != "pass"]
    if failures:
        raise ConfigurationError(
            "codex_context_incomplete",
            "Active draft context is not sufficient to configure Codex",
            {"checks": failures},
        )
    context = settings.active_context
    assert context is not None
    args = [
        "run",
        "--project",
        str(repository_root),
        "fwr-mcp",
        "--draft-id",
        str(state["draft_id"]),
        "--draft-slot",
        str(state["draft_slot"]),
        "--source",
        str(state["ranking_source"]),
        "--model",
        str(state["recommendation_model"]),
        "--database",
        str(settings.db_path.expanduser().resolve()),
    ]
    if effective.strategy:
        args.extend(["--strategy", effective.strategy])
    uv = shutil.which("uv") or "uv"
    block_lines = [
        "[mcp_servers.fantasy-war-room]",
        f"command = {json.dumps(uv)}",
        "args = [",
        *(f"  {json.dumps(arg)}," for arg in args),
        "]",
        f"cwd = {json.dumps(str(repository_root))}",
        "startup_timeout_sec = 15",
        "tool_timeout_sec = 30",
        "required = false",
    ]
    block = "\n".join(block_lines) + "\n"
    managed_block = f"{MANAGED_START}\n{block}{MANAGED_END}\n"
    path = repository_root / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = _update_codex_toml(existing, managed_block)
    path.write_text(updated, encoding="utf-8")
    return {
        "schema_version": "1.0",
        "path": str(path),
        "written": True,
        "draft_id": state["draft_id"],
        "draft_slot": state["draft_slot"],
        "ranking_source": state["ranking_source"],
        "recommendation_model": state["recommendation_model"],
        "strategy": effective.strategy,
        "database": str(settings.db_path.expanduser().resolve()),
        "working_directory": str(repository_root),
        "toml_block": block,
    }


def _update_codex_toml(existing: str, managed_block: str) -> str:
    try:
        parsed = tomllib.loads(existing) if existing.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            "invalid_codex_config",
            "Existing .codex/config.toml is malformed; FWR did not modify it",
            {"error": str(exc)},
        ) from exc
    marker_count = (existing.count(MANAGED_START), existing.count(MANAGED_END))
    if marker_count not in {(0, 0), (1, 1)}:
        raise ConfigurationError(
            "invalid_managed_codex_block",
            "FWR managed markers are missing or duplicated; FWR did not modify the file",
            {"start_markers": marker_count[0], "end_markers": marker_count[1]},
        )
    managed_pattern = re.compile(
        rf"(?ms)^{re.escape(MANAGED_START)}\n.*?^{re.escape(MANAGED_END)}\n?"
    )
    managed_match = managed_pattern.search(existing)
    servers = parsed.get("mcp_servers")
    semantic_fwr = isinstance(servers, dict) and "fantasy-war-room" in servers
    if marker_count == (1, 1):
        if managed_match is None or not semantic_fwr:
            raise ConfigurationError(
                "invalid_managed_codex_block",
                "The FWR managed block is invalid; FWR did not modify the file",
            )
        updated = managed_pattern.sub(managed_block, existing, count=1)
    else:
        if semantic_fwr:
            raise ConfigurationError(
                "unmanaged_codex_mcp_config",
                "An unmanaged Fantasy War Room MCP section already exists; remove it before "
                "running fwr codex configure",
            )
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        updated = existing + separator + managed_block
    try:
        reparsed = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:  # defensive: generated TOML must always parse
        raise ConfigurationError(
            "generated_codex_config_invalid",
            "FWR could not safely generate valid Codex TOML",
            {"error": str(exc)},
        ) from exc
    generated_servers = reparsed.get("mcp_servers")
    if not isinstance(generated_servers, dict) or "fantasy-war-room" not in generated_servers:
        raise ConfigurationError(
            "generated_codex_config_invalid",
            "Generated Codex configuration is missing the FWR MCP section",
        )
    return updated
