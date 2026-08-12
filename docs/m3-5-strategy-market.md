# M3.5: Strategy Profiles and Market Context

## Status

M3.5A is implemented. M3.5B remains approved design and is not implemented.

M3.5A adds a deterministic, configurable strategy layer after
`trusted-board-1.1`, followed by immutable ADP intelligence and market timing.
It does not change the existing raw recommendation policies or permit an LLM to
become authoritative for draft facts or calculations.

The milestone is divided into:

- **M3.5A:** strategy profiles, target semantics, roster-construction policy,
  raw-versus-adjusted recommendations, and strategy-aware MCP behavior.
- **M3.5B:** immutable ADP observations, market timing, deterministic opponent
  positional demand, target-window refinement, and decision logs.

## Current architecture

Fantasy War Room is a local-first, time-aware decision system:

```text
Sleeper adapters
  -> explicit fwr sync / fwr watch
  -> append-only DuckDB snapshots
  -> as-of RecommendationInputs
  -> deterministic recommendation engine
  -> CLI rendering or read-only stdio MCP tools
  -> Codex conversational explanation
```

The established boundaries remain authoritative:

- Provider network I/O occurs only behind adapters during explicit commands.
- Recommendation and MCP calls never synchronize implicitly.
- Draft, player-directory, ranking, and projection observations are immutable.
- Every persisted observation carries an UTC `observed_at` timestamp.
- Imported intelligence is visible in a historical replay only when both its
  source observation and local import occurred by the requested cutoff.
- Canonical player identity is the join boundary between draft state,
  rankings, and projections.
- CBS projections are scored using the selected draft's persisted league
  scoring context and scoring-settings hash.
- Repository code constructs versioned recommendation inputs; decision code is
  pure and performs no database or network I/O.
- `baseline-1.0`, `trusted-board-1.0`, and `trusted-board-1.1` are deterministic
  raw policies.
- CLI and MCP presentation limits are applied only after scoring the complete
  candidate universe.
- MCP binds to one explicit draft, uses one short-lived read-only transaction
  per call, and never initializes or migrates the database.
- Fantasy War Room is authoritative for availability, projections, rankings,
  roster allocation, scarcity, VORP, scores, and provenance. Codex may explain
  or synthesize these results but must not invent or replace them.

M3.5 extends this flow rather than replacing it:

```text
RecommendationInputs
  -> trusted-board-1.1 raw recommendation
  -> resolved strategy profile
  -> deterministic strategy adjustment
  -> preserved raw result + strategy-adjusted result
```

The deferred M3.5B design would supply additional immutable inputs to that same
strategy adjustment:

```text
ADP observations + draft state
  -> market context + opponent demand
  -> target-window refinement
  -> strategy-adjusted recommendation
```

## Non-negotiable compatibility rules

- Existing `baseline-1.0`, `trusted-board-1.0`, and `trusted-board-1.1` remain
  unchanged.
- The strategy layer operates after `trusted-board-1.1`; it does not alter raw
  weights, transformations, calculations, candidate fields, or ordering.
- Both raw and strategy-adjusted recommendations are returned.
- Existing unstrategized CLI and MCP behavior remains available.
- Strategy configuration is data, not scattered player-specific branching.
- All adjustment reasons and accepted raw-value costs are visible.
- No target rule can introduce availability, ranking, projection, ADP, or
  probability claims absent from authoritative inputs.
- Historical and append-only guarantees apply to every new persisted market or
  decision observation.
- M4 must consume the same strategy profile and market semantics used by live
  recommendations.

## Active strategy profile

The initial profile is named `logan-ppr-2flex-1.0` and applies to:

- 10-team NFL redraft;
- full PPR;
- snake draft;
- one QB;
- one TE;
- two FLEX slots; and
- non-keeper play.
- draft slot 7.

Its construction philosophy is value-flexible. Trusted analyst rankings remain
the primary raw player-quality signal. Targets may override nearby raw choices
at a transparent, configured cost, while major raw-value differences may still
override a target.

The approved requirements are:

- WR-WR starts are allowed.
- Chase Brown is a high-priority target at 2.04 / pick 14, but too early at
  1.07.
- Trey McBride must never be selected at 2.04.
- McBride's earliest acceptable selection is 3.07 / pick 27.
- Missing McBride is an acceptable outcome rather than a reason to reach.
- Colston Loveland is the preferred configurable fallback in approximately
  Rounds 5–6.
- Kyler Murray is a preferred later target, not an instruction to draft him
  immediately.
- QB2 and TE2 have diminishing utility.
- TE3 is prohibited in this format.
- Eligible RB/WR bench depth is generally preferred over redundant QB/TE
  depth. M3.5 does not call that depth "upside" because it has no ceiling or
  variance input.
- Enough final selections must remain to fill K and DEF.
- Existing `baseline-1.0`, `trusted-board-1.0`, and `trusted-board-1.1` remain
  unchanged.

FLEX represents roster optionality. It is not a distinct position that must be
filled early, and the profile must not impose a generic balanced-build or
early-RB requirement.

## M3.5A: Strategy profiles

### Profile schema and loading

Define a frozen, explicitly versioned public model such as
`fwr.strategy-profile/1.0`. It contains:

- profile name and schema version;
- strategy-adjuster version;
- `required_raw_model`, initially `trusted-board-1.1`;
- `required_ranking_source`, initially `parlay-play-hybrid`;
- compatible sport, format, team count, scoring, and roster constraints;
- construction philosophy;
- target definitions and fallback relationships;
- positional diminishing-return rules;
- roster-completion constraints; and
- generic adjustment thresholds and deterministic tie-breaking rules.

The initial profile has these explicit, provisional parameters:

```json
{
  "profile_name": "logan-ppr-2flex-1.0",
  "required_raw_model": "trusted-board-1.1",
  "required_ranking_source": "parlay-play-hybrid",
  "sport": "nfl",
  "draft_type": "snake",
  "league_type": "redraft",
  "keeper_status": "non_keeper",
  "scoring_format": "full_ppr",
  "compatible_draft_slots": [7],
  "k": 1,
  "defense": 1,
  "default_max_target_raw_score_deficit": 5.0,
  "default_max_target_raw_rank_displacement": 2,
  "qb2_policy": {
    "demotion_class": "redundant_qb_depth",
    "late_round_exception_start_round": null
  },
  "te2_policy": {
    "demotion_class": "redundant_te_depth",
    "late_round_exception_start_round": null
  },
  "te3_policy": {"prohibited": true},
  "bench_depth_policy": {
    "prefer_positions": ["RB", "WR"],
    "over_demotion_classes": ["redundant_qb_depth", "redundant_te_depth"]
  },
  "roster_completion_guard": {
    "required_positions": ["K", "DEF"],
    "trigger": "remaining_user_picks_lte_unfilled_required_slots"
  }
}
```

Every value is profile-configurable and provisional. The initial Brown ceiling
of `5.0` raw-score points and two raw ranks is justified only by the prior
Brown/Amon-Ra mock fixture, where the raw gap was small. It is not an evaluated
optimal coefficient and must not be generalized into a championship claim.

`late_round_exception_start_round: null` means no late-round exception. If a
future profile supplies a round, the QB2 or TE2 demotion stops at that round;
it does not promote the redundant position, bypass TE3 prohibition, or bypass
the K/DEF completion guard.

The required raw model and ranking source are compatibility constraints.
Conflicting CLI or MCP inputs fail with a structured compatibility error by
default. A future intentional-override option is permitted only if the response
visibly records the requested value, required value, override flag, and altered
profile hash; silent overrides are prohibited.

Before running the raw model for a strategy-aware request, compatibility is
validated against normalized `RecommendationInputs`: NFL sport, 10 teams,
slot 7, snake draft, redraft league type, non-keeper status, full-PPR scoring,
one starting QB, one starting TE, and two FLEX slots. League type comes from
the persisted canonical Sleeper `settings.type`; scoring format comes from the
persisted normalized reception scoring value. Missing canonical league type is
`unknown` and therefore incompatible rather than guessed. Failures use
`strategy_profile_incompatible` with expected/actual constraints and safe
draft/scoring provenance. Unstrategized recommendation remains available.

Editable profiles live in an XDG-compatible user configuration directory, for
example:

```text
$XDG_CONFIG_HOME/fantasy-war-room/strategies/logan-ppr-2flex-1.0.json
```

Configuration precedence is:

```text
CLI --strategy
  -> FWR_STRATEGY or FWR_MCP_STRATEGY
  -> active strategy in user configuration
  -> no strategy
```

Loading a strategy does not access the network or mutate the analytical
warehouse. The normalized effective profile is canonicalized and hashed.
Strategy-aware responses and persisted decision logs include the profile name,
schema version, strategy-adjuster version, and profile hash. The normalized
effective rules must also be queryable so a profile edit cannot make an earlier
decision uninterpretable.

Configured players should use stable provider identifiers, preferably Sleeper
player IDs, with names retained for diagnostics. Strict canonical name
resolution may be supported as an explicit fallback, but ambiguity must fail;
fuzzy identity guesses are prohibited.

### Target-player semantics

Targets use a reusable data model rather than player-name branches. A target
may define:

- priority;
- primary or fallback role;
- `window_mode`: `hard_gate` or `promotion_only`;
- preferred overall pick;
- earliest and latest overall pick;
- earliest and latest round;
- maximum accepted raw-score deficit;
- maximum accepted raw-rank displacement;
- allowed override positions or target groups;
- prerequisites and fallback activation conditions;
- behavior after acquisition by the user; and
- behavior after selection by another team.

Window modes have exact semantics:

- `hard_gate`: outside the configured window the target is strategy-ineligible
  and cannot appear in the actionable adjusted ordering. The unchanged raw
  result may still contain the player.
- `promotion_only`: outside the configured window the player remains eligible
  at unchanged raw rank but receives no target promotion. Inside the window,
  promotion is still limited by configured raw-cost ceilings.

Round constraints determine eligibility. `preferred_overall_pick` is metadata
for the expected price at a compatible draft slot; it never changes a round
gate into a universal overall-pick rule. The initial profile is explicitly
compatible only with slot 7, so its Round 2 selection is 2.04 / pick 14 and its
Round 3 selection is 3.07 / pick 27. A different slot fails profile
compatibility rather than reusing those overall picks incorrectly.

The provisional initial target configuration is:

| Target | Window mode | Round window | Preferred pick metadata | Maximum raw-score deficit | Maximum raw-rank displacement |
| --- | --- | --- | ---: | ---: | ---: |
| Chase Brown | `hard_gate` | Round 2 or later | 14 | 5.0 | 2 |
| Trey McBride | `hard_gate` | Round 3 or later | 27 | 5.0 | 2 |
| Colston Loveland | `promotion_only` | Rounds 5–6 | null | 5.0 | 2 |
| Kyler Murray | `promotion_only` | deferred until market context | null | 5.0 | 2 |

These values are independently configurable per target. The repeated `5.0`
and `2` values are provisional defaults, not evidence that every target should
have the same long-term ceiling.

At each draft state, a target has a derived deterministic state such as:

- `too_early`;
- `in_window`;
- `deferred_pending_market_context`;
- `acquired_by_user`;
- `selected_by_opponent`;
- `window_expired`; or
- `fallback_inactive` / `fallback_active`.

This state comes from the selected draft snapshot, effective profile, and—once
available—selected market snapshot. It is not conversational memory.

#### Chase Brown

- High-priority target.
- `window_mode=hard_gate`, `earliest_round=2`, and
  `preferred_overall_pick=14` for compatible slot 7.
- He is truly strategy-ineligible at 1.07, not merely unpromoted. He remains
  visible in the preserved raw recommendation for auditability.
- At pick 14, he may override a nearby raw WR recommendation only when his raw
  score deficit is no more than the provisional `5.0` ceiling and his raw-rank
  displacement is no more than the provisional `2` ceiling.
- The output reports the exact raw score deficit, raw rank displacement, and
  rule that allowed or rejected the override.
- A major raw-value difference still wins.
- If Brown was selected earlier, continue value-flexibly; do not manufacture a
  compensating RB need, and continue allowing WR-WR.

#### Trey McBride

- High-priority TE target.
- `window_mode=hard_gate` and `earliest_round=3`.
- He is strategy-ineligible throughout Rounds 1–2, including 2.04 / pick 14.
- `preferred_overall_pick=27` records the preferred slot-7 Round 3 price; pick
  27 is not a universal Round 3 threshold.
- If available in Round 3 or later, he is in-window, subject to configured raw
  cost protection.
- If another team selects him before Round 3, record an acceptable missed
  target and activate the configured fallback path.
- Missing McBride never triggers an immediate reach for a lesser TE.

#### Colston Loveland

- Preferred McBride fallback.
- `window_mode=promotion_only`, `earliest_round=5`, and `latest_round=6` in the
  provisional initial profile.
- Initial manual window is approximately Rounds 5–6.
- The window is editable configuration, not an embedded player-specific
  constant.
- M3.5A uses the manual window.
- M3.5B may refine it using compatible immutable market observations while
  retaining both the manual and market-derived windows in output.
- Once Loveland or another intended TE is acquired, TE2 receives the configured
  `redundant_te_depth` demotion class and TE3 remains prohibited.

#### Kyler Murray

- Preferred later QB target.
- `window_mode=promotion_only` with no manual earliest round in M3.5A.
- This encodes an acquisition plan, not an instruction to draft him now.
- M3.5A must not invent an authoritative round before compatible ADP context
  exists. His state remains deferred rather than promoted immediately.
- M3.5B activates a window using explicit market context.
- Once acquired, QB is intentionally filled and QB2 receives the configured
  `redundant_qb_depth` demotion class.
- If another team selects him, mark the target missed and use the configured
  fallback behavior instead of repeatedly promoting or discussing him.

### Raw versus strategy-adjusted recommendations

The decision boundary is:

```python
raw = recommend(inputs, "trusted-board-1.1")
adjusted = apply_strategy(
    raw=raw,
    inputs=inputs,
    profile=resolved_profile,
    market_context=None,
)
```

The raw result remains the existing, complete `trusted-board-1.1` result. The
strategy layer returns a new versioned wrapper rather than changing the raw
schema.

Each adjusted candidate includes:

- raw rank and raw score;
- strategy rank and target-promotion class;
- raw score deficit from the leading raw candidate;
- raw rank displacement;
- applied target promotions;
- positional utility penalties;
- hard prohibitions;
- roster-completion effects; and
- structured reason codes with human-readable explanations.

The overall response includes:

- complete or explicitly embedded raw recommendation;
- adjusted candidate ordering;
- target states;
- effective profile and profile hash;
- raw and strategy model versions;
- market-context status; and
- unchanged recommendation input provenance.

The initial deterministic adjustment order is:

1. Calculate the complete `trusted-board-1.1` result.
2. Assign hard eligibility/prohibition, including `hard_gate` windows and TE3.
3. Assign roster-completion directives, including the K/DEF guard.
4. Assign target-promotion class after applying raw-cost ceilings.
5. Assign positional utility/demotion class.
6. Preserve unchanged raw rank.
7. Use canonical player identity as the final tie-breaker.

There is no second weighted strategy score. Raw recommendation scores are
copied unchanged and never recalculated. The adjusted ordering is the stable
lexicographic policy key:

```text
(
  hard_eligibility_or_prohibition,
  roster_completion_directive,
  target_promotion_class,
  positional_utility_or_demotion_class,
  raw_recommendation_rank,
  canonical_player_id,
)
```

Each enum defines an explicit best-to-worst order in the profile schema.
Target promotion may improve the promotion class only when both the score and
rank ceilings pass. Positional demotion may move redundant QB/TE depth below
eligible RB/WR depth, but never changes raw rank or raw score.

The initial enum order is explicit:

- eligibility: `eligible` before `prohibited`; prohibited candidates are
  omitted from the actionable ordering but retained in raw/audit output;
- completion: `normal_selection` or the terminal
  `roster_completion_required` directive, which makes offensive candidates
  non-actionable rather than merely reranking them;
- target promotion: `eligible_target_within_cost` before `no_promotion`;
- positional utility: `normal_depth` before `redundant_qb_depth` and
  `redundant_te_depth`; the two redundant classes tie and therefore fall back
  to unchanged raw rank; and
- final ties: unchanged raw rank, then canonical player ID.

A configured QB2/TE2 late-round exception changes that candidate's positional
class from redundant depth back to `normal_depth` beginning at the configured
round. The initial profile sets both exceptions to null.

The complete raw universe and strategy ordering are always evaluated before
presentation limiting. The shared `limit_strategy_result` helper slices only
the actionable adjusted `candidates`; it does not alter the embedded raw
result, `evaluated_candidates`, ranks, target states, decisions, actionability,
or completion directive. CLI and MCP use this same helper.

This prevents an opaque target bonus from overwhelming arbitrary raw value and
makes every override auditable.

### Positional utility and roster completion

Position counts are derived from completed picks assigned to the user's draft
slot. They are distinct from the projection-maximizing starter allocation.

- Filling FLEX does not create a forced positional requirement.
- After one QB is intentionally acquired, QB2 receives
  `redundant_qb_depth`; the initial profile has no late-round exception.
- After one TE is intentionally acquired, TE2 receives
  `redundant_te_depth`; the initial profile has no late-round exception.
- TE3 is hard-ineligible in every round, including any future TE2 exception.
- Eligible RB/WR bench depth sorts ahead of the two redundant-depth classes.
  No ceiling, variance, breakout, or "upside" property is inferred.
- No penalty is applied merely because RB and WR counts are unbalanced.

The strategy input must expose all roster slots, including K and DEF/DST, user
position counts, draft rounds, remaining user selections, and outstanding
mandatory roster slots.

Let `R` be remaining user selections and `M` be unfilled required K plus DEF
slots. When `R <= M`, the offensive recommendation becomes non-actionable and
returns `roster_completion_required` with `R`, `M`, and the missing positions.
When `R > M`, exactly `M` final selections remain reserved but offensive
candidates remain actionable. M3.5A does not invent a K or DEF ranking policy.

The public result sets `actionable=false`, `actionable_choice=null`, and an
empty actionable `candidates` tuple when the guard fires. It preserves the
complete raw recommendation and complete `evaluated_candidates` diagnostic
ordering. The directive uses rule version `required-k-def-reservation-1.0` and
labels `R == M` as `exact_boundary` and `R < M` as `already_impossible`. The
already-impossible state never restores an offensive recommendation.

### Strategy temporal semantics

An explicit recommendation `as_of=T` selects warehouse observations as of
`T`; it does not time-travel an editable XDG strategy file. M3.5A uses the
explicitly supplied or currently resolved profile at call time.

Strategy provenance includes:

- profile name, schema version, and canonical hash;
- `profile_temporal_status=current_explicit_profile`;
- warehouse decision cutoff `T`; and
- whether an immutable recorded profile/decision snapshot was used.

Historical strategy-as-of reconstruction is unavailable in M3.5A. It becomes
valid only when an exact immutable decision or profile snapshot is explicitly
selected in M3.5B or later. Responses must not imply that today's editable
profile was the profile in force at historical time `T`.

## M3.5B: ADP and market context

### Immutable ADP intelligence

ADP becomes a first-class observation family rather than being inferred from
expert overall rank or overloaded into an unrelated ranking snapshot.

An ADP snapshot contains:

- snapshot ID;
- source and source version;
- season;
- league size;
- scoring format;
- draft type;
- UTC `observed_at`;
- UTC `imported_at`;
- payload hash;
- identity resolver version;
- total, matched, unresolved, and ambiguous row counts; and
- schema version.

Entries contain canonical identity, source row, overall ADP, dispersion when
provided, sample size when provided, position/team metadata, resolution status
and method, and the raw payload. Unresolved and ambiguous rows are preserved in
an issue table.

Selection at time `T` requires:

- `observed_at <= T`;
- `imported_at <= T`;
- matching season, league size, scoring classification, and draft type; and
- a compatible, explicitly selected source.

Incompatible data is reported rather than borrowed silently. Initial ingestion
may be an explicit local CSV command. Future network providers remain behind
adapter interfaces.

### Market timing and window refinement

Market context is deterministic and descriptive. It reports:

- ADP snapshot provenance;
- player ADP, dispersion, and sample size when available;
- current overall pick;
- next and following user picks;
- picks until the user's next selection;
- ADP relative to the current pick and next user pick;
- the configured manual target window;
- the market-derived window;
- the effective window;
- refinement model version; and
- missing or incompatible data limitations.

Permitted classifications include `too_early`, `market_reach`,
`market_aligned`, `market_fall`, and `in_effective_window`. These are not
survival probabilities. M3.5B must not claim a player will be available at the
next pick or emit a percentage without a separately evaluated calibration
model.

Manual target windows remain the authoritative fallback when market data is
absent. Market refinement never silently erases them. Kyler Murray becomes an
active later target only when compatible market context supplies an effective
window. Loveland's approximate Rounds 5–6 window may be refined but remains
visible and configurable.

### Deterministic opponent positional demand

Opponent demand uses only persisted draft facts and selected intelligence:

- each opponent's drafted position counts;
- fixed starter vacancies;
- remaining FLEX needs;
- intervening snake-draft picks before the user's next selection;
- available players and market ranks; and
- one versioned deterministic allocation policy.

A v1 algorithm reconstructs opponent roster needs, allocates FLEX demand
across RB/WR/TE under a documented rule, deterministically assigns likely
positions through intervening picks, and aggregates positional pressure. Ties
use market rank, trusted rank, canonical player ID, and opponent slot in a
fixed order.

The result is a deterministic demand scenario, not a claim about an individual
opponent's intent or a probability of player survival. Its inputs, assumptions,
and model version are returned.

### Decision logs

M3.5B introduces explicit append-only decision observations for replay and
evaluation. A log records:

- decision ID and UTC `observed_at`;
- draft ID and user slot;
- exact draft, player, ranking, projection, and ADP snapshot IDs;
- raw policy version;
- strategy profile name, schema version, and hash;
- market and opponent-demand model versions;
- raw and adjusted orderings;
- target states and adjustment reasons;
- chosen player when later supplied explicitly; and
- canonical input and result hashes.

Plain `recommend` remains a derived read. Persistence requires an explicit CLI
operation such as `fwr decisions record` or `fwr recommend --record`. MCP stays
read-only and does not write decision logs.

## MCP integration

### Startup contract

Add an optional strategy selection:

```console
fwr-mcp \
  --draft-id DRAFT_ID \
  --strategy logan-ppr-2flex-1.0 \
  [--draft-slot SLOT] \
  [--source parlay-play-hybrid] \
  [--model trusted-board-1.1] \
  [--database PATH]
```

`FWR_MCP_STRATEGY` supplies the environment layer. A profile that requires
`trusted-board-1.1` must reject a conflicting raw model explicitly.

The server remains bound to one draft and performs no provider calls,
synchronization, migrations, generic SQL, or writes. Strategy, recommendation,
and M3.5B market inputs are selected coherently within the same short-lived
read-only transaction.

### Tools

M3.5A adds `get_draft_strategy`. It returns:

- effective strategy profile and hash;
- draft compatibility result;
- construction philosophy;
- target definitions and current derived states;
- fallback relationships;
- positional diminishing-return rules;
- roster-completion constraints;
- raw base model and strategy-adjuster versions; and
- identity-resolution and snapshot provenance.

`recommend_pick` becomes strategy-aware when a startup strategy is active. It
returns both the unchanged raw recommendation and the strategy-adjusted result.
It never flattens the two into an unexplained single score.

M3.5B adds:

- `get_market_context`; and
- `get_opponent_demand`.

These tools share the optional `as_of` contract and return the selected ADP
snapshot and draft snapshot IDs. When combining calls, clients must compare
both identifiers and refresh `recommend_pick` if either changed.

### Fresh-session initialization instructions

Server initialization instructions are generated dynamically from the selected
profile. A fresh Codex session must learn, without conversational memory, that:

1. Fantasy War Room is authoritative for synchronized draft facts and
   deterministic calculations.
2. For "Who should I take?", call `recommend_pick` first.
3. The active profile is `logan-ppr-2flex-1.0`.
4. Use `get_draft_strategy` before explaining construction preferences, target
   windows, fallbacks, or positional utility.
5. Distinguish raw `trusted-board-1.1` results from strategy adjustments.
6. Never recommend a target outside the effective allowed window.
7. Never invent availability, projections, rankings, ADP, opponent intent, or
   survival probabilities.
8. Missing McBride is acceptable and does not justify reaching for another TE.
9. Kyler Murray is a later plan, not an immediate-pick instruction.
10. MCP does not synchronize; stale state requires checking the separate
    `fwr watch` process.

Critical constraints are repeated in relevant tool descriptions so safety does
not depend on initialization instructions alone.

## CLI contracts

M3.5A proposes:

```console
fwr strategies list
fwr strategies show logan-ppr-2flex-1.0
fwr strategies validate PATH
fwr strategies install PATH
fwr strategies active
fwr recommend --strategy logan-ppr-2flex-1.0
```

M3.5B proposes:

```console
fwr adp import FILE --source SOURCE --season 2026 \
  --scoring ppr --league-size 10 --draft-type snake \
  --source-version VERSION --observed-at TIMESTAMP
fwr adp list
fwr adp unresolved
fwr market-context --draft-id DRAFT_ID --strategy PROFILE
fwr opponent-demand --draft-id DRAFT_ID --strategy PROFILE
fwr decisions record --draft-id DRAFT_ID --strategy PROFILE
fwr decisions list
```

Final command naming may align with existing plural command groups, but these
behaviors are required:

- all finite commands support stable JSON envelopes;
- human and JSON rendering remain separate from domain logic;
- paths are XDG-compatible and independent of current working directory;
- recommendation and market-context commands perform no implicit sync;
- no-strategy `fwr recommend` preserves current behavior; and
- conflicting profile/model or incompatible market inputs fail explicitly.

## Database migrations

The current schema ends at migration 8. Planned ordered additions are:

- **Migration 9:** immutable `adp_snapshots`, `adp_entries`, and
  `adp_match_issues` tables.
- **Migration 10:** append-only strategy decision logs and candidate/reason
  rows.
- **Optional later migration:** immutable strategy-profile snapshots only if
  historical profile-as-of selection is required.

Editable strategy source files remain user configuration rather than mutable
warehouse records. Each result and decision log embeds or identifies the
normalized effective profile, its hash, and its temporal status. In M3.5A,
`as_of` applies only to warehouse observations; the editable profile is the
current explicitly resolved profile.

Every migration must be transactional, ordered, idempotently initialized, and
tested against both fresh and upgraded databases without data loss.

## Public tool and model contracts

Existing raw recommendation models retain their schemas. New versioned models
include equivalents of:

- `fwr.strategy-profile/1.0`;
- `fwr.strategy-recommendation/1.0`;
- `fwr.market-context/1.0`;
- `fwr.opponent-demand/1.0`; and
- `fwr.decision-log/1.0`.

The strategy-aware MCP recommendation should use a new response schema such as
`fwr.mcp.recommendation/2.0`, because preserving raw and adjusted results is a
material contract change. Unstrategized compatibility behavior may continue to
return the existing MCP recommendation schema.

All public models are frozen, explicitly versioned, deterministic under equal
inputs, and include exact provenance. Presentation limits never change raw or
adjusted scoring, target eligibility, market calculations, or baselines.

## Tests

### M3.5A profile and adjustment tests

- XDG profile discovery and configuration precedence.
- Profile schema, window, threshold, and league-compatibility validation.
- Required raw-model and ranking-source conflicts fail unless a future visible
  intentional override is explicitly requested.
- Slot 7 compatibility makes Round 2 equal pick 14 and Round 3 equal pick 27;
  other draft slots fail profile compatibility.
- Stable provider-ID resolution and ambiguous-name rejection.
- Byte-equivalent raw results for `baseline-1.0`, `trusted-board-1.0`, and
  `trusted-board-1.1` before and after strategy support.
- Chase Brown's `hard_gate` makes him strategy-ineligible at 1.07 while leaving
  him visible in the raw result.
- Chase Brown may override a nearby raw WR at 2.04 only within configured raw
  ceilings of `5.0` score points and two raw ranks.
- A major raw-value gap overrides the Brown target.
- If Brown is already selected, the result remains value-flexible and permits
  WR-WR.
- McBride's `earliest_round=3` hard gate prohibits him at 2.04; pick 27 is
  preferred slot-7 metadata, not a universal Round 3 threshold.
- Missing McBride activates the fallback without causing an immediate TE
  reach.
- Loveland is promotion-only in configured Rounds 5–6 and remains raw-eligible
  without promotion outside that window.
- Kyler receives no immediate promotion without market context.
- Acquired or missed targets stop repeated promotion.
- QB2 and TE2 receive their explicit redundant-depth demotion classes, with no
  initial late-round exception.
- TE3 is excluded by a hard strategy constraint.
- Eligible RB/WR bench depth sorts ahead of redundant QB/TE depth without an
  unsupported upside claim.
- FLEX creates no forced early positional selection.
- No generic balanced-build or early-RB rule is introduced.
- K/DEF selection reservation activates at exact remaining-pick boundaries.
- The `R <= M` K/DEF guard becomes non-actionable and reports its evidence.
- Adjusted output retains unchanged raw rank, raw score, cost, and every
  reason; no second weighted strategy score exists.
- Input shuffling and explicit replay produce identical results.
- Output limits do not alter raw scores, adjusted ordering, or target states.
- Historical `as_of` with a current editable profile reports
  `profile_temporal_status=current_explicit_profile` and does not claim
  historical strategy reconstruction.

### M3.5A MCP and CLI tests

- `--strategy` and environment/config precedence.
- Dynamic initialization instructions name the effective profile and contain
  every fresh-session constraint.
- `get_draft_strategy` returns a stable, read-only, versioned result.
- `recommend_pick` returns both raw and adjusted recommendations.
- All tool annotations remain read-only and non-destructive.
- MCP opens one short-lived read-only transaction per attempt.
- No MCP path initializes, migrates, synchronizes, writes, or constructs a
  provider.
- The stdio server works outside the repository directory.
- Existing no-strategy MCP behavior remains compatible.
- Domain failures retain structured envelopes and set MCP `isError=true`.

### M3.5B intelligence and market tests

- ADP A -> B -> A history is preserved append-only.
- `observed_at` and `imported_at` both govern as-of visibility.
- Source, season, scoring, league-size, and draft-type compatibility is exact.
- Unresolved and ambiguous identities are retained without fuzzy acceptance.
- Missing ADP falls back to the manual window with an explicit limitation.
- Market-window refinement is deterministic and returns manual, derived, and
  effective windows.
- Market classifications never become unsupported probabilities.
- Opponent roster reconstruction handles fixed positions and FLEX.
- Snake turns and turn boundaries produce correct intervening opponent picks.
- Opponent demand is invariant to input ordering and uses documented ties.
- Decision logs are append-only and retain every snapshot/model/profile hash.
- MCP market tools share coherent draft and ADP provenance.
- Lock contention remains bounded and returns stable `database_busy` without
  dropping watcher observations.
- Unit and integration tests never access the public internet.

### Standard quality gates

```console
uv run ruff format .
uv run ruff check .
uv run mypy src
uv run pytest
```

## Acceptance criteria

### M3.5A

M3.5A is accepted when:

1. `logan-ppr-2flex-1.0` is an editable, validated, versioned profile.
2. The complete approved target and roster requirements are represented as
   configuration and generic rules.
3. Existing raw policies and their outputs remain unchanged.
4. Strategy adjustment is pure, deterministic, and occurs only after
   `trusted-board-1.1`.
5. Every adjusted result preserves the complete raw recommendation and exposes
   adjustment costs and reasons.
6. Brown, McBride, Loveland, and Kyler obey their approved semantics at every
   tested window boundary.
7. Explicit QB2/TE2 demotion classes, TE3 prohibition, RB/WR bench-depth
   preference, and the `R <= M` K/DEF guard are enforced without imposing
   roster balance or inventing upside.
8. `fwr recommend --strategy` works without synchronization and with stable
   human and JSON presentation.
9. MCP accepts `--strategy`, exposes `get_draft_strategy`, and returns
   strategy-aware `recommend_pick` output.
10. A fresh Codex session learns the active strategy from initialization
    instructions and tools, not prior conversation.
11. MCP remains read-only, local, stdio-only, and safe under watcher
    contention.
12. Warehouse historical replay and explicit-as-of determinism remain intact,
    while editable-profile provenance explicitly disclaims historical strategy
    reconstruction.

### M3.5B

M3.5B is accepted when:

1. Compatible ADP observations are imported and selected immutably as of an
   explicit cutoff.
2. Market context refines manual target windows without hiding either input.
3. Kyler and Loveland timing uses selected market evidence when present and
   explicit fallback behavior when absent.
4. Opponent positional demand is deterministic, versioned, and supported by
   visible evidence rather than claims of opponent intent.
5. No unsupported survival probability is emitted.
6. Explicit decision logs preserve raw and adjusted choices with complete
   snapshot and strategy provenance.
7. `get_market_context` and `get_opponent_demand` are coherent, read-only MCP
   tools.
8. Strategy-aware recommendation uses the same market context selected in its
   transaction.
9. Existing snapshot, recommendation, CLI, MCP, and database-contention tests
   remain green.

## Risks and backwards compatibility

- Raw policy drift is prevented by golden equality tests and a strict
  post-`trusted-board-1.1` boundary.
- Profile edits could otherwise damage reproducibility; normalized profile
  hashes and temporal-status labels are therefore mandatory provenance.
- Player names are not stable identity keys; provider IDs are preferred and
  ambiguity fails explicitly.
- Target promotion could conceal excessive value loss; configured ceilings and
  reported raw costs are mandatory.
- ADP must not be mislabeled as availability probability.
- Starter allocation must not be confused with construction requirements;
  FLEX remains optionality.
- Reserving K/DEF picks does not authorize inventing a K/DEF ranking policy.
- CLI and MCP must not diverge in as-of selection; shared connection-taking
  repository helpers should remain the single query semantics.
- Recommendation and MCP reads must not begin persisting decision logs
  implicitly.
- No-strategy automation and the existing raw model choices remain supported.

## Dependency boundary before M4A

M4A must simulate the same manager used by live recommendations. It must not
call the old raw policy directly as its complete selection policy.

Before M4A begins, M3.5 must expose a reusable pure boundary conceptually
equivalent to:

```python
evaluate_pick(
    recommendation_inputs,
    resolved_strategy_profile,
    market_context,
    opponent_demand,
) -> StrategyRecommendationResult
```

M3.5A permits absent market inputs and applies explicit manual-window fallback
semantics. M3.5B supplies immutable ADP context and deterministic opponent
demand. This M3.5 boundary is deterministic and accepts no RNG seed. M4 owns
the simulation wrapper and supplies an explicit seed to any stochastic
future-state generation around this deterministic evaluator.

M4A depends on M3.5 providing:

- the same resolved profile and profile hash used live;
- the same target state machine and window rules;
- the unchanged `trusted-board-1.1` raw result;
- the same positional diminishing returns, TE3 prohibition, and K/DEF
  completion constraints;
- the same market-window refinement;
- the same opponent-demand model, or an explicitly versioned stochastic
  extension;
- complete draft, player, ranking, projection, ADP, strategy, and model
  provenance, with M4 adding simulation-model and seed provenance; and
- replay fixtures proving live evaluation and simulation choose through the
  same policy boundary at identical states.

M4A may add future-state generation and Monte Carlo aggregation. It must not
redefine player valuation, user preferences, target timing, roster constraints,
or market interpretation. No championship-probability improvement may be
claimed without evaluation evidence.
