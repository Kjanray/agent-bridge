# Orchestration

Rules for any agent in this repo that delegates through `agent-bridge`. Keep this file short: a rule stays only if removing it would cause mistakes.

## 1. Decide whether to delegate

- Do the work yourself by default. Delegate only self-contained work that needs an independent view (review), is read-heavy (research, exploration), is long-running, or needs a capability you lack.
- Scale effort to the task. A simple task gets zero delegates. Never fan out by default. The bridge runs at most two Codex jobs with one queued and rejects excess work.
- Reads parallelise, writes do not. One writer per set of files. Parallel writers each get `mode="write", worktree=true` and non-overlapping files. Only the orchestrator merges and edits shared files.
- Architecture and final integration stay with one agent (you) or the human. Worktrees prevent file conflicts, not conflicting decisions.

## 2. Route to the right harness

| Harness | Use for | Notes |
|---|---|---|
| Codex (ChatGPT web) | long tasks, web research, attacking a plan | Billed per message: send one complete brief, never small pings. Idles after ~30 min without a prompt: for long jobs ask it to stop at checkpoints and report, then resume with `session_id` and "continue". |
| Claude | design analysis, review, implementation | |
| Kiro worker | bounded implementation | Stateless (no `session_id`). Needs `mode="write"`. |
| OpenCode | second opinion, implementation | |
| Gemini | video, audio, very large inputs (`@path/to/file`) | Free-tier quota is small: no research-sized tasks. |

Have a different harness review work than the one that wrote it.

Use `mode="auto"` only for a trusted, well-scoped task where the delegate should continue without interactive permission prompts. The bridge maps it to each CLI's native autonomous mode; `list_auto_modes()` reports the exact mapping. Keep `read_only` as the default and prefer ordinary `write` when prompts are acceptable.

Codex runs through an automated ChatGPT tab (one tab per session, all in one launcher browser), so:

- Keep its threads small. A new task is a new call; reuse `session_id` only to continue the same task. Large pasted context goes in a file, with the path in the brief.
- `browser stage timed out` means a tab is overloaded. The bridge temporarily rejects new Codex jobs after this class of failure; do not retry around the circuit breaker.
- `ChatGPT stopped responding` cannot be fixed by Codex's own retries: resend the brief once. If either error repeats, ask the human to restart the Codex Web GPT launcher.

## 3. Write the brief

The delegate has none of your history. Every prompt states:

`Goal | Decisions already made | Scope and files | Non-goals ("do not change X") | How to verify (a command) | Deliverable format`

Paste the actual error text, paths and constraints. Ask for a summary of about 300 words; large outputs go to a file and the path comes back.

## 4. Use the result

- A delegate's claim is evidence, not truth. Read the diff and run the check yourself before integrating.
- Follow up with the returned `session_id` instead of starting a fresh call.
- A `BLOCKED: <need>` reply gets an answer through `session_id`, not a restart.
- Reviews must cite `file:line`, severity and evidence. "Looks good" without evidence is a failed review. Tell reviewers to report only correctness and requirement gaps, so review does not drive over-engineering.

## 5. Stop conditions

- Two failed corrections of the same delegate: stop, rethink the brief or do it yourself.
- Collect every background task with `check_task` or end it with `cancel_task`. Leave no orphans.
- After merging or rejecting a worktree: `git worktree remove <path>` and `git branch -D bridge/<task_id>`.
- Stop and ask the human before destructive actions, on ambiguous requirements, and when agents disagree on architecture.
- The bridge enforces delegation depth 2. Do not work around it.

## 6. Safety

`read_only` is the default. Pass `mode="write"` when the task needs edits, or `mode="auto"` only when the caller has intentionally authorized autonomous execution. Auto mode must use native bounded/trusted modes; do not add dangerous sandbox-bypass flags. Every call is logged to `logs/calls.jsonl` in the bridge repo.

## Sources

Anthropic: Building effective agents; How we built our multi-agent research system; Claude Code best practices. OpenAI: A practical guide to building agents; Codex AGENTS.md and subagents docs. Cognition: Don't build multi-agents. Cemri et al.: Why do multi-agent LLM systems fail? (MAST). Practitioner reports from r/ClaudeCode, Hacker News, zen-mcp-server, claude-squad, conductor.
