# 60–90 second demo runbook

This demo uses one already-synchronized historical Sleeper snapshot and a fixed simulation seed.
It does not require a draft to be live. Do one dry run before recording and keep the draft ID and
timestamp private if they identify a private league.

## A. Pre-recording check

From the repository root, replace the two placeholders with a historical point where the user is
on the clock and compatible rankings, projections, and ADP were already observed:

```console
export DEMO_DRAFT_ID="YOUR_DRAFT_ID"
export DEMO_AS_OF="2026-08-01T20:00:00Z"
uv sync --locked
uv run fwr doctor --json
uv run fwr context --json
uv run fwr state-at --draft-id "$DEMO_DRAFT_ID" --at "$DEMO_AS_OF" --json
uv run fwr rankings list --json
uv run fwr projections list --json
uv run fwr adp list --json
uv run fwr draft-ready --json
uv run pytest tests/test_recommend_integration.py tests/test_survival_integration.py -q
codex mcp list
git status --short
```

`draft-ready` must report the compatible ranking and projection inputs rather than missing them.
The state command must show the intended historical pick. `codex mcp list` must show the
project-local `fantasy-war-room` server; in a fresh Codex session, `/mcp` must list both
`recommend_pick` and `simulate_next_pick_survival`. If the demo uses the active league's current
draft, generate that config with `uv run fwr codex configure --json`. A historical mock must
already be explicitly bound in the ignored `.codex/config.toml`; do not run the configure command
and accidentally replace it with the active draft. The MCP stays read-only and network-free; no
watcher is needed for this fixed replay.

For a final CLI smoke check, copy two canonical player IDs from the recommendation output and run:

```console
uv run fwr recommend --draft-id "$DEMO_DRAFT_ID" --as-of "$DEMO_AS_OF" --json
uv run fwr survival --draft-id "$DEMO_DRAFT_ID" --player-id PLAYER_ONE \
  --player-id PLAYER_TWO --as-of "$DEMO_AS_OF" --simulations 5000 --seed 42 --json
```

Do not record local config files, usernames, league IDs, database paths, or terminal history. Crop
the frame to the tool results you intend to show.

## B. Terminal layout

Have three windows ready:

1. A terminal showing the historical `state-at` result, stopped at the snapshot time and recent
   picks. Increase the font and clear unrelated history.
2. A fresh Codex session opened in this repository with the `fantasy-war-room` MCP connected.
3. Optionally, a browser tab on this README's architecture diagram. A Sleeper tab is useful only
   if it contains no private league or account information.

Keep the Codex input box prefilled with the prompt below. Use the terminal for the opening shot,
then spend most of the recording in Codex.

## C. Exact demo flow

1. **0–10 seconds — historical fact.** Show the first terminal and say: “This is a real Sleeper
   draft state captured by Fantasy War Room. Every observation is immutable, so I can replay the
   decision exactly as it looked at this timestamp.”
2. **10–25 seconds — authoritative state.** Paste the prompt in Codex. Let it call
   `recommend_pick` first. Point out the draft snapshot, turn/roster context, and provenance.
3. **25–45 seconds — deterministic quality.** Show the top recommendation and two serious
   alternatives. Say: “This ordering is FWR's deterministic quality calculation—projection,
   VORP, scarcity, roster effect, and ranking inputs—not an LLM guess.”
4. **45–65 seconds — wait cost.** Let Codex call `simulate_next_pick_survival` for those
   alternatives with 5,000 simulations, seed 42, model `adp-only-1.0`, and the same `as_of`.
   Highlight each simulated availability rate.
5. **65–80 seconds — separation of concerns.** Say: “Recommendation answers who is best now.
   Survival answers what waiting may cost. Codex interprets both, but Fantasy War Room owns the
   state and math.”
6. **80–90 seconds — architecture/provenance.** Briefly show the README diagram or the result's
   snapshot/source fields. End on the read-only MCP boundary.

If the chosen historical snapshot is not on the user's clock, select an earlier committed
snapshot during the dry run. Do not improvise with a current timestamp: using the same `as_of`
and seed is what makes the recorded result reproducible.

## D. Exact Codex demo prompt

```text
Use only the fantasy-war-room MCP tools—do not use shell commands, web search, or model memory.
Replay draft state as of DEMO_AS_OF.

First call recommend_pick and give me the best pick plus the two strongest serious alternatives.
Then call simulate_next_pick_survival for those alternatives using their canonical player IDs,
5,000 simulations, seed 42, and model adp-only-1.0 at the same as-of timestamp.

Answer in at most six short bullets. Clearly separate deterministic player quality from simulated
wait cost. Call the simulation output a “simulated availability rate,” not a calibrated or
ground-truth probability. Mention the key snapshot/source provenance and one important limitation.
Fantasy War Room owns the facts and calculations; your job is only to interpret them.
```

Replace `DEMO_AS_OF` before recording. The MCP is already bound to the configured draft ID, so the
prompt must not ask it to switch drafts.

## E. Voiceover outline

“LLMs are good at reasoning, but I didn't want the LLM inventing the state or the math. Fantasy
War Room synchronizes Sleeper into immutable, time-aware DuckDB snapshots and joins that state to
versioned rankings, projections, ADP, and schedule data. Here I'm replaying a real historical
pick. The deterministic engine recommends the best player using value, scarcity, and roster
effects. Then a seeded 5,000-run simulation tells me the wait cost for the alternatives as a
simulated availability rate. Codex can explain the tradeoff through a read-only MCP, but it cannot
change the data or become the source of the answer. FWR owns the calculations; the LLM interprets
them.”
