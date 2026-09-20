# Agent Bridge Reliability Review

Date: 2026-09-20

## Phase 1: Failure cleanup

Completed fixes:
- timeout cleanup no longer waits indefinitely on inherited pipes
- cancellation returns promptly while background cleanup completes
- task state remains observable after cancellation
- MCP delegations default to background execution; explicit foreground calls and task polling are capped at 60s
- delegate stdout/stderr are written to transcript files rather than buffered completely in RAM
- parsed transcript data, returned output, retained tasks, and completed request-thread bookkeeping are bounded
- MCP shutdown cancels both active workers and jobs queued on worker slots before joining request threads

Validation:
- `python test_server.py` -> 37 passed
- synthetic multi-megabyte output check -> bounded returned output with full transcript on disk
- live Codex delegation smoke test -> `BRIDGE_OK` with a valid session id and transcript files
- live Kiro `mode="auto"` smoke test -> shell tool executed without an approval pause and returned `KIRO_AUTO_OK`

## Phase 2: Architecture review

Reviewed patterns from OpenHands, LangGraph, AutoGen, and CrewAI.

Findings:
- keep task state separate from execution workers
- workers should be replaceable
- checkpoint state should survive worker failure
- retries should handle transient startup failures

## Phase 3: MCP integration decision

Completed MCP additions:
- explicit `mode="auto"` for Codex, Claude, Kiro, Gemini, and OpenCode using each CLI's native autonomous permission mode
- `list_auto_modes` so callers can inspect the exact native mapping

Recommended future MCP additions:
- bridge_health / bridge_doctor
- task_history
- worker_status
- benchmark_report

Avoid embedding autonomous planning loops directly inside MCP. MCP should expose reliable primitives; orchestration policy should remain in the agent layer.

## Remaining work

- add worker lifecycle supervisor
- run old vs new load benchmark
- add checkpoint persistence
- add health diagnostics

## GitHub MCP issue survey (2026-09-20)

Patterns worth defending against:

- **stdio backpressure can look like a random hang.** TypeScript SDK issue #2776 shows an unread stderr pipe filling and blocking the child; Python SDK issues #671/#1333 report similar stdio stalls. The bridge now redirects delegate stdout/stderr to files instead of leaving child pipes buffered in-process.
  - https://github.com/modelcontextprotocol/typescript-sdk/issues/2776
  - https://github.com/modelcontextprotocol/python-sdk/issues/671
  - https://github.com/modelcontextprotocol/python-sdk/issues/1333
- **child-process cleanup is a recurring leak source.** TypeScript SDK #2023/#2002 and Python SDK #2231/#3457 cover orphaned descendants, parent-death cleanup, and missing process-group/resource-limit hooks. The bridge now kills the full Windows process tree and launches POSIX delegates in their own session so the process group can be killed. Resource limits are still future work.
  - https://github.com/modelcontextprotocol/typescript-sdk/issues/2023
  - https://github.com/modelcontextprotocol/typescript-sdk/issues/2002
  - https://github.com/modelcontextprotocol/python-sdk/issues/2231
  - https://github.com/modelcontextprotocol/python-sdk/issues/3457
- **unbounded MCP results consume or exceed model context.** GitHub MCP Server #142/#608/#2122 show commit/log/PR payloads reaching tens of thousands of tokens or crashing clients. The bridge now caps parsed transcript data and returned output, reports `output_truncated`, and keeps the transcript path for inspection.
  - https://github.com/github/github-mcp-server/issues/142
  - https://github.com/github/github-mcp-server/issues/608
  - https://github.com/github/github-mcp-server/issues/2122
- **cancellation needs request-scoped lifecycle handling.** Python SDK #1458/#2610 show timeout/cancellation paths either failing to notify or killing the stdio receive loop. The bridge currently keeps cancellation isolated from the receive loop, but it does not yet map MCP `notifications/cancelled` request IDs onto delegate/check-task work.
  - https://github.com/modelcontextprotocol/python-sdk/issues/1458
  - https://github.com/modelcontextprotocol/python-sdk/issues/2610
- **protocol evolution is now material.** The bridge explicitly implements initialize-era revisions through `2025-06-18`; newer MCP SDKs support the `2026-07-28` discovery/negotiation era. Legacy fallback should remain compatible, but native `server/discover` support should be a planned upgrade so connection behavior does not depend on client fallback heuristics.
  - https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28

### Prioritized follow-up

1. Add request-ID tracking and `notifications/cancelled` propagation without allowing cancellation races to stop the stdio receive loop.
2. Add a `bridge_health`/`bridge_doctor` tool exposing task counts, transcript disk usage, worker process state, recent failures, and effective limits.
3. Add per-worker resource limits where the host OS supports them, plus a transcript disk quota/rotation policy.
4. Add explicit MCP 2026-07-28 discovery/version-negotiation support after a compatibility test matrix against current Codex, Claude, Gemini, OpenCode, and MCP Inspector clients.
5. Keep connector schemas honest about downstream output caps/truncation; `Codex Native2` should surface an explicit truncation flag instead of advertising an output budget the native tool may cap below.
