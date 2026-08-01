# M1: Sleeper ingestion and as-of draft snapshots

## Goal

Build a working CLI that discovers a user's Sleeper leagues, downloads a
league's live draft state, stores immutable state changes in DuckDB, and
reconstructs the state as of an arbitrary timestamp.

No recommendation model is included in M1.

## Commands

### fwr doctor

Validate configuration, database access, and required directories.

### fwr discover

Arguments:

- --username
- --season, default current configured season

Resolve the username to a user ID, retrieve NFL leagues, and display:

- league name
- league ID
- status
- draft ID
- number of teams

### fwr sync

Arguments:

- --league-id, falling back to configuration

Retrieve:

- league
- most recent league draft
- complete draft metadata
- all completed picks

Persist a new snapshot only when the canonical payload hash differs from the
latest stored snapshot for the draft.

### fwr state-at

Arguments:

- --draft-id
- --at, an ISO-8601 timestamp with timezone

Return the latest snapshot whose observed_at is less than or equal to --at.

### fwr watch

Arguments:

- --league-id
- --interval, default 2 seconds

Fetch static league and draft metadata initially, then poll only the draft
picks endpoint. Persist and render the state only when picks change. Exit
cleanly on Ctrl-C.

## DuckDB schema

### draft_snapshots

- snapshot_id VARCHAR primary key
- league_id VARCHAR not null
- draft_id VARCHAR not null
- observed_at TIMESTAMPTZ not null
- source_updated_at TIMESTAMPTZ null
- payload_hash VARCHAR not null
- pick_count INTEGER not null
- league_payload JSON not null
- draft_payload JSON not null
- picks_payload JSON not null

Unique logical constraint:

- draft_id plus payload_hash

### draft_snapshot_picks

- snapshot_id VARCHAR not null
- draft_id VARCHAR not null
- pick_no INTEGER not null
- round INTEGER
- draft_slot INTEGER
- roster_id VARCHAR
- picked_by VARCHAR
- player_id VARCHAR
- player_name VARCHAR
- position VARCHAR
- team VARCHAR
- raw_payload JSON not null

Primary key:

- snapshot_id plus pick_no

## As-of rule

For a draft and decision timestamp, select:

1. snapshots for that draft
2. observed_at less than or equal to the decision time
3. newest observed_at
4. normalized picks belonging to that snapshot

## Sleeper adapter methods

- get_user(username_or_id)
- get_user_leagues(user_id, season)
- get_league(league_id)
- get_league_drafts(league_id)
- get_draft(draft_id)
- get_draft_picks(draft_id)

Use:

- an explicit timeout
- a descriptive user agent
- retry with exponential backoff for transient HTTP errors
- response.raise_for_status()

## Portability and agent interface

The application must not assume it is running from the repository root.

Use platformdirs to resolve operating-system-appropriate locations:

* user configuration directory
* user data directory
* user cache directory

On Linux, the default locations should follow the XDG Base Directory
conventions.

### Configuration

Add an `fwr configure` command.

Arguments:

* `--username`
* `--league-id`, optional
* `--season`, optional
* `--db-path`, optional
* `--non-interactive`, optional

Behavior:

1. Resolve the Sleeper username to an immutable user ID.
2. Retrieve the user's NFL leagues for the selected season.
3. If `--league-id` is supplied, validate that league.
4. If there is one matching league, select it.
5. If there are multiple leagues and the terminal is interactive, display a
   selection prompt.
6. If there are multiple leagues in non-interactive mode, return a useful
   error and the available league IDs.
7. Persist the selected configuration to the user's configuration directory.

Configuration precedence must be:

1. explicit CLI arguments
2. environment variables
3. user configuration file
4. application defaults

The `.env` file is a development convenience and must not be required for an
installed user.

### Machine-readable output

The following commands must support `--json`:

* `fwr doctor`
* `fwr configure`
* `fwr discover`
* `fwr sync`
* `fwr state-at`

JSON output must be written to stdout.

Human-readable diagnostic messages and errors must be written to stderr when
JSON mode is active.

JSON responses must have stable top-level fields:

* `status`
* `command`
* `data`
* `error`

Successful commands use `status: "success"` and `error: null`.

Failed commands use `status: "error"` and include:

* a stable error code
* a human-readable message
* optional structured details

### Exit codes

Use documented process exit codes:

* `0`: success
* `1`: unexpected application failure
* `2`: invalid input
* `3`: configuration failure
* `4`: provider or network failure
* `5`: requested resource not found

### Path independence

No application command may rely on the current working directory.

Database, configuration, and cache paths must be resolved explicitly.

Users may override every default path through configuration or a CLI option.

## Tests

- username resolution
- mocked league discovery
- mocked draft synchronization
- unchanged draft payload does not create a duplicate snapshot
- a changed pick list creates a new snapshot
- as-of query excludes future snapshots
- malformed timestamps produce a useful CLI error
- network errors produce a useful CLI error
- configuration is stored outside the repository
- CLI arguments override environment and file configuration
- environment variables override file configuration
- all supported commands produce valid JSON with --json
- JSON mode keeps diagnostics out of stdout
- commands work when launched outside the repository directory
- documented exit codes are returned for configuration and network failures
Unit tests may not contact Sleeper.
