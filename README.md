# Agent bridge

A stdio MCP server that lets coding-agent CLIs on one machine delegate to each other. Installed once, available in every project: the project is whatever directory the calling harness was launched from.

- `ask_codex`, `ask_claude`, `ask_kiro`, `ask_gemini`, `ask_opencode`
- `check_task(task_id, wait_s?)`, `cancel_task(task_id)`
- `list_shared_skills()`

Every `ask_*` tool takes `prompt` plus optional:

| Argument | Effect |
|---|---|
| `skill` | Prepend a shared instruction file. |
| `session_id` | Continue an earlier conversation (not Kiro: the worker is stateless). |
| `mode` | `read_only` (default) or `write`. Mapped to each CLI's own sandbox/approval flags. |
| `background` | Return a `task_id` at once; collect with `check_task`, stop with `cancel_task`. |
| `worktree` | Run in `<project>/.worktrees/<task_id>` on branch `bridge/<task_id>`. Kept if the delegate changed files, removed if not. The project must be a git repo; only committed files exist in a worktree. |
| `timeout_s` | Default 600, or 1800 in background. |

Results are JSON: `status` (`ok`, `error`, `timeout`, `cancelled`, `running`), `task_id`, `session_id`, `output`, `files_changed`, `worktree`, `branch`, `duration_s`.

## Layout

| Path | Purpose |
|---|---|
| `server.py` | The server. No third-party dependencies. |
| `skills/` | Shared instruction files. A project's `.agents/<name>.md` overrides the global skill of the same name. `skills/orchestration.md` holds the delegation rules. |
| `rules.md` | The short block installed into each harness's global instructions file. |
| `kiro/worker.json` | The Kiro `worker` agent that `ask_kiro` selects. It has no bridge access. |
| `logs/calls.jsonl` | Audit log of every call, with the project root (gitignored). |
| `test_server.py` | `python test_server.py`. Uses a fake CLI; makes no model calls. |

## Install on a machine

Replace `<repo>` with the absolute path of this checkout, using forward slashes.

| Harness | MCP registration | Global rules file (paste `rules.md`) |
|---|---|---|
| Claude Code | `claude mcp add --scope user agent-bridge -- python <repo>/server.py` | `~/.claude/CLAUDE.md` |
| Codex | `codex mcp add agent-bridge -- python <repo>/server.py`, then in `~/.codex/config.toml` add `tool_timeout_sec = 1800` and `env_vars = ["AGENT_BRIDGE_DEPTH", "AGENT_BRIDGE_ORIGIN"]`. Do not set `required = true`: a bridge failure would then stop Codex from starting. | `~/.codex/AGENTS.md` |
| Kiro | `mcpServers` entry in `~/.kiro/settings/mcp.json`; copy `kiro/worker.json` to `~/.kiro/agents/` | `~/.kiro/steering/agent-bridge.md` |
| Gemini CLI | `mcpServers` entry in `~/.gemini/settings.json` | `~/.gemini/GEMINI.md` |
| OpenCode | `mcp` entry (`"type": "local"`) in `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/AGENTS.md` |

Set `GEMINI_API_KEY` as a user environment variable. Gemini CLI reads only the nearest `.env`, so a project `.env` hides `~/.gemini/.env`.

## Behaviour notes

- Calls run on their own thread, capped per harness (Codex 5, others 3). Tasks do not survive a bridge restart.
- Delegated processes inherit `AGENT_BRIDGE_DEPTH`; the default maximum depth is 2 so a peer can consult one more peer without an infinite delegation loop. Override with `AGENT_BRIDGE_MAX_DEPTH`.
- `AGENT_BRIDGE_ROOT` overrides the project directory if a harness launches MCP servers from somewhere else.
- Helper processes never inherit the server's stdin: on Windows a child that inherits a pipe with a pending read deadlocks.
