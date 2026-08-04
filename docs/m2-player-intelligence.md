# M2: Player Intelligence and Available Draft Board

## Goal

Build a local, time-aware player intelligence layer that:

1. maps Sleeper player IDs to canonical internal players
2. preserves player metadata changes over time
3. imports versioned ranking, ADP, and optional projection CSV files
4. resolves imported rows to canonical players without silently guessing
5. renders the remaining draft board using only information available as of an
   explicit timestamp

M2 does not recommend which player to draft.

## Non-goals

Do not add:

* value-over-replacement calculations
* roster-construction scores
* player recommendations
* opponent modeling
* Monte Carlo simulation
* trained projections
* LLM calls
* MCP
* a web frontend
* integrations with fantasy platforms other than Sleeper

## Database migrations

All M2 tables must be introduced through the ordered migration system.

Existing M1 databases must upgrade without losing draft snapshots or normalized
picks.

## Sleeper player ingestion

Add a provider operation for the Sleeper NFL player directory.

Add:

```console
fwr players sync
fwr players sync --force
fwr players sync --json
```

Behavior:

* Use the XDG cache directory for the downloaded raw payload.
* Do not contact Sleeper when a valid cache entry is less than 24 hours old.
* `--force` bypasses the cache freshness check.
* Canonicalize the payload before hashing.
* Skip a consecutive identical player-directory observation.
* Permit the same payload to appear again after an intervening changed payload.
* Persist all timestamps in timezone-aware UTC.
* Report whether the network or cache supplied the payload.
* Report whether a new database snapshot was created.
* Never require the repository working directory.

## Canonical player identity

The internal canonical player ID must not equal or depend exclusively on a
Sleeper player ID.

Maintain a mapping between:

* canonical player ID
* provider
* provider player ID

Store provider identifiers when available, including:

* Sleeper
* GSIS
* ESPN
* Yahoo
* Sportradar
* FantasyData

Missing identifiers are valid and must not cause ingestion failure.

Track player observations including:

* first name
* last name
* normalized full name
* primary position
* fantasy positions
* NFL team
* active status
* injury status
* years of experience
* provider identifiers
* raw provider payload
* observed_at
* player-directory snapshot ID

Player metadata changes must create new observations rather than overwrite
historical source data.

## Player commands

Add:

```console
fwr players search QUERY
fwr players search QUERY --position QB
fwr players search QUERY --team ARI
fwr players search QUERY --as-of 2026-08-01T12:00:00Z
fwr players search QUERY --json
```

Search should be case-insensitive and support normalized names.

An as-of search must only use player observations available on or before the
requested timestamp.

## Ranking CSV format

Support a CSV containing:

Required:

* `player_name`

Optional identity fields:

* `sleeper_id`
* `gsis_id`
* `espn_id`
* `yahoo_id`
* `position`
* `team`

Optional ranking fields:

* `overall_rank`
* `positional_rank`
* `adp`
* `adp_sd`
* `projected_points`

At least one ranking field must be populated on each usable row.

The import command must require metadata:

```console
fwr rankings import FILE \
  --source SOURCE \
  --season SEASON \
  --scoring SCORING_FORMAT \
  --league-size LEAGUE_SIZE
```

Also support:

* `--source-version`
* `--observed-at`
* `--json`

If `--observed-at` is omitted, use the current UTC observation time.

## Ranking snapshots

Every ranking import must preserve:

* ranking snapshot ID
* source
* source version
* season
* scoring format
* league size
* observed_at
* imported_at
* canonical content hash
* original filename
* total row count
* matched row count
* unresolved row count
* ambiguous row count
* schema version

Skip only a consecutive identical import for the same source, season, scoring
format, and league size.

Permit ranking state A, then B, then A again.

## Identity resolution

Resolve each ranking row using this precedence:

1. explicit Sleeper ID
2. another explicit provider ID
3. exact normalized name plus position plus NFL team
4. exact normalized name plus position when exactly one candidate exists
5. unresolved or ambiguous

Do not automatically accept fuzzy matching.

Persist unresolved and ambiguous rows with:

* source row number
* source player name
* supplied position
* supplied team
* candidate canonical player IDs
* match status
* reason
* raw row payload

Add:

```console
fwr rankings list
fwr rankings list --json
fwr rankings unresolved
fwr rankings unresolved --snapshot-id SNAPSHOT_ID
fwr rankings unresolved --json
```

## Available-player board

Add:

```console
fwr board
fwr board --draft-id DRAFT_ID
fwr board --source SOURCE
fwr board --position WR
fwr board --limit 100
fwr board --as-of 2026-08-20T19:00:00Z
fwr board --json
```

The board must select:

1. the newest draft snapshot at or before the requested timestamp
2. the newest player-directory snapshot at or before the requested timestamp
3. the newest matching ranking snapshot at or before the requested timestamp

It must then exclude canonical players whose Sleeper IDs appear in the selected
draft snapshot.

The board must not perform network requests. Users must explicitly run player,
ranking, and draft synchronization commands beforehand.

Sort by:

1. overall rank when present
2. ADP when present
3. normalized player name as a deterministic tie-breaker

Each board result must include:

* canonical player ID
* Sleeper player ID
* player name
* position
* NFL team
* overall rank
* positional rank
* ADP
* ADP standard deviation
* projected points
* ranking source
* draft snapshot ID and observed_at
* player snapshot ID and observed_at
* ranking snapshot ID and observed_at
* output schema version

Human output should use Rich tables.

JSON output must use the existing stable envelope and contain no human
formatting on stdout.

## Proposed tables

### schema_migrations

* version
* name
* applied_at

### player_directory_snapshots

* snapshot_id
* provider
* sport
* observed_at
* fetched_at
* payload_hash
* player_count
* raw_cache_path
* schema_version

### canonical_players

* canonical_player_id
* created_at

### player_provider_ids

* canonical_player_id
* provider
* provider_player_id
* first_observed_at

### player_observations

* snapshot_id
* canonical_player_id
* provider_player_id
* first_name
* last_name
* normalized_full_name
* position
* fantasy_positions
* team
* active
* status
* injury_status
* provider_ids
* raw_payload

### ranking_snapshots

* ranking_snapshot_id
* source
* source_version
* season
* scoring_format
* league_size
* observed_at
* imported_at
* payload_hash
* original_filename
* row counts
* schema_version

### ranking_entries

* ranking_snapshot_id
* source_row_number
* canonical_player_id nullable
* source_player_name
* source_position
* source_team
* overall_rank
* positional_rank
* adp
* adp_sd
* projected_points
* match_status
* raw_payload

### ranking_match_issues

* ranking_snapshot_id
* source_row_number
* match_status
* reason
* candidate_player_ids
* raw_payload

Exact naming may change, but the resulting schema must preserve these concepts.

## Tests

Add tests for:

* upgrading an existing M1 database without data loss
* cache hit avoiding a network request
* `--force` making a network request
* identical consecutive player payload deduplication
* player state A → B → A
* player metadata as-of queries
* explicit Sleeper ID ranking matches
* unique exact name, position, and team matches
* ambiguous names not being silently resolved
* unresolved players being persisted
* malformed numeric CSV fields
* ranking import deduplication
* ranking state A → B → A
* board exclusion of already-drafted players
* board reconstruction at two different as-of timestamps
* board provenance fields
* stable JSON output
* commands working outside the repository directory
* zero public internet access from tests

## Documentation

Update the README with:

* player synchronization
* the cache policy
* the ranking CSV schema
* an example ranking import
* how identity resolution works
* how to inspect unresolved rows
* how to render the current board
* how to reproduce a historical board with `--as-of`
* a statement that M2 does not yet recommend picks

Include a small synthetic ranking CSV fixture that is safe to redistribute.

