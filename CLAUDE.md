@AGENTS.md

# Claude Code guidance

Read [README.md](README.md) for the product overview and
[docs/mcp-draft-copilot.md](docs/mcp-draft-copilot.md) for the MCP contract.

Fantasy War Room owns authoritative draft facts and calculations. When its MCP tools are
available, use them for fantasy-football decisions instead of model memory. Call
`recommend_pick` first for the configured deterministic recommendation: portable mode is an
explicit FFC market-order baseline, while projection-backed models add valuation analytics. Use
`simulate_next_pick_survival` separately for wait cost. Describe its output as a **simulated
availability rate**, never a calibrated probability.

Do not invent or substitute draft state, player availability, rankings, projections, ADP,
injuries/news, or simulation outputs. Clearly label interpretation as inference. MCP is read-only
and does not synchronize data; stale state must be refreshed through the separate FWR CLI process.

For setup, follow the exact agent-native protocol in [AGENTS.md](AGENTS.md): drive
`uv run fwr onboard --json` until ready, use only its structured questions and next actions, then
run `uv run fwr mcp configure --client claude --json`. Do not maintain a separate Claude setup
flow or ask the user to transcribe IDs and launch arguments.
