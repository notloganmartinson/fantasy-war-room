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

## Product direction

Fantasy War Room is personal-use-first, but the repository must remain usable
by other Sleeper users without modifying application source code.

The target onboarding flow is:

1. clone the repository
2. `uv sync`
3. configure a Sleeper username
4. discover and select one of that user's leagues
5. synchronize authoritative league/draft state
6. validate required local intelligence
7. configure the local read-only FWR MCP
8. use FWR from Codex or another MCP client

Do not require users to edit Python source, hard-code league IDs, copy another
user's database, or inherit another user's strategy profile.

## Portability and multi-league requirements

- Sleeper account identity and league selection are user configuration, not
  source-code constants.
- Support multiple Sleeper league contexts for one user.
- League settings, scoring, roster positions, draft format, draft order, and
  current draft state come from authoritative Sleeper observations.
- Do not assume a fixed league size, draft slot, PPR format, FLEX count, or
  round count.
- Unsupported formats must fail explicitly with useful diagnostics rather than
  silently approximating another format.
- Personal strategy preferences must never be the default behavior for a clean
  installation.
- `logan-ppr-2flex-1.0` may remain available as an optional profile but must not
  be implicitly applied to other users or leagues.
- Ranking, projection, ADP, schedule, and strategy dependencies must be
  explicit and inspectable.
- A clean clone must report missing intelligence clearly rather than silently
  borrowing data from an incompatible league, scoring format, or source.
- Do not commit user databases, generated league configuration, private data,
  generated Codex configuration, or proprietary source exports.
- Setup commands should be idempotent and safe to rerun when league state or
  draft order changes.
- Configuration migrations must preserve existing users when the config schema
  evolves.
- Commands intended for agent automation must support stable `--json` output.
- Generated MCP configuration must preserve the MCP read-only/network-free
  boundary: synchronization remains a separate CLI process.

## Current provider boundary

The currently supported live provider scope is:

- Sleeper
- NFL
- redraft
- snake draft
- single quarterback
- non-keeper

Within that supported boundary, league size, scoring, roster construction,
draft slot, and round count must be derived from the selected league rather
than hard-coded.

Expansion beyond this boundary should happen only when a real workflow
requires it and should not weaken existing deterministic behavior.
