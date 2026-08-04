# Fantasy War Room

Fantasy War Room is a local-first CLI that records immutable Sleeper draft and player-directory
snapshots in DuckDB, imports versioned ranking data, and reconstructs an available-player board
as of a timezone-aware timestamp. It does not recommend which player to draft.

## Install and configure

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```console
uv sync
uv run fwr configure --username YOUR_SLEEPER_USERNAME
uv run fwr doctor
```

Configuration, data, and cache use the operating system's user directories. On Linux these
follow `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_CACHE_HOME`. A second person simply runs
`fwr configure` with their own public Sleeper username; if they have several leagues, they can
choose interactively or use `--league-id`. No repository edits or credentials are required.

For an unattended setup:

```console
uv run fwr configure --username alice --season 2026 --league-id 123456 --non-interactive --json
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
uv run fwr discover
uv run fwr sync
uv run fwr state-at --draft-id 987654 --at 2026-07-31T19:00:00-05:00
uv run fwr watch --interval 2
uv run fwr players sync
uv run fwr players search "Josh Allen" --as-of 2026-08-01T12:00:00Z
uv run fwr rankings list
uv run fwr board --source my-rankings --as-of 2026-08-20T19:00:00Z
```

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

All finite commands accept `--json` and return a stable envelope with `status`, `command`, `data`,
and `error`. In JSON mode stdout contains JSON only. `watch` remains the interactive continuous
command.

Exit codes are: `0` success, `1` unexpected failure, `2` invalid input, `3` configuration
failure, `4` Sleeper/network failure, and `5` resource not found.

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
