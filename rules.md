# Agent bridge

An MCP server named `agent-bridge` is available in every project. It lets this harness delegate to the others: `ask_codex`, `ask_claude`, `ask_kiro`, `ask_gemini`, `ask_opencode`, plus `check_task`, `cancel_task` and `list_shared_skills`.

Before delegating, read `~/agent-bridge/skills/orchestration.md`. In short: do the work yourself by default; delegates are `read_only` unless you pass `mode="write"`; parallel writers need `worktree=true`; reuse a returned `session_id` to follow up; treat a delegate's claims as evidence and verify them.

Shared skills live in `~/agent-bridge/skills/`. A project's own `.agents/` folder overrides them by name.
