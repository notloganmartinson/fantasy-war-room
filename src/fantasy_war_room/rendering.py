from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

stdout = Console()
stderr = Console(stderr=True)


def jsonable(value: Any) -> Any:
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


def diagnostic(message: str) -> None:
    stderr.print(message)
