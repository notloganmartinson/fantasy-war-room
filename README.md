# Fantasy War Room

Fantasy War Room M1 is a local-first CLI that discovers Sleeper leagues, records immutable
draft-state snapshots in DuckDB, and reconstructs a draft as of a timezone-aware timestamp.
It contains no projections or recommendations.

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

## Commands

```console
uv run fwr discover
uv run fwr sync
uv run fwr state-at --draft-id 987654 --at 2026-07-31T19:00:00-05:00
uv run fwr watch --interval 2
```

`doctor`, `configure`, `discover`, `sync`, and `state-at` accept `--json` and return a stable
envelope with `status`, `command`, `data`, and `error`. In JSON mode stdout contains JSON only.

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
