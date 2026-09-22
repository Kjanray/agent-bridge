# Agent bridge

A stdio MCP server that lets coding-agent CLIs on one machine delegate to each other. Installed once, available in every project: the project is whatever directory the calling harness was launched from.

- `ask_codex`, `ask_claude`, `ask_kiro`, `ask_gemini`, `ask_opencode`
- `check_task(task_id, wait_s?)`, `cancel_task(task_id)`
- `list_shared_skills()`, `list_auto_modes()`

Every `ask_*` tool takes `prompt` plus optional:

| Argument | Effect |
|---|---|
| `skill` | Prepend a shared instruction file. |
| `session_id` | Continue an earlier conversation (not Kiro: the worker is stateless). |
| `mode` | `read_only` (default), `write`, or `auto`. `auto` is explicit trusted autonomous execution using each CLI's native no-prompt permission mode. |
| `model` | Model to run inside that harness's own subscription. Omit for the harness default. Each `ask_*` schema lists the names known to work; the value is passed through verbatim, so a new model works without a bridge change. |
| `background` | Defaults to `true` for MCP calls: return a `task_id` at once; collect with `check_task`, stop with `cancel_task`. Codex calls are always detached; other explicit foreground calls are capped at 60s. |
| `worktree` | Run in `<project>/.worktrees/<task_id>` on branch `bridge/<task_id>`. Kept if the delegate changed files, removed if not. The project must be a git repo; only committed files exist in a worktree. |
| `timeout_s` | Worker timeout. Defaults to 1800s for MCP/background calls; explicit foreground calls are capped at 60s. |

Results are JSON: `status` (`ok`, `error`, `timeout`, `cancelled`, `running`), `task_id`, `session_id`, `output`, `output_truncated`, transcript paths/bytes, `files_changed`, `worktree`, `branch`, and `duration_s`. Large delegate output is written to transcript files instead of being buffered in the MCP process.

## Layout

| Path | Purpose |
|---|---|
| `server.py` | The server. No third-party dependencies. |
| `auto/` | Native `mode="auto"` policy registry for Codex, Claude, Kiro, Gemini, and OpenCode. |
| `skills/` | Shared instruction files. A project's `.agents/<name>.md` overrides the global skill of the same name. `skills/orchestration.md` holds the delegation rules. |
| `rules.md` | The short block installed into each harness's global instructions file. |
| `kiro/worker.json` | The bounded Kiro `worker` agent that `ask_kiro` selects. It has no bridge access. |
| `kiro/trusted-dev.json` | Optional user-global Kiro profile for interactive autonomous development with sensitive paths/destructive commands still gated. |
| `logs/calls.jsonl` | Audit log of every call, with the project root (gitignored). |
| `logs/transcripts/` | Delegate stdout/stderr transcripts. Only a bounded slice is parsed into memory. Old files are removed as completed task state is evicted. |
| `test_server.py` | `python test_server.py`. Uses a fake CLI; makes no model calls. |

## Install on a machine

Replace `<repo>` with the absolute path of this checkout, using forward slashes.

| Harness | MCP registration | Global rules file (paste `rules.md`) |
|---|---|---|
| Claude Code | `claude mcp add --scope user agent-bridge -- python <repo>/server.py` | `~/.claude/CLAUDE.md` |
| Codex | `codex mcp add agent-bridge -- python <repo>/server.py`, then in `~/.codex/config.toml` add `tool_timeout_sec = 1800` and `env_vars = ["AGENT_BRIDGE_DEPTH", "AGENT_BRIDGE_ORIGIN"]`. Do not set `required = true`: a bridge failure would then stop Codex from starting. | `~/.codex/AGENTS.md` |
| Kiro | `mcpServers` entry in `~/.kiro/settings/mcp.json`; copy `kiro/worker.json` and optionally `kiro/trusted-dev.json` to `~/.kiro/agents/` | `~/.kiro/steering/agent-bridge.md` |
| Gemini CLI | `mcpServers` entry in `~/.gemini/settings.json` | `~/.gemini/GEMINI.md` |
| OpenCode | `mcp` entry (`"type": "local"`) in `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/AGENTS.md` |

Set `GEMINI_API_KEY` as a user environment variable. Gemini CLI reads only the nearest `.env`, so a project `.env` hides `~/.gemini/.env`.

For an interactive Kiro session that should stop asking about routine development actions, use `kiro-cli chat --agent trusted-dev`. That profile lists `@agent-bridge` in both `tools` and `allowedTools`, which is what stops Kiro re-asking for permission on every `ask_*` delegation: an MCP server reached through `includeMcpJson` is loaded but not trusted, so without the entry each bridge call prompts again. The MCP bridge itself keeps using the smaller `worker` profile and applies `--trust-all-tools` only when the caller explicitly chooses `mode="auto"`.

## Behaviour notes

- MCP `ask_*` calls default to detached execution. Codex is forced into detached execution, and `check_task(wait_s=...)` is hard-capped at 60s, so one MCP request cannot outlive the ChatGPT-web transport. Tasks do not survive a bridge restart.
- `mode="auto"` is opt-in and removes interactive permission prompts using native harness controls: Codex keeps `workspace-write` but sets `approval_policy="never"`; Claude uses `--permission-mode auto --permission-prompts none`; Kiro trusts all tools exposed by its bounded `worker` profile; Gemini uses `--approval-mode yolo`; OpenCode uses `--auto`. Run `list_auto_modes()` to inspect the effective mapping.
- Codex auto mode deliberately does not use `--approve-for-me`: that path invokes Codex automatic approval review and can fail when the separate Codex allowance is exhausted even while the Native2 ChatGPT-web model still has capacity.
- Codex admits at most three outstanding jobs by default: two running and one queued. Extra jobs fail immediately instead of creating more browser tabs. Override with `AGENT_BRIDGE_CODEX_MAX_PARALLEL` (hard-capped at 4) and `AGENT_BRIDGE_CODEX_MAX_OUTSTANDING`.
- Codex task prompts are capped at 32,768 characters; put large context in a file and pass its path. Known ChatGPT browser failures open a 120-second circuit breaker, increasing to 600 seconds after another failure within ten minutes. Configure these with `AGENT_BRIDGE_CODEX_MAX_PROMPT_CHARS`, `AGENT_BRIDGE_CODEX_FAILURE_WINDOW_SECONDS`, `AGENT_BRIDGE_CODEX_COOLDOWN_SECONDS`, and `AGENT_BRIDGE_CODEX_REPEAT_COOLDOWN_SECONDS`.
- Delegated Codex calls are pinned to `chatgpt-web/high` so they stay on the Native2 browser route instead of falling through to a native Codex model. Override the default with `AGENT_BRIDGE_CODEX_MODEL`, or per call with `model=`.
- `model=` reaches each CLI's own flag (`codex -m`, `claude --model`, `kiro-cli --model`, `gemini -m`, `opencode -m provider/model`), so a delegate can be pointed at a cheaper or stronger model inside the same subscription without touching billing. The names in each schema are a snapshot; `kiro-cli chat --list-models` and `opencode models` print the live lists.
- A delegate failure that looks like a usage limit (quota exhausted, rate limit, 429, credits gone) is recorded in `logs/limits.json` with the harness, model, timestamp and the original message. The next session reads it back in the MCP `initialize` instructions, so the caller knows which harness is spent before wasting a delegation on it. The note is advisory only — nothing is blocked — and expires after an hour (`AGENT_BRIDGE_LIMIT_COOLDOWN_SECONDS`).
- Delegated Codex processes are started with nested multi-agent features and unbounded connection retries disabled. This keeps fan-out and retry behavior under the bridge's admission controls.
- Delegated processes inherit `AGENT_BRIDGE_DEPTH`; the default maximum depth is 2 so a peer can consult one more peer without an infinite delegation loop. Override with `AGENT_BRIDGE_MAX_DEPTH`.
- `AGENT_BRIDGE_ROOT` overrides the project directory if a harness launches MCP servers from somewhere else.
- Delegate stdout/stderr are redirected to disk; only up to 4 MiB is read for parsing and returned `output` is capped at 16,384 characters by default. Completed in-memory task state is capped at 128 entries. Override with `AGENT_BRIDGE_MAX_PARSE_BYTES`, `AGENT_BRIDGE_MAX_RESULT_CHARS`, and `AGENT_BRIDGE_MAX_RETAINED_TASKS`.
- Helper processes never inherit the server's stdin: on Windows a child that inherits a pipe with a pending read deadlocks.
