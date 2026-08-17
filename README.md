# Fantasy War Room

Fantasy War Room is a local-first CLI that records immutable Sleeper draft and player-directory
snapshots in DuckDB, imports versioned ranking data, and reconstructs an available-player board
as of a timezone-aware timestamp. Its deterministic recommendation engine can also serve a
read-only local MCP draft copilot for Codex CLI.

## Clean-clone onboarding

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```console
git clone https://github.com/notloganmartinson/fantasy-war-room.git
cd fantasy-war-room
uv sync
uv run fwr setup --username YOUR_SLEEPER_USERNAME
uv run fwr data bootstrap
uv run fwr draft-ready
uv run fwr codex configure
```

`setup` resolves the Sleeper account, selects a league, synchronizes its authoritative draft
state, and refreshes the canonical player directory. With multiple leagues it prompts for one;
automation must pass `--league-id`. An unpublished draft order is reported as pending and setup
is safe to rerun.

Sleeper connectivity and intelligence readiness are separate. `data bootstrap` derives the
active league's season, team count, scoring format, and draft type from synchronized Sleeper
observations. It automatically acquires exact-format ADP from Fantasy Football Calculator and
derives NFL bye weeks from the nflverse schedule dataset. A clean clone still has no rankings or
projections: `draft-ready` reports those required inputs and does not claim `READY` until both
are imported compatibly.

Fantasy Football Calculator's documented ADP API is free for personal and commercial use and
requests attribution; its data is based on human mock drafts and updates daily. nflverse
schedule data is distributed under CC BY 4.0. FWR caches sanitized raw responses for 24 hours in
the XDG cache directory, then stores normalized immutable observations in DuckDB with source
URI/version, fetch/observation/import times, source and normalized payload hashes, identity
resolver version, and deterministic transformation version. Use `--force` to bypass the cache.

FantasyPros rankings and projections are not automatic in M4B. Its official API requires an
approved private key and imposes usage and redistribution restrictions, so it is not a portable
default. FWR reports only whether the environment variable is present; it never sends, persists,
logs, or prints the credential value in this milestone. Existing local
ranking and CBS projection imports remain available; Logan's Parlay Play/CBS exports and database
are neither bundled nor required.

For an unattended setup:

```console
uv run fwr setup --username alice --season 2026 --league-id 123456 \
  --non-interactive --json
```

Values can also be supplied with `FWR_SLEEPER_USERNAME`, `FWR_SLEEPER_LEAGUE_ID`, `FWR_SEASON`,
and `FWR_DB_PATH`. Precedence is CLI, environment (including an optional development `.env`),
user config, then defaults.

For local development, copy `.env.example` to `.env` and uncomment only the overrides you
need. The example is intentionally inert: uncommented environment values take precedence over
the configured user, and the default database remains in the XDG user data directory. If you
override `FWR_DB_PATH`, use an absolute path so commands remain independent of the current
working directory.

## Commands

```console
uv run fwr setup --username alice
uv run fwr data bootstrap
uv run fwr data bootstrap --force --json
uv run fwr leagues list
uv run fwr leagues use LEAGUE_ID
uv run fwr context
uv run fwr draft-ready --json
uv run fwr codex configure --json
uv run fwr discover
uv run fwr drafts list
uv run fwr sync
uv run fwr sync --draft-id STANDALONE_DRAFT_ID
uv run fwr watch --draft-id STANDALONE_DRAFT_ID
uv run fwr state-at --draft-id 987654 --at 2026-07-31T19:00:00-05:00
uv run fwr watch --interval 2
uv run fwr players sync
uv run fwr players search "Josh Allen" --as-of 2026-08-01T12:00:00Z
uv run fwr rankings list
uv run fwr board --source my-rankings --as-of 2026-08-20T19:00:00Z
uv run fwr recommend --draft-id 987654 --model trusted-board-1.1
uv run fwr recommend --draft-id 987654 --draft-slot 7 \
  --strategy logan-ppr-2flex-1.0
uv run fwr strategies show logan-ppr-2flex-1.0
```

Each saved league context contains only local selections: provider, season, league ID, and
optional ranking source, recommendation model, and strategy. Scoring, roster construction,
draft order, and draft state come from Sleeper observations. Switching leagues does not leak
preferences between them. Personalized strategies are opt-in; `logan-ppr-2flex-1.0` remains
available but is never selected for a new league.

The configured Sleeper user ID is the account boundary. Running setup or configure for a
different resolved user clears the previous user's active league and saved league contexts while
preserving machine settings such as the database path and polling interval.

`drafts list` discovers both league-associated and standalone Sleeper drafts for the configured
user. `sync --draft-id` and `watch --draft-id` operate on that exact draft instead of selecting a
league's newest draft. League drafts use their associated league scoring context. A standalone
mock does not inherit configured scoring implicitly; when needed, associate it explicitly with
`--scoring-context-league-id LEAGUE_ID`. The source draft and scoring context remain distinct in
snapshot provenance.

Player synchronization caches the canonical raw Sleeper NFL player directory under the XDG
cache directory for 24 hours. A fresh cache avoids all network access; `players sync --force`
bypasses freshness. Player searches and boards are always local and never synchronize implicitly.

Ranking CSV imports require `player_name` and at least one populated ranking value per usable row.
Supported columns are:

- Identity: `sleeper_id`, `gsis_id`, `espn_id`, `yahoo_id`, `position`, and `team`.
- Ranking data: `overall_rank`, `positional_rank`, `adp`, `adp_sd`, and `projected_points`.

Import metadata is explicit:

```console
uv run fwr rankings import rankings.csv \
  --source my-rankings --season 2026 --scoring ppr --league-size 10 \
  --source-version 2026-08-01 --observed-at 2026-08-01T12:00:00Z
```

Rows resolve by explicit provider ID or exact normalized identity. Ambiguous and unresolved rows
are preserved for inspection with `fwr rankings unresolved`; fuzzy matches are never accepted.

All current recommendation models require an exact-scoring projection snapshot and a compatible
ranking snapshot. `baseline-1.0` uses the portable model selection fallback but still requires
both inputs because the current deterministic input builder calculates projection value and an
expert-rank component. `trusted-board-1.0` and `trusted-board-1.1` likewise require both, and a
selected strategy may additionally require an exact ranking source/model combination. ADP and
schedule are optional market context today; `data bootstrap` acquiring them does not fabricate
rankings or projections merely to turn readiness green.

All finite commands accept `--json` and return a stable envelope with `status`, `command`, `data`,
and `error`. In JSON mode stdout contains JSON only. `watch` remains the interactive continuous
command.

Exit codes are: `0` success, `1` unexpected failure, `2` invalid input, `3` configuration
failure, `4` Sleeper/network failure, and `5` resource not found.

## Local MCP draft copilot

Keep synchronization in a separate terminal:

```console
uv run fwr watch --draft-id DRAFT_ID --scoring-context-league-id LEAGUE_ID
```

The MCP server requires an explicit draft ID, reads DuckDB with short-lived read-only
transactions, and never synchronizes or contacts a provider. `fwr codex configure` writes an
explicitly marked FWR-managed block in the ignored project-scoped `.codex/config.toml`, using the
active context's exact draft, slot, source, model, optional strategy, database, and working
directory. It preserves unrelated Codex configuration. An equivalent unmanaged FWR table must
be removed manually before the command will take ownership. Restart Codex in the trusted
repository afterward.

Pass `--strategy logan-ppr-2flex-1.0` to enable the M3.5A deterministic strategy layer.
The initial profile is compatible with draft slot 7 and preserves the complete raw
`trusted-board-1.1` result alongside its strategy-adjusted ordering. Strategy-aware MCP
startup also exposes `get_draft_strategy` and embeds the active profile in fresh-session
instructions.

## Development

```console
uv run ruff format .
uv run ruff check .
uv run mypy src
uv run pytest
```

doctor: makes sure fwr is installed and configured correctly on the current machine
discover: connect to Sleeper using configured username and finds your leagues
- returns league name, league id, draft id, number of teams, league status and season
sync: downloads current state of configured league and draft then considers saving it to DuckDB
- retrieves league settings scoring settings roster positions draft metadata completed picks
fetches the current draft state and save it only if something has changed
