# M3: Deterministic Baseline Draft Recommendations

## Status

Approved design. Phase A standalone draft ingestion is implemented; Phase B
recommendation scoring is not implemented.

## Goal

M3 adds the first local, explainable answer to:

> Who should I draft right now?

The public entry point is `fwr recommend`. The first model is a deterministic,
versioned baseline for Sleeper NFL redraft snake drafts with one starting QB. It
uses the league's actual scoring settings, the user's roster, the current turn,
available CBS projections, and an independently transformed expert-ranking
signal.

M3 does not add an LLM, MCP, trained model, Monte Carlo simulation, opponent
strategy model, or championship-probability claim. Kicker and defense
projections remain available as intelligence, but neither position blocks the
first offensive recommender.

## Existing inputs and boundaries

M3 consumes existing immutable data rather than downloading anything:

- `draft_snapshots` and `draft_snapshot_picks` provide the league, draft
  metadata, completed picks, draft slots, roster IDs, and pick numbers.
- `player_directory_snapshots`, `player_observations`, and
  `player_provider_ids` provide as-of canonical identity and Sleeper-ID
  mappings.
- `ranking_snapshots` and `ranking_entries` provide Rotoworld overall rank and
  matching provenance.
- `projection_snapshots` and `projection_entries` provide CBS projections,
  league scoring provenance, exact or known-component totals, and identity
  status.
- The existing board establishes the drafted-player exclusion rule, but its
  ranking-only query is not sufficient for recommendations. M3 needs a
  dedicated read model that selects all four snapshot families consistently.

Decision code remains pure and must not perform network or database I/O. The
repository constructs a versioned recommendation input model; a function under
`fantasy_war_room.decision` computes the result from that model.

## Command design

```console
fwr recommend
fwr recommend --draft-id DRAFT_ID
fwr recommend --draft-slot 7
fwr recommend --source rotoworld
fwr recommend --limit 10
fwr recommend --as-of 2026-08-20T19:00:00Z
fwr recommend --json
```

Options:

- `--draft-id`: select a specific stored draft. Otherwise use the configured
  league's newest draft snapshot as of the decision time.
- `--draft-slot`: explicit one-based slot. It overrides user-to-slot discovery
  and is the supported path for standalone or imported mock drafts.
- `--source`: select the expert-ranking source, consistent with `fwr board`.
  M3 initially selects projection source `cbs` internally because only one
  statistical projection provider exists. A future `--projection-source`
  option can be added without changing `--source` semantics.
- `--limit`: number of ranked candidates returned. It must not alter the
  candidate universe, replacement levels, percentiles, or scores.
- `--as-of`: timezone-aware knowledge cutoff. Default is current UTC time.
- `--json`: use the existing stable `status`, `command`, `data`, and `error`
  envelope. Human rendering remains separate.

The command performs no implicit synchronization. Missing or incompatible
snapshots produce stable input/configuration errors with the missing snapshot
kind and selection criteria.

## Draft ingestion prerequisite and standalone mocks

`--draft-id` can only select a draft that has entered the immutable snapshot
store. M3 therefore includes a draft-ingestion prerequisite before the
recommender is considered draft-night ready:

```console
fwr drafts list
fwr sync --draft-id DRAFT_ID
fwr watch --draft-id DRAFT_ID
```

Implementation order is Phase A (these draft commands), then Phase B
(`fwr recommend`). They ship in the same M3 milestone or Phase A ships first;
the recommender is not declared draft-night ready without them.

- `fwr drafts list` uses a new Sleeper adapter operation for the configured
  user's drafts in the configured season. It lists league-linked and standalone
  mock draft IDs, draft type, status, team count, and league ID when present.
  It has stable human and JSON output. A local-only indicator shows whether
  each discovered draft already has snapshots.
- `fwr sync --draft-id` fetches that draft directly and its completed picks,
  instead of requiring discovery through the configured league. A league-linked
  draft fetches its own league. A standalone mock may use an explicitly supplied
  or configured league as its scoring context, but that association is recorded
  as context rather than falsely claiming the mock belongs to the league.
- `fwr watch --draft-id` fetches static draft/context metadata once and polls
  that draft's picks through the existing immutable A -> B -> A snapshot path.
  It does not switch back to the configured league's primary draft.

Standalone mock draft metadata supplies its draft order, snake settings, team
count, rounds, and roster-slot structure where Sleeper provides them. League
scoring still comes from an explicit compatible scoring context because M3 must
not infer custom scoring from a generic `ppr` label. If neither the draft nor an
explicit/configured league supplies scoring settings, ingestion may preserve
the draft, but recommendation fails with `mock_scoring_context_required`.
Compatibility validation reports team-count or roster-structure differences;
it never silently rewrites the mock. A small ordered migration may be required
to represent a nullable source league and a separate scoring-context league
without overloading the existing non-null `league_id` meaning.

All three commands preserve the current default league workflow when
`--draft-id` is absent. Their provider calls remain in adapters; repository and
decision code remain network-free.

## As-of and provenance contract

One recommendation uses a single UTC decision time `T` and records the exact
selected IDs. Selection is:

1. newest matching draft snapshot with `observed_at <= T`;
2. newest Sleeper player-directory snapshot with `observed_at <= T` and
   `fetched_at <= T`;
3. newest matching ranking snapshot with `observed_at <= T` and
   `imported_at <= T`;
4. newest CBS projection snapshot with `observed_at <= T` and
   `imported_at <= T`, matching the draft season and selected league scoring
   identity.

Using both source observation time and import time for imported intelligence
prevents a historical replay from seeing a ranking or projection that was only
loaded after `T`. Draft `observed_at` is already its local ingestion time.

The selected projection snapshot must match the selected league's canonical
scoring-settings hash. Its `league_snapshot_id`, `player_snapshot_id`, and
`scoring_calculator_version` remain visible in provenance. Canonical player IDs
join ranking and projection entries. A candidate requires a matched projection
entry; a ranking entry is optional and contributes only when it is matched.
This keeps projected players outside the Top 200 eligible while exposing their
missing expert signal. Drafted Sleeper IDs are excluded through all retained
Sleeper provider mappings, preserving the M2 collision policy.

Recommendation output records:

- decision timestamp;
- draft, player-directory, ranking, and projection snapshot IDs and timestamps;
- ranking source and source version;
- projection source and source version;
- ranking resolver version;
- projection scoring calculator version and scoring-settings hash;
- recommendation model, schema, roster allocator, replacement-level, scarcity,
  and survival-model versions.

Recommendations are derived read results in M3 and need not be persisted.
Their complete input provenance and versioned deterministic policy make them
reproducible. If later evaluation requires saved decisions, that is a separate
append-only observation table and migration.

## User draft-slot resolution

Slot resolution follows this precedence:

1. explicit `--draft-slot`;
2. configured immutable Sleeper user ID looked up in the draft payload's
   `draft_order` mapping;
3. an unambiguous prior pick whose `picked_by` equals the configured user ID,
   using that pick's `draft_slot`;
4. otherwise fail with `draft_slot_unresolved` and instruct the caller to pass
   `--draft-slot`.

The selected slot must be within `1..team_count`. Conflicting inferred slots are
a data-integrity error, never resolved by guessing. `picked_by` is only a
fallback because commissioners, co-managers, and automated picks can make it an
imperfect ownership signal.

When present, `slot_to_roster_id` is retained to relate the slot to a Sleeper
roster. Picks are attributed primarily by `draft_slot`, with the roster mapping
as a consistency check or fallback. The recommender does not require a Sleeper
user for a mock draft when `--draft-slot` is explicit.

## Roster reconstruction

The user's roster at `T` is every completed pick in the selected draft snapshot
assigned to the resolved draft slot. Each pick is joined to canonical identity
through its Sleeper ID. Its as-of player observation supplies position and
fantasy-position eligibility; stored pick metadata is diagnostic fallback only.
An unmapped drafted Sleeper ID remains excluded from availability and appears as
an unresolved roster item rather than being silently dropped.

League `roster_positions` is parsed into:

- fixed offensive slots: `QB`, `RB`, `WR`, `TE`;
- FLEX slots whose eligibility is RB/WR/TE (`FLEX` and supported equivalent
  Sleeper tokens);
- bench slots (`BN`);
- ignored-for-v1 starter slots (`K`, `DEF`/`DST`); and
- unsupported slots.

The initial model rejects superflex, multiple-QB, keeper, auction, and non-snake
formats with explicit unsupported-format details. Unknown flex tokens are not
guessed.

Roster allocation is projection-aware. Given all drafted offensive players
with projection baselines, the allocator finds the legal QB/RB/WR/TE/FLEX
assignment with maximum total starting-lineup projection. This is a small
deterministic maximum-weight assignment problem, not a fixed-slots-first greedy
pass. A player can occupy one slot, a slot can hold one eligible player, and
FLEX accepts RB/WR/TE.

The optimization objective is lexicographic:

1. maximize summed starter projection baseline;
2. maximize filled offensive starter slots;
3. prefer fixed assignments over FLEX when projected totals are equal;
4. prefer slot order `QB`, `RB`, `WR`, `TE`, `FLEX`;
5. break remaining ties by canonical player ID.

Exact and known-component baselines participate according to the documented
projection fallback and retain their completeness labels. Drafted players
without a projection baseline are reported as unmodeled roster depth and are
not assigned an invented value.

The result exposes the selected starting lineup, each player's assigned slot,
filled and vacant fixed/FLEX slots, bench and unmodeled depth, ignored K/DST
vacancies, unresolved roster picks, and total starting-lineup projection.

## Current pick and snake context

Let `N` be the league team count and `P` the next overall pick number:

```text
P = max(completed pick_no) + 1, or 1 when no picks exist
round = floor((P - 1) / N) + 1
direction = forward for odd rounds, reverse for even rounds
```

For draft slot `S`, its scheduled pick in round `R` is:

```text
user_pick(R) = (R - 1) * N + S                    when R is odd
user_pick(R) = (R - 1) * N + (N - S + 1)        when R is even
```

The context returns:

- current overall pick and round;
- forward/reverse direction;
- resolved user slot;
- whether the user is on the clock;
- the user's next scheduled pick;
- opponent picks before that selection when not on the clock;
- opponent picks between this pick and the following user selection when the
  user is on the clock.

The two interpretations are labeled rather than hidden behind one ambiguous
number. Recommendation remains available before the user's turn, but the output
includes `on_the_clock=false`. A completed draft returns a stable not-actionable
result.

## Projection baseline

For each matched available offensive player:

```text
projection_baseline = league_projected_points
                      if league_projected_points is not null
                      else league_known_component_points
```

The numerical baseline is accompanied by:

- `projection_value_kind`: `exact` or `known_component`;
- `scoring_completeness`;
- `unprojected_scoring_keys`;
- CBS projected points and CBS points per game;
- league projection source and calculator provenance.

Known-component values are useful deterministic inputs, not exact totals. The
model never substitutes CBS points for missing league-scored values. Candidates
without a league projection baseline cannot receive VORP and are omitted from
the scored set with an explicit exclusion count and reason.

## Replacement level v1

Baseline-1.0 uses a static structural replacement baseline calculated from the
complete matched projected player universe selected as of `T`. This universe
includes both drafted and undrafted players from the selected projection
snapshot. Availability affects candidate eligibility and scarcity, but it does
not remove players from replacement-level construction.

Consequently, replacement remains constant throughout a replay while the
projection snapshot, scoring context, team count, and roster structure remain
the same. Drafted starters are represented once in the full player universe and
are not counted again as unmet demand against the remaining pool.

For each position `p` in QB/RB/WR/TE:

```text
fixed_demand[p] = team_count * fixed_starter_slots[p]
```

FLEX demand is allocated across RB/WR/TE without imposing a fixed positional
split:

1. sort each position's complete projected universe by projection baseline
   descending, then canonical player ID;
2. reserve its first `fixed_demand[p]` players for fixed demand;
3. for each of `team_count * flex_slots` FLEX units, select the highest next
   unreserved RB, WR, or TE projection and assign that unit to its position;
4. define `total_demand[p] = fixed_demand[p] + allocated_flex_demand[p]`;
5. define replacement projection as the projection of the player at
   `total_demand[p]` in that position's complete projected universe, using
   one-based demand.

Then:

```text
VORP(candidate) = projection_baseline(candidate)
                  - replacement_projection(candidate.position)
```

The output exposes universe size, fixed demand, allocated FLEX demand, total
demand, replacement player ID, replacement projection, and value kind for each
position. If the complete universe cannot satisfy demand, replacement and VORP
are null for that position and affected candidates are not scored; the model
does not invent a replacement value. A position with zero fixed and allocated
FLEX demand is outside the league's offensive roster model and is likewise not
scored.

The replacement-model version is `structural-starter-demand-1.0`. A future
dynamic model would have to subtract already-filled league starter demand
before operating on the remaining pool. It must not apply full original demand
to only undrafted players. Changing to such a policy requires a new model
version and replay comparison.

## Positional scarcity v1

Scarcity measures the visible one-round positional drop rather than duplicating
VORP. For a candidate, sort remaining available players at the same position by
projection baseline and canonical ID. Let the lookahead contain up to the next
`team_count` players after the candidate—one league round of positional depth.

```text
scarcity_points = candidate_projection
                  - mean(projection of the positional lookahead)
```

If no following player exists, scarcity is null and receives the minimum
normalized scarcity component. The lookahead count, comparison player IDs,
mean, and raw point drop are returned. The use of one league round is structural
rather than a hidden constant.

## Expert-ranking signal

Overall rank remains independent from projection points. It is transformed to
a unitless expert percentile over the full matched ranking snapshot, not merely
the displayed `--limit` rows:

```text
expert_percentile = 1 - (ordinal_index - 1) / (ranked_player_count - 1)
```

`ordinal_index` comes from sorting by published overall rank and canonical ID.
The best ranked player receives 1 and the last receives 0. Ties receive the
same midpoint percentile. A one-player ranking set receives 1. Missing overall
rank receives a null raw percentile and zero expert-score contribution, plus an
important limitation.

This transformation makes the ordinal signal combinable without pretending
that rank 10 is a fixed number of fantasy points better than rank 20.

## Roster-fit adjustment v1

For every candidate, run the projection-aware lineup allocator before and after
adding the candidate:

```text
starter_projection_delta = max(0, lineup_projection_after
                                   - lineup_projection_before)
```

The candidate effect is classified from the optimal before/after assignments:

- `fills_fixed_vacancy`;
- `fills_flex_vacancy`;
- `upgrades_fixed_starter`;
- `upgrades_flex_or_rebalances_lineup` (including pushing an existing starter
  to FLEX and another player to the bench);
- `bench_depth` when the candidate does not enter the optimal lineup.

This prevents an elite player from being labeled irrelevant merely because the
nominal fixed position is occupied. The output includes lineup projection
before and after, starter projection delta, candidate slot, displaced player
and old/new slots, resulting bench movement, and vacancy changes.

Roster-fit value is normalized across the complete scored candidate universe:

```text
roster_fit_value = starter_projection_delta / maximum_candidate_delta
```

It is zero for every candidate when the maximum delta is zero. This keeps the
component in `[0, 1]` without a hidden vacancy constant. Roster fit remains
capped at ten percent of the score, so a bench candidate with exceptional VORP,
scarcity, and expert value can still win.

## Next-pick availability v1

Rotoworld overall rank is not calibrated ADP and must not be converted into a
probability. With the currently imported Top 200, M3 returns:

```json
{
  "status": "unsupported_uncalibrated",
  "probability_available_at_next_pick": null,
  "reason": "No calibrated ADP survival model is available"
}
```

If an as-of ranking snapshot contains ADP, M3 may additionally expose the
descriptive values `adp`, `adp_sd`, `next_user_pick`, and
`adp_minus_next_user_pick`. It still does not call that margin a probability and
does not include it in recommendation score v1.

A probability model requires historical ADP snapshots and completed drafts
segmented by season, scoring format, league size, and draft type. M3 evaluation
data must support held-out calibration measurement (for example Brier score and
reliability bins) before a versioned isotonic or logistic survival model can
produce probabilities. Until then the availability component weight is exactly
zero.

## Recommendation score v1

Raw VORP and scarcity are converted to empirical percentiles over the complete
scored candidate universe. For a value `x`:

```text
percentile(x) = (count(values < x) + 0.5 * count(values = x)) / count(values)
```

Higher is always better. Percentiles and the expert transformation are
calculated before applying `--limit`, ensuring output size cannot change a
score.

The initial policy is:

| Component | Weight | Rationale |
| --- | ---: | --- |
| VORP percentile | 50 | League-scored value above a roster-derived replacement is the primary signal. |
| Expert-rank percentile | 20 | Independent expert ordering supplies information not present in CBS projections. |
| Scarcity percentile | 20 | One-round positional drop rewards fragile tiers without dominating player value. |
| Roster-fit value | 10 | Optimal-lineup improvement matters, but exceptional long-term value can override it. |
| Next-pick probability | 0 | No calibrated survival data exists yet. |

```text
recommendation_score =
    50 * vorp_percentile
  + 20 * expert_percentile_or_zero
  + 20 * scarcity_percentile_or_zero
  + 10 * roster_fit_value
```

The score is in `[0, 100]`. These are provisional policy weights, not learned
coefficients and not claims of optimality. They live in one immutable model
specification, are returned in every response, and change only with a model
version bump. M3 acceptance includes sensitivity reporting against the three
baselines; it does not silently tune weights on the evaluation fixtures.

Final ordering is recommendation score descending, VORP descending, expert
overall rank ascending with null last, projection descending, normalized name,
then canonical player ID. This defines reproducibility even under exact ties.

## Candidate explanation schema

Every returned candidate includes:

- canonical and Sleeper player IDs, display name, position, and team;
- recommendation rank and score;
- every raw component, normalized component, and applied weight;
- league projection baseline, value kind, exact total if available,
  known-component total, CBS points, completeness, and unprojected keys;
- replacement player/projection, VORP, and replacement demand details;
- published expert overall and positional ranks and transformed percentile;
- scarcity lookahead, mean comparison projection, and point drop;
- roster-effect category and value, optimal lineup before/after, starter
  projection delta, slot/displacement details, and before/after vacancies;
- next-pick availability status, nullable probability, and any descriptive ADP
  margin;
- candidate-specific limitations;
- source snapshot provenance.

The response also includes global context:

- decision time and draft context;
- reconstructed user roster and vacancies;
- replacement-level table;
- model specification and weights;
- baseline recommendations;
- excluded-candidate counts by reason;
- global limitations.

All public models use explicit schema versions. Proposed initial versions:

- recommendation result schema: `1.0`;
- recommendation model: `baseline-1.0`;
- roster allocator: `max-projection-offensive-flex-1.0`;
- replacement model: `structural-starter-demand-1.0`;
- scarcity model: `one-round-drop-1.0`;
- survival model: `unavailable-1.0`.

## Baselines and evaluation

Every result identifies three deterministic baseline choices over the same
available, as-of candidate universe:

1. `highest_expert_rank`: lowest Rotoworld overall rank;
2. `highest_league_projection`: highest projection baseline, preserving whether
   it is exact or known-component;
3. `greedy_vorp`: highest raw VORP.

Offline replay compares recommendation v1 with these baselines at historical
draft states. Initial evaluation reports only observable, non-championship
metrics:

- recommendation/baseline agreement rate;
- projection baseline and VORP selected at each pick;
- positional and starter-vacancy distribution;
- whether a passed-over candidate survived to the user's next pick;
- projection/ranking/identity coverage and exclusion rates;
- deterministic replay equality;
- invalid-roster and unsupported-format counts.

No metric is described as championship probability or causal improvement.

## Proposed implementation boundaries

The recommendation result itself requires no persistence migration. The Phase
A direct-draft prerequisite may require the small ordered draft-context
migration described above so standalone mocks are represented truthfully.

Suggested modules:

- Sleeper adapter/service: user-draft discovery and direct draft-ID sync/watch
  before recommendation is declared draft-night ready;
- repository: direct draft snapshot support plus one read method that builds
  `RecommendationInputs` from selected immutable snapshots;
- `decision/recommend.py`: pure roster allocation, turn context, replacement,
  scarcity, normalization, scoring, and baseline functions;
- models: versioned input, context, candidate explanation, provenance, and
  result models;
- CLI: argument/configuration resolution only;
- rendering: human recommendation table and explanation summary;
- tests: synthetic local snapshots only, with no provider access.

SQL should lead from matched projection entries and left-join the selected
matched ranking entries by canonical player ID. It must not use ranking
`projected_points` as the CBS statistical projection. K/DST remain in
provenance and intelligence tables but are filtered from the v1 scored
candidate universe.

## Tests

### Slot, roster, and turn context

- configured user resolves through `draft_order`;
- explicit draft-slot override works without a configured user;
- override takes precedence over configured ownership;
- ambiguous or missing ownership returns a structured error;
- first pick, turn pick, reverse-round pick, and turn-boundary arithmetic;
- completed draft is not actionable;
- roster reconstruction at two as-of times;
- the allocator chooses the maximum-projection legal fixed/FLEX lineup;
- an elite player can replace a lower projected starter and move another player
  through FLEX to the bench;
- equal-projection lineup assignments use deterministic tie breaking;
- FLEX accepts RB/WR/TE and rejects QB;
- K/DST vacancies do not block offensive output;
- unsupported superflex or non-snake formats fail explicitly.

### Temporal and identity behavior

- no draft, player, ranking, or projection snapshot after `T` is selected;
- imported intelligence with `observed_at <= T` but `imported_at > T` is not
  visible in replay;
- a later ranking reprocessing is invisible before its import/observation time;
- drafted exclusion covers every retained Sleeper ID for a canonical player;
- unresolved drafted IDs remain excluded and are reported;
- ranking and projection entries join only by canonical identity;
- changing `--source` selects the correct as-of ranking provenance.

### Projection, replacement, and scoring

- exact league points are preferred when present;
- partial projections use known-component points and retain limitations;
- CBS projected points are never substituted for league projection baseline;
- FLEX demand is allocated to the highest marginal RB/WR/TE projections;
- replacement levels change predictably with team count and roster slots;
- replacement uses drafted and undrafted players from the complete selected
  projection universe;
- drafting players without changing projection/league context does not move
  replacement deeper;
- replacement at two draft as-of times is identical when structural inputs are
  identical;
- insufficient position depth is explicit rather than assigned a zero;
- VORP is calculated against the correct positional replacement;
- `--limit` does not affect replacement, percentiles, or scores.

### Scarcity, ranking, and roster fit

- one-round positional lookahead uses team count;
- ordinal expert rank is transformed independently from points;
- tied ranks and tied component values are deterministic;
- missing expert rank is visible and receives no expert contribution;
- fixed and FLEX vacancies produce the correct optimal-lineup delta;
- a candidate can upgrade an occupied position and displace/reassign starters;
- a candidate outside the optimal lineup has zero starter projection delta;
- exceptional bench value can outrank a lower-value starter need;
- all score components sum exactly to the reported total.

### Availability and baselines

- Rotoworld rank never produces a fabricated survival probability;
- ADP fields produce only a descriptive next-pick margin in v1;
- each baseline selects its documented candidate;
- replay records whether passed-over candidates actually survived without
  treating that observation as a model probability.

### Interfaces and reproducibility

- `fwr drafts list` discovers standalone and league-linked Sleeper drafts;
- `fwr sync --draft-id` persists the selected draft rather than the configured
  league's primary draft;
- `fwr watch --draft-id` polls the selected draft and preserves A -> B -> A;
- standalone mocks work with explicit draft slot and scoring-context provenance;
- missing mock scoring context returns a structured error;
- human and JSON renderers consume the same domain result;
- JSON envelope and public schemas are stable;
- repeated calls with identical inputs are byte-equivalent after excluding no
  fields (the result contains no wall-clock generation timestamp);
- shuffled database row order produces identical results;
- command works outside the repository directory;
- recommendation code performs no network I/O;
- all M1, M2, ranking, identity-collision, projection, migration, board, and CLI
  tests continue to pass.

## Acceptance criteria

M3 is accepted when:

1. `fwr recommend` answers from local snapshots only and supports every planned
   option and stable JSON output.
2. `drafts list`, direct draft-ID sync/watch, and configured-user/explicit mock
   slot resolution make both league drafts and standalone mocks ingestible.
3. The user roster, current turn, snake direction, and next selections are
   reconstructed correctly as of `T`.
4. Static structural FLEX-aware replacement, VORP, scarcity, expert
   transformation, and projection-aware roster effects exactly follow their
   versioned definitions.
5. Known-component projections remain explicitly distinct from exact totals.
6. No uncalibrated next-pick probability is emitted or scored.
7. Every score is reproducible from returned components and weights.
8. Baseline selections and non-claiming replay metrics are available for
   evaluation.
9. Snapshot provenance proves that no future information entered an as-of
   result.
10. Existing behavior and the complete test suite remain green.

## Known limitations after M3

- The score weights are transparent policy choices, not empirically optimized
  coefficients.
- Static structural replacement does not react to position runs within an
  unchanged projection context; scarcity supplies the current available-pool
  depth signal.
- Projection completeness is partial when CBS omits league scoring events; this
  is disclosed per candidate.
- Rotoworld rank is expert opinion, not ADP or calibrated availability.
- The model does not predict opponent selections, roster construction, injury,
  bye-week interaction, or season outcomes.
- K/DST are preserved but not scored by the first offensive recommender.
- Superflex, multiple-QB, keeper, auction, dynasty, and non-Sleeper formats are
  outside the initial scope.
- No recommendation is evidence of championship-probability improvement.
