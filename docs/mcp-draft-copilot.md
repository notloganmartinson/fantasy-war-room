# MCP Draft Copilot

## Status

Implemented as the first local read-only MCP draft copilot.

The implemented baseline exposes six tools: `get_draft_state`,
`get_my_roster`, `get_available_players`, `recommend_pick`,
`compare_players`, and `get_position_outlook`. Implemented M3.5A adds optional
strategy-aware startup, dynamic profile instructions, strategy-adjusted
`recommend_pick` output, and `get_draft_strategy`. M3.5B Part 1 adds read-only
`get_market_context` and `get_opponent_demand` with coherent ADP and schedule
provenance.

## Goal

Expose Fantasy War Room's synchronized, deterministic draft intelligence to a
local Codex CLI session over MCP:

```text
Sleeper -> fwr watch -> DuckDB snapshots -> FWR decision engine -> MCP -> Codex
```

Fantasy War Room remains authoritative for draft state, identity, availability,
roster allocation, rankings, projections, VORP, scarcity, recommendation
scores, and provenance. Codex may synthesize those facts into conversational
strategy, but it must label that synthesis as inference and must not replace an
FWR fact with model memory.

The first release is local, read-only, stdio-only, and draft-night focused. It
does not synchronize data, contact Sleeper or another provider, write DuckDB,
run Monte Carlo simulations, or estimate next-pick survival probabilities.

## Boundary and safety model

The MCP process is bound to one explicitly supplied draft ID at startup. A tool
cannot switch to another draft. `fwr watch` remains the only process refreshing
that context. MCP v1 does not infer a draft from the configured league.

Each tool call:

1. resolves the explicit `as_of` argument or captures one live UTC cutoff;
2. opens a new DuckDB connection with `read_only=True`;
3. starts one read transaction so a concurrent watcher cannot produce a mixed
   snapshot view;
4. selects all inputs under the existing M3 as-of and import-visibility rules;
5. invokes existing pure decision functions where derived values are needed;
6. returns versioned structured data and exact provenance; and
7. closes the read transaction and connection without a write.

The server never retains a DuckDB connection, cursor, transaction, or database
handle between calls. Every attempt follows one open/read-transaction/close
cycle, including failed attempts.

Market-aware calls select draft, ranking, projection, compatible ADP, and team
schedule snapshots inside that transaction. Missing compatible ADP returns
explicit `no_compatible_market_context` classifications and manual-window
fallback; another source or format is never borrowed silently.

The server does not call `IntelligenceRepository.initialize()`, run migrations,
or use a repository method that may write. `McpReadRepository` opens DuckDB in
read-only mode and reuses the repository's existing recommendation-selection
helpers so CLI and MCP retain the same input semantics.

The server has no provider adapters in its dependency graph. Network clients
are neither constructed nor accepted by the MCP service. Startup and tool
execution write protocol messages only to stdout; diagnostics go to stderr.

### Cross-process lock contention

DuckDB file access is not assumed to allow the watcher writer and MCP reader to
overlap without contention. Both sides use a shared, narrowly classified lock
retry policy:

- retry only errors positively identified as database file-lock contention;
- close any partially opened connection before retrying;
- use bounded exponential backoff with short deterministic delays
  `50 ms`, `100 ms`, `200 ms`, `400 ms`, `800 ms`; no jitter);
- MCP returns stable `database_busy` after all attempts are exhausted; and
- watcher polling logs a transient lock conflict to stderr and retries the
  same fetched canonical state until it is persisted, rather than crashing or
  advancing past that state.

The exact attempt count and delays become versioned operational constants and
are exercised in real subprocess tests. Retries must not cover SQL, schema,
corruption, validation, or other database errors.

Watcher retry is part of this milestone because a read-only copilot is not safe
if its reads can terminate authoritative ingestion. The watcher retains the
pending canonical state across lock retries; a later provider poll must not
cause an unpersisted state to be skipped.

If subprocess stress testing shows native single-file contention cannot meet
these guarantees reliably on supported systems, the fallback is an atomic read
replica. After a successful watcher commit, a consistent DuckDB copy would be
created at a temporary sibling path, closed and synced, then atomically replace
a read-only replica consumed by MCP. Replica publication would expose source
snapshot provenance and staleness. This fallback is documented now but is not
implemented unless practical testing proves it necessary.

## MCP library

The implementation uses the official Python MCP SDK's `MCPServer` API for
stdio transport, typed tool schemas, structured results, tool annotations, and
the initialization `instructions` field. The implemented release is pinned as
`mcp==2.0.0` in project metadata and the lockfile.

Every tool is annotated read-only and non-destructive. The implementation does
not expose generic SQL, file, resource-write, sync, or provider tools.

Codex supports local stdio servers, reads server-wide MCP instructions, and can
register a stdio command with `codex mcp add`; its MCP configuration also
supports an explicit command, argument list, environment, working directory,
timeouts, and tool policies. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

## Implemented package layout

```text
src/fantasy_war_room/
  mcp/
    __init__.py
    server.py          # MCPServer declaration, argument parsing, instructions, stdio entry point
    service.py         # tool-oriented, read-only application facade
    repository.py      # read-only transaction and context queries
    models.py          # stable MCP envelope and error models
```

Existing modules retain their responsibilities:

- `decision/recommend.py` remains the only recommendation calculator.
- `decision/models.py` remains the source of recommendation result models.
- shared repository query helpers select immutable inputs for both CLI and MCP.
- provider adapters and synchronization services remain outside the MCP path.

The project exposes this console entry point:

```toml
[project.scripts]
fwr-mcp = "fantasy_war_room.mcp.server:main"
```

`fwr-mcp` runs stdio by default. It has no HTTP listener and no daemon mode in
the first release.

## Configuration

The server is configured once at process startup. MCP v1 requires
`--draft-id`:

```console
fwr-mcp \
  --draft-id DRAFT_ID \
  [--draft-slot SLOT] \
  [--source parlay-play-hybrid] \
  [--model trusted-board-1.1] \
  [--strategy logan-ppr-2flex-1.0] \
  [--database PATH]
```

The implemented startup parser requires `--draft-id`; accepts optional
`--draft-slot`, `--source`, `--model`, and `--database`; and defaults source to
`parlay-play-hybrid` and model to `trusted-board-1.1`. The database path falls
back through the application's existing `FWR_DB_PATH`, user configuration, and
XDG default behavior. MCP-specific environment variables are not implemented
in v1. Explicit `recommend_pick` arguments may override source and model for
that call.

`draft_id` is always required. Draft-slot resolution uses the existing M3
precedence; an explicit startup slot is sufficient for a standalone mock
without user ownership.

`--strategy` also resolves through `FWR_MCP_STRATEGY`, then the application's
active `strategy` setting. The initial profile requires draft slot 7,
`trusted-board-1.1`, and `parlay-play-hybrid`; conflicting contexts return a
structured error. Without a strategy, the original six-tool contract is
unchanged.

The database path continues to use XDG configuration and `platformdirs`, not
the process working directory. An absolute project path in the Codex
registration command makes the executable itself independent of where Codex
is launched.

Startup validates arguments but does not require or mutate the database.
Missing databases, pending migrations, or absent snapshots become structured
MCP errors with an instruction to run the relevant FWR CLI command outside
MCP.

## Decision-time contract

Every tool accepts an optional common `as_of` argument containing a
timezone-aware ISO 8601 timestamp. It is part of the input schema even when
omitted from the shorter examples below.

- With explicit `as_of`, the service uses exactly that UTC-normalized cutoff.
- Without it, the service captures current UTC once after acquiring a database
  connection and uses it for the complete transaction.
- Same explicit `as_of` + same snapshots + same arguments produces a
  byte-identical canonical structured result.
- For repeated live calls without `as_of`, deterministic facts, scores,
  ordering, and selected snapshot IDs remain stable while state is unchanged,
  but the reported query cutoff may differ. Byte-identical output is therefore
  not promised for live calls.

## Common response contract

All public MCP models are frozen Pydantic models with explicit schema versions.
Each tool returns this envelope:

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "data": {},
  "error": null,
  "provenance": {}
}
```

On a domain failure, `status` is `error`, `data` is null, and `error` contains a
stable code, message, and safe details. The MCP tool result itself must also set
`isError=true`; a domain failure must never look like a successful MCP tool call
merely because its content contains an error envelope. The server uses the
SDK's structured `CallToolResult` or equivalent API rather than flattening the
error into normal text. Input-schema failures follow MCP validation semantics
and remain client-visible errors.

Common provenance includes, where applicable:

- decision/as-of time;
- draft, player-directory, ranking, and projection snapshot IDs and times;
- ranking source, version, resolver version, and import time;
- projection source/version, calculator version, import time, player snapshot,
  scoring-context league, and scoring-settings hash;
- recommendation, roster allocator, replacement, scarcity, trusted-rank, and
  trusted-tier model versions.

There is no response-generation timestamp. Explicit replay is byte-stable under
the decision-time contract above. In live use, calls select the newest committed
state at their own cutoff; snapshot IDs make an intervening watcher update
obvious.

## Shared decision context

`DraftCopilotService` obtains one `DraftDecisionContext` per call. It contains:

- the complete `RecommendationInputs` selected by the existing M3 contract;
- the full, untrimmed `RecommendationResult` for the requested policy when a
  tool needs recommendation-derived values;
- as-of canonical identity/name indexes;
- all completed picks and their availability classification; and
- safe exclusion and unresolved-identity counts.

The service does not cache across tool calls. This prevents stale data while
`fwr watch` is active. Output limits are applied only after the full result is
calculated, preserving replacement levels, normalizations, scores, and
baselines.

## Tools

### `get_draft_state`

Arguments: none.

Returns schema `fwr.mcp.draft-state/1.0`:

- draft ID, status, type, team count, and season;
- next overall pick and current round;
- snake direction;
- resolved user slot and resolution method;
- whether the user is on the clock;
- next and following scheduled user picks;
- opponent picks before the next user pick and between user selections;
- the ten most recent completed picks, ordered by pick number, with canonical
  player ID/name when resolved; and
- draft snapshot provenance and unresolved-pick count.

Turn arithmetic uses the existing Phase B implementation. Unsupported draft
formats return `unsupported_draft_format` rather than an approximation.

### `get_my_roster`

Arguments: none.

Returns schema `fwr.mcp.roster/1.0`:

- projection-aware offensive starting assignments by QB/RB/WR/TE/FLEX slot;
- FLEX assignments explicitly identified;
- bench players;
- fixed and FLEX vacancies;
- drafted position counts;
- projected starting-lineup total;
- each starter's baseline kind (`exact` or `known_component`) and scoring
  completeness; and
- unresolved roster items with Sleeper player ID and pick number, without
  silently dropping them.

The existing `max-projection-offensive-flex-1.0` allocator is authoritative.
K/DST can appear in position counts or an unmodeled-roster section but do not
enter the offensive lineup calculation.

### `get_available_players`

Arguments:

```json
{
  "position": "QB | RB | WR | TE | null",
  "limit": 20
}
```

`limit` defaults to 20 and is bounded to 1..100. Position is optional; the
first recommendation system remains offensive-only under every policy.

Returns schema `fwr.mcp.available-players/1.0`, ordered by the configured MCP
model's deterministic recommendation order. Every row includes:

- canonical/Sleeper IDs, readable name, position, and team;
- availability state, always `available` in this result;
- trusted overall and positional rank and analyst tier when present;
- league projection baseline, value kind, scoring completeness, and
  unprojected scoring keys;
- VORP and structural replacement details;
- raw and normalized scarcity with comparison player IDs; and
- recommendation rank/score and model version so the ordering is explicit.

Drafted players are removed through every retained Sleeper provider mapping for
their canonical identity. A postcondition checks that no completed-pick
canonical ID or mapped Sleeper ID occurs in the response. Violating it is a
`data_integrity_error`, never a row that is mislabeled available.

Players without a usable league projection baseline are not silently treated
as zero. Exclusion counts and reasons are returned alongside the rows.

### `recommend_pick`

Arguments:

```json
{
  "model": "trusted-board-1.1",
  "source": "parlay-play-hybrid",
  "limit": 10
}
```

Defaults are shown above; supported models remain `baseline-1.0`,
`trusted-board-1.0`, and `trusted-board-1.1`. `limit` defaults to 10 and is
bounded to 1..100.

Returns schema `fwr.mcp.recommendation/1.0` containing the existing complete,
versioned recommendation result after presentation limiting. Candidate data
includes projection and completeness, VORP, scarcity, roster effect, trusted
rank/tier values and components when the model supplies them, every component
weight and contribution, limitations, baselines, and full provenance.

The implementation calls the existing recommendation input builder and pure
`recommend()` function. MCP contains no scoring, VORP, scarcity, roster, rank,
or tier formula. The next-pick component remains zero and availability remains
`unsupported_uncalibrated`.

### `compare_players`

Arguments:

```json
{
  "players": ["Ja'Marr Chase", "canonical-player-id"]
}
```

Exactly two selectors are accepted in version 1. Names use the existing strict
identity normalization and verified aliases only. No fuzzy matching is added.
Ambiguous names return candidates and `player_ambiguous`; missing names return
`player_not_found`.

Returns schema `fwr.mcp.player-comparison/1.0` with side-by-side:

- resolved identity and availability;
- draft pick metadata when drafted;
- trusted overall/positional rank and analyst tier;
- projection baseline and completeness;
- VORP and structural replacement;
- current scarcity, roster effect, and recommendation score when the player is
  an eligible available candidate; and
- explicit `not_applicable` reasons for unavailable or unscored fields.

A drafted player is never hypothetically reinserted into the current candidate
universe merely to manufacture a score. Its drafted state is prominent and its
current recommendation score, scarcity, and roster effect are not applicable.

### `get_position_outlook`

Arguments:

```json
{
  "position": "QB | RB | WR | TE | null"
}
```

When position is null, return all four positions in QB/RB/WR/TE order. Returns
schema `fwr.mcp.position-outlook/1.0` with:

- top available players per position;
- structural replacement and current one-round scarcity details;
- nearby trusted tiers among the next `team_count` players;
- the user's fixed/FLEX vacancies and position counts;
- available `starter_caliber_count`, defined as players with VORP >= 0;
- available `projected_depth_count`, defined as all players with a usable
  league projection baseline; and
- deterministic flags and their evidence.

The separately versioned `position-outlook-1.0` classifier uses these rules:

- `exhausted`: zero available starter-caliber players;
- `thinning`: starter-caliber count is positive and no greater than team count;
- `deep`: starter-caliber count is at least twice team count;
- `sharp_tier_drop`: the top available analyst-tier player has no player from
  the same tier among the following `team_count` available players; and
- `projection_cliff`: the top available player's existing scarcity percentile
  is at least 0.75.

The response includes the counts, comparison set, tier sequence, percentile,
and thresholds that produced each flag. These are deterministic FWR
classifications, not claims that a player will survive. Positions between
`team_count + 1` and `2 * team_count - 1` starter-caliber players receive no
depth label rather than an invented conclusion.

`sharp_tier_drop` is `unavailable` when the top available player has no trusted
analyst tier. Null tiers on Rotoworld fallback players are missing data: they
are never treated as a tier match, a boundary, or evidence of a tier drop.

### `get_draft_strategy`

This tool is registered only when MCP starts with an active strategy. It
returns schema `fwr.mcp.draft-strategy/1.0` with the normalized profile,
profile hash, temporal status, derived target states, remaining user picks,
and K/DEF completion status. It is read-only and uses the same coherent
recommendation context as `recommend_pick`.

With a strategy active, `recommend_pick` preserves the complete unchanged raw
`trusted-board-1.1` result and separately returns the deterministic adjusted
ordering. No second weighted strategy score is created. If the K/DEF guard
fires, it returns `actionable=false`, a null `actionable_choice`, empty
actionable candidates, and `roster_completion_required`; Codex must not treat
the embedded raw leader as a recommendation. Requested limits are applied only
to the adjusted presentation rows after full strategy evaluation.

The adjusted response also exposes effective reserved-position state and a
limit-invariant cross-position `value_summary`. In the default profile, Kyler
Murray reserves QB while active without receiving an early promotion; other
QBs do not become actionable ahead of eligible non-QBs. TE2 is classified by
its actual starting/FLEX effect and configured raw-value ceilings, while TE3
remains prohibited.

## Server instructions for Codex

The MCP initialization `instructions` field begins with the most important
workflow so it remains useful even when a client truncates instructions:

> Fantasy War Room is authoritative for synchronized draft facts. For “Who
> should I take?”, call recommend_pick first; it is the coherent source for
> turn, roster, candidates, and provenance. Never invent availability or
> next-pick probabilities.

The full instructions are:

1. For a current-pick question such as “Who should I take?”, call
   `recommend_pick` first. It is the authoritative single coherent call and
   already contains turn context, roster state, recommendations, and
   provenance. Do not require a preceding `get_draft_state` call.
2. Use `recommend_pick` with `trusted-board-1.1` and
   `parlay-play-hybrid` unless the user requests another supported model or
   source.
3. Use `get_draft_state` for direct questions about pick, round, clock, recent
   picks, or draft status.
4. Use `get_my_roster` whenever roster construction, vacancies, FLEX, or the
   user's next positional priority matters.
5. Use `compare_players` for direct player comparisons.
6. Use `get_position_outlook` before saying a position is deep, thin, or safe
   to wait on.
7. When combining multiple tool responses, compare `draft_snapshot_id`. If it
   changed, refresh `recommend_pick` before giving current-pick advice.
8. Never invent that a player is available. If availability is absent or the
   snapshot is stale, call a tool. If a player is drafted, say so explicitly.
9. Never overwrite an FWR fact with memory or general football knowledge.
10. Clearly distinguish deterministic FWR findings from strategic inference.
11. When recommending a player, explain: why this player now; the closest
   alternatives; which position to prioritize afterward; and any tier or
   scarcity concern.
12. Do not claim a player will definitely survive to the next pick. FWR has no
    calibrated availability probability, so do not invent one.
13. Do not imply that MCP synchronized the draft. When state appears stale,
    tell the user to check the separate `fwr watch` process.

Tool descriptions repeat the critical availability and no-probability
constraints so safety does not depend on instructions alone.

## Error handling

Stable errors include:

- `database_not_found`, `database_schema_incompatible`, and `database_busy`;
- `draft_not_found`, `no_draft_snapshot`, and `draft_slot_unresolved`;
- `unsupported_draft_format` and `draft_not_actionable`;
- `mock_scoring_context_required` and `incompatible_scoring_context`;
- `missing_player_snapshot`, `missing_ranking_snapshot`, and
  `missing_projection_snapshot`;
- `insufficient_projection_depth`;
- `unsupported_model`, `unsupported_source`, and `invalid_position`;
- `player_not_found` and `player_ambiguous`; and
- `data_integrity_error`.

Errors identify the bound draft, requested source/model, as-of time, and safe
snapshot criteria where useful. They do not include SQL, full raw provider
payloads, local cache contents, or stack traces. Unexpected exceptions are
logged to stderr with a correlation ID and returned as `internal_error`.

The server never recovers by synchronizing, selecting a different draft,
inheriting an unrelated league context, changing source/model, or guessing a
player identity.

## Codex registration

The first release prefers a trusted project-scoped `.codex/config.toml`, not a
global registration. A broken or draft-specific Fantasy War Room server must
not prevent unrelated Codex sessions from starting. The local file contains
the active draft ID and should not be committed:

```toml
[mcp_servers.fantasy-war-room]
command = "uv"
args = [
  "run",
  "--project",
  "/home/el-ahrairah/fantasy-war-room",
  "fwr-mcp",
  "--draft-id",
  "DRAFT_ID",
  "--strategy",
  "logan-ppr-2flex-1.0",
]
startup_timeout_sec = 10
tool_timeout_sec = 30
required = false
default_tools_approval_mode = "writes"
enabled_tools = [
  "get_draft_state",
  "get_my_roster",
  "get_available_players",
  "recommend_pick",
  "compare_players",
  "get_position_outlook",
  "get_draft_strategy",
]
```

The server uses only the scoring context already persisted on the selected
draft snapshot. It has no option that can attach or inherit a different league
context; that remains an explicit `fwr sync`/`fwr watch` responsibility. An
optional `--draft-slot SLOT` may be appended for mock ownership.

For users who intentionally want a global registration, the equivalent CLI
form remains:

```console
codex mcp add fantasy-war-room -- \
  uv run --project /home/el-ahrairah/fantasy-war-room \
  fwr-mcp --draft-id DRAFT_ID
```

That global option is documented as opt-in, not the draft-night default.
`codex mcp list` verifies registration; `/mcp` verifies the active server in a
Codex session. The config uses `writes` approval behavior plus an allowlist of
the six baseline tools plus `get_draft_strategy`. `required = false` prevents initialization
failure from blocking the project session. These forms follow the official
Codex MCP documentation. The stdio entry point is integration-tested outside
the repository working directory; project-scoped Codex configuration remains a
documented operator setup rather than an application parser feature.

## Implemented test coverage

### Pure and schema tests

- request bounds, enums, frozen response models, and JSON schemas;
- optional `as_of` parsing, UTC normalization, and rejection of naive times;
- provenance completeness and absence of generation timestamps;
- position-outlook flags at exact boundaries, including unavailable
  `sharp_tier_drop` for a null top tier;
- deterministic player-name resolution and ambiguity handling;
- server instructions contain every required behavioral rule; and
- all six tools advertise read-only/non-destructive annotations.

### Read-only integration tests

Using synthetic local DuckDB fixtures:

- configured league and standalone mock contexts;
- required exact draft binding, no configured-league fallback, and draft-slot
  resolution;
- coherent as-of snapshot selection while newer snapshots exist;
- ranking/projection import-time visibility;
- explicit compatible scoring context only;
- database opened read-only, with migration/write attempts failing the test;
- no long-lived MCP database handle and one open/transaction/close lifecycle
  per attempt;
- bounded MCP lock retry and `database_busy` after exhaustion;
- bounded watcher-side lock retry without terminating its polling loop;
- no network/provider construction (network entry points patched to fail);
- watcher-style updates become visible on the next call, not midway through a
  call; and
- missing database/schema/snapshot failures remain structured.

Real subprocess contention tests run a watch-style writer and MCP-style reader
against the same temporary DuckDB file. They deliberately overlap connection
attempts and transactions, then assert:

- transient lock failures are retried on both sides;
- every supplied A -> B -> A and metadata-only watcher state is eventually
  persisted in order;
- the watcher remains alive after MCP contention;
- MCP returns either one fully committed state or `database_busy`, never a
  mixture of snapshot families;
- no connection remains open after a tool result; and
- repeated stress runs complete within the bounded retry/timeout policy.

If this test is unreliable on a supported platform, implementation stops for a
design review of the atomic read-replica fallback rather than silently relaxing
the guarantees.

### Tool behavior tests

- turn state and recent-pick ordering;
- projection-aware starters, FLEX, bench, vacancies, and unresolved picks;
- drafted exclusion through every retained Sleeper ID;
- available position filters and limit invariance;
- `recommend_pick` equals direct pure-engine output for all three policies;
- `recommend_pick` alone returns coherent turn, roster, candidate, and
  provenance data for a current-pick question;
- default model/source are `trusted-board-1.1` and
  `parlay-play-hybrid`;
- trusted rank/tier components and all weights are returned;
- comparisons of available, drafted, missing, and ambiguous players;
- partial projection provenance is retained;
- position depth/scarcity/tier flags carry raw evidence;
- no next-pick probability appears except the explicit uncalibrated state;
- same explicit `as_of`, snapshots, and arguments are byte-stable after
  canonical JSON serialization;
- live calls with unchanged state preserve facts, scores, ordering, and
  snapshot IDs while allowing the query cutoff to differ; and
- cross-tool fixtures with differing `draft_snapshot_id` require a refreshed
  primary recommendation in the server-instruction workflow.

### Protocol and launch tests

- launch the stdio server through the installed entry point;
- initialize, list tools, inspect schemas/instructions, and call every tool with
  an MCP client;
- assert domain failures are client-visible MCP results with `isError=true`
  while retaining the stable structured FWR error envelope;
- assert stdout contains only MCP protocol traffic and logs use stderr;
- launch with a working directory outside the repository;
- run via the implemented `uv run --project ... fwr-mcp` command;
- verify clean shutdown and useful startup/tool timeouts.

All existing M1, M2, projection, ranking, draft ingestion, and recommendation
tests remain green. Unit and integration tests never use the public internet.

## Implemented baseline contract

The implemented first MCP milestone provides:

1. all six tools return stable, versioned structured results;
2. every database connection used by MCP is demonstrably read-only;
3. no MCP code path imports or calls provider synchronization;
4. every response identifies the snapshots and deterministic model used;
5. drafted players cannot appear in an available-player response;
6. `recommend_pick` is identical to the existing engine for the same inputs;
7. `trusted-board-1.1` and `parlay-play-hybrid` are MCP defaults without
   changing CLI defaults;
8. source/model overrides are explicit and never silent;
9. no probability, ADP survival estimate, Monte Carlo result, or championship
   claim is produced;
10. every MCP call uses a bounded open/read/close lifecycle and reports
    `database_busy` after classified lock retries are exhausted;
11. watcher-side retry eventually persists every observed state without a
    read-contended MCP process crashing the watcher;
12. a concurrent watcher cannot yield a mixed-state tool response;
13. domain failures retain the FWR envelope and are marked `isError=true`;
14. explicit-as-of replay is byte-stable while live calls make only the
    narrower deterministic-facts guarantee;
15. MCP v1 requires an explicit draft ID;
16. the tested MCP SDK release is pinned exactly in both project metadata and
    lockfile;
17. the preferred project-scoped registration exposes exactly the six
    read-only tools and does not block unrelated Codex sessions;
18. Codex can start and use the server outside the repository directory; and
19. the documented draft-night workflow works end to end with Terminal 1
    watching and Terminal 2 running Codex.

## Deferred work

- provider synchronization or write tools;
- remote/HTTP MCP deployment and authentication;
- MCP resources or prompts beyond server instructions;
- ADP ingestion and calibrated next-pick survival probabilities;
- Monte Carlo draft simulation;
- opponent modeling;
- persistence of conversations or recommendations; and
- championship-probability evaluation.
