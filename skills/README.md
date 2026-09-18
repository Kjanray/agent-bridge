# Shared agent skills

These Markdown files contain harness-neutral instructions that Claude, Codex, and Kiro can all use.

The local `agent-bridge` MCP tools accept a `skill` argument. For example, `skill: "code-review"` loads `code-review.md` (from the project's `.agents/` if present, otherwise from this folder) and prepends it to the delegated task.

Keep these files focused on reusable workflows. Harness-specific startup behavior belongs in `CLAUDE.md`, `AGENTS.md`, or `.kiro/`.
