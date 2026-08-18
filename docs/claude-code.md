# Claude Code MCP setup

Fantasy War Room exposes a client-independent local stdio MCP server. Codex is the primary tested
integration, but Claude Code can connect to the same `fwr-mcp` entry point. The server remains
read-only and network-free; synchronization runs separately through the FWR CLI.

## Prerequisites

Complete the normal onboarding and confirm that the active draft is ready:

```console
uv sync
uv run fwr setup --username YOUR_SLEEPER_USERNAME
uv run fwr data refresh
uv run fwr data status --json
uv run fwr draft-ready --json
uv run fwr context --json
```

`draft-ready` must report `ready: true`. New league contexts use the zero-key
`portable-market-1.0` path and do not require user-supplied rankings or projections. From the two
JSON responses, note the resolved draft ID, draft slot, source, recommendation model, database
path, and optional strategy. Do not commit those values.

## Register the existing stdio server

From the repository root, replace every placeholder with the resolved local value:

```console
claude mcp add --scope local --transport stdio fantasy-war-room -- \
  uv run --project /ABSOLUTE/PATH/TO/fantasy-war-room fwr-mcp \
  --draft-id DRAFT_ID \
  --draft-slot DRAFT_SLOT \
  --source SOURCE \
  --model RECOMMENDATION_MODEL \
  --database /ABSOLUTE/PATH/TO/fantasy-war-room.duckdb
```

If the active context intentionally selects a strategy, append
`--strategy STRATEGY_PROFILE`. Do not add a personal strategy otherwise. The server also accepts
optional `--adp-source` and `--schedule-source` overrides; omit them to use the implemented
`local-adp` and `local-schedule` defaults.

Use local scope, as shown above. Claude Code stores it privately for this project. Project scope
would create a shared `.mcp.json`, which is inappropriate for draft IDs, local paths, and other
user-specific configuration.

Verify the connection:

```console
claude mcp get fantasy-war-room
claude mcp list
claude
```

Inside Claude Code, open `/mcp` and confirm that `fantasy-war-room` is connected. Then ask Claude
to use `recommend_pick` before giving pick advice and
`simulate_next_pick_survival` for serious alternatives. Survival results are simulated
availability rates under a named model, not calibrated probabilities or player-quality scores.

Keep live synchronization in another terminal when needed:

```console
uv run fwr watch --draft-id DRAFT_ID
```

Standalone mocks that need league scoring context must have been synchronized with the explicit
`--scoring-context-league-id` workflow described in
[the MCP guide](mcp-draft-copilot.md#configuration). The MCP server cannot attach or change that
context.

## Why setup is manual

FWR currently resolves MCP startup values through `fwr codex configure`, which writes a
Codex-specific project configuration. It does not emit or install a Claude Code registration, so
a completely portable one-command Claude setup is not currently available. The manual local
registration above invokes the same implemented server with the same resolved arguments; it does
not require a new protocol or server mode.

Re-run the registration when the selected league, draft, draft slot, ranking source, model,
strategy, or database path changes. Remove the old entry first with:

```console
claude mcp remove fantasy-war-room
```

Command syntax and scope behavior follow the
[official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
