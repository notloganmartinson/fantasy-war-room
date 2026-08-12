from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from fantasy_war_room.config import load_settings
from fantasy_war_room.decision.models import OffensivePosition, RecommendationModelVersion
from fantasy_war_room.errors import FwrError, InputError
from fantasy_war_room.mcp.models import McpEnvelope, McpError
from fantasy_war_room.mcp.repository import McpReadRepository
from fantasy_war_room.mcp.service import DraftCopilotService

LOGGER = logging.getLogger(__name__)
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
SERVER_INSTRUCTIONS = """Fantasy War Room is authoritative for synchronized draft facts. For
“Who should I take?”, call recommend_pick first; it is the coherent source for turn, roster,
candidates, and provenance. Never invent availability or next-pick probabilities.

Use get_draft_state for direct pick, round, clock, recent-pick, or status questions. Use
get_my_roster when roster construction matters, compare_players for direct comparisons, and
get_position_outlook before claiming a position is deep, thin, or safe to wait on. When combining
tools, compare draft_snapshot_id; if it changed, refresh recommend_pick before advising. Never
overwrite FWR facts with memory. Distinguish deterministic findings from strategic inference.
When recommending, explain why now, closest alternatives, what to prioritize afterward, and tier
or scarcity concerns. Do not claim a player will survive or invent availability probabilities.
MCP does not synchronize; if state is stale, tell the user to check fwr watch.
"""


def create_server(service: DraftCopilotService) -> MCPServer:
    server = MCPServer(
        "Fantasy War Room Draft Copilot",
        version="1.0.0",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def get_draft_state(as_of: str | None = None) -> CallToolResult:
        """Read the selected draft's coherent turn state and recent picks."""
        return _call("fwr.mcp.draft-state/1.0", lambda: service.get_draft_state(as_of=as_of))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def get_my_roster(as_of: str | None = None) -> CallToolResult:
        """Read the user's projection-aware offensive lineup, bench, and vacancies."""
        return _call("fwr.mcp.roster/1.0", lambda: service.get_my_roster(as_of=as_of))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def get_available_players(
        position: str | None = None,
        limit: int = 20,
        as_of: str | None = None,
    ) -> CallToolResult:
        """Read currently available offensive players; never infer availability from memory."""
        return _call(
            "fwr.mcp.available-players/1.0",
            lambda: _available_players(service, position, limit, as_of),
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def recommend_pick(
        model: str = "trusted-board-1.1",
        source: str = "parlay-play-hybrid",
        limit: int = 10,
        as_of: str | None = None,
    ) -> CallToolResult:
        """Run the existing deterministic engine as the coherent current-pick authority."""
        return _call(
            "fwr.mcp.recommendation/1.0",
            lambda: _recommend_pick(service, model, source, limit, as_of),
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def compare_players(players: list[str], as_of: str | None = None) -> CallToolResult:
        """Compare exactly two canonical IDs or deterministic player-name matches."""
        return _call(
            "fwr.mcp.player-comparison/1.0",
            lambda: service.compare_players(players=players, as_of=as_of),
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    def get_position_outlook(
        position: str | None = None, as_of: str | None = None
    ) -> CallToolResult:
        """Read deterministic depth, scarcity, vacancy, and trusted-tier evidence."""
        return _call(
            "fwr.mcp.position-outlook/1.0",
            lambda: service.get_position_outlook(position=_position(position), as_of=as_of),
        )

    return server


def _available_players(
    service: DraftCopilotService,
    position: str | None,
    limit: int,
    as_of: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _limit(limit)
    return service.get_available_players(position=_position(position), limit=limit, as_of=as_of)


def _recommend_pick(
    service: DraftCopilotService,
    model: str,
    source: str,
    limit: int,
    as_of: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _limit(limit)
    return service.recommend_pick(model=_model(model), source=source, limit=limit, as_of=as_of)


def _call(
    schema_version: str,
    operation: Callable[[], tuple[dict[str, Any], dict[str, Any]]],
) -> CallToolResult:
    try:
        data, provenance = operation()
        envelope = McpEnvelope(
            schema_version=schema_version,
            status="ok",
            data=data,
            error=None,
            provenance=provenance,
        )
        return _result(envelope, is_error=False)
    except FwrError as error:
        envelope = McpEnvelope(
            schema_version=schema_version,
            status="error",
            data=None,
            error=McpError(code=error.code, message=error.message, details=error.details),
            provenance=None,
        )
        return _result(envelope, is_error=True)
    except Exception:
        LOGGER.exception("Unexpected MCP tool failure")
        envelope = McpEnvelope(
            schema_version=schema_version,
            status="error",
            data=None,
            error=McpError(code="internal_error", message="Unexpected local MCP failure"),
            provenance=None,
        )
        return _result(envelope, is_error=True)


def _result(envelope: McpEnvelope, *, is_error: bool) -> CallToolResult:
    structured = envelope.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(text=json.dumps(structured, sort_keys=True, separators=(",", ":")))],
        structured_content=structured,
        is_error=is_error,
    )


def _position(value: str | None) -> OffensivePosition | None:
    if value is None:
        return None
    normalized = value.upper()
    if normalized not in {"QB", "RB", "WR", "TE"}:
        raise InputError(
            "invalid_position",
            "Position must be one of QB, RB, WR, or TE",
            {"position": value},
        )
    return cast(OffensivePosition, normalized)


def _model(value: str) -> RecommendationModelVersion:
    supported = {"baseline-1.0", "trusted-board-1.0", "trusted-board-1.1"}
    if value not in supported:
        raise InputError(
            "unsupported_recommendation_model",
            "Unsupported recommendation model",
            {"model": value, "supported_models": sorted(supported)},
        )
    return cast(RecommendationModelVersion, value)


def _limit(value: int) -> None:
    if not 1 <= value <= 100:
        raise InputError("invalid_limit", "limit must be between 1 and 100", {"limit": value})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fwr-mcp")
    parser.add_argument("--draft-id", required=True)
    parser.add_argument("--draft-slot", type=int)
    parser.add_argument("--source", default="parlay-play-hybrid")
    parser.add_argument(
        "--model",
        default="trusted-board-1.1",
        choices=("baseline-1.0", "trusted-board-1.0", "trusted-board-1.1"),
    )
    parser.add_argument("--database", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = load_settings(db_path=args.database)
    service = DraftCopilotService(
        McpReadRepository(settings.db_path),
        draft_id=args.draft_id,
        sleeper_user_id=settings.sleeper_user_id,
        draft_slot=args.draft_slot,
        default_source=args.source,
        default_model=_model(args.model),
    )
    logging.basicConfig(level=logging.WARNING)
    create_server(service).run("stdio")
