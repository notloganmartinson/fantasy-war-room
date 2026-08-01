# Fantasy War Room

Fantasy War Room is a local-first, time-aware fantasy football decision system.

## Engineering invariants

- Python 3.12 managed through uv.
- Use a src package layout.
- Use Polars rather than pandas.
- Use DuckDB for the local analytical warehouse.
- External providers must live behind adapter interfaces.
- Domain and decision code must not perform network I/O.
- All source observations are append-only.
- Never overwrite historical projections, rankings, or draft states.
- Every persisted observation must include an observed_at timestamp in UTC.
- Historical queries must support an explicit as-of timestamp.
- Unit tests must not access the public internet.
- Use mocked HTTP responses for Sleeper adapter tests.
- Decision functions must be deterministic when given an explicit random seed.
- Do not claim championship-probability improvements without evaluation evidence.
- Core application logic must not depend on Codex, Claude, Goose, or any LLM.
- CLI commands are the primary public automation interface.
- Agent-facing commands must provide stable JSON output.
- Human-readable and JSON rendering must be separate from domain logic.
- Application paths must not depend on the current working directory.
- Use XDG-compatible user directories through platformdirs.
- Configuration precedence is CLI, environment, user config, then defaults.
- Public data models must use explicit versioned schemas.

## Commands

- Install/sync: `uv sync`
- Format: `uv run ruff format .`
- Lint: `uv run ruff check .`
- Type-check: `uv run mypy src`
- Test: `uv run pytest`
- CLI: `uv run fwr --help`

## Initial scope

Support one format first:

- Sleeper
- NFL
- Redraft
- Snake draft
- Single quarterback
- Non-keeper
