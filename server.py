from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from auto import AutoController


# The bridge is installed once per machine; the project is wherever the harness launched it.
ROOT = Path(os.environ.get("AGENT_BRIDGE_ROOT") or os.getcwd()).resolve()
GLOBAL_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
LOG_PATH = Path(__file__).resolve().parent / "logs" / "calls.jsonl"
LIMITS_PATH = Path(__file__).resolve().parent / "logs" / "limits.json"
TRANSCRIPT_DIR = Path(__file__).resolve().parent / "logs" / "transcripts"
MAX_DEPTH = int(os.environ.get("AGENT_BRIDGE_MAX_DEPTH", "2"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_BRIDGE_TIMEOUT_SECONDS", "600"))
BACKGROUND_TIMEOUT_SECONDS = int(os.environ.get("AGENT_BRIDGE_BACKGROUND_TIMEOUT_SECONDS", "1800"))
CHECK_TASK_MAX_WAIT_SECONDS = min(60.0, float(os.environ.get("AGENT_BRIDGE_CHECK_MAX_WAIT_SECONDS", "60")))
MCP_FOREGROUND_MAX_SECONDS = min(60.0, float(os.environ.get("AGENT_BRIDGE_FOREGROUND_MAX_SECONDS", "60")))
MAX_PARSE_BYTES = max(65_536, int(os.environ.get("AGENT_BRIDGE_MAX_PARSE_BYTES", str(4 * 1024 * 1024))))
MAX_RESULT_CHARS = max(4_096, int(os.environ.get("AGENT_BRIDGE_MAX_RESULT_CHARS", "16384")))
MAX_RETAINED_TASKS = max(8, int(os.environ.get("AGENT_BRIDGE_MAX_RETAINED_TASKS", "128")))
CODEX_MAX_PARALLEL = max(1, min(4, int(os.environ.get("AGENT_BRIDGE_CODEX_MAX_PARALLEL", "2"))))
CODEX_MAX_OUTSTANDING = max(
    CODEX_MAX_PARALLEL,
    int(os.environ.get("AGENT_BRIDGE_CODEX_MAX_OUTSTANDING", "3")),
)
CODEX_MAX_PROMPT_CHARS = max(4_096, int(os.environ.get("AGENT_BRIDGE_CODEX_MAX_PROMPT_CHARS", "32768")))
CODEX_MODEL = os.environ.get("AGENT_BRIDGE_CODEX_MODEL", "chatgpt-web/high").strip() or "chatgpt-web/high"
CODEX_FAILURE_WINDOW_SECONDS = max(
    1.0,
    float(os.environ.get("AGENT_BRIDGE_CODEX_FAILURE_WINDOW_SECONDS", "600")),
)
CODEX_COOLDOWN_SECONDS = max(0.0, float(os.environ.get("AGENT_BRIDGE_CODEX_COOLDOWN_SECONDS", "120")))
CODEX_REPEAT_COOLDOWN_SECONDS = max(
    CODEX_COOLDOWN_SECONDS,
    float(os.environ.get("AGENT_BRIDGE_CODEX_REPEAT_COOLDOWN_SECONDS", "600")),
)
MODES = ("read_only", "write", "auto")
# ponytail: flat cooldowns, because no CLI reports its reset time in a parseable way. The original
# failure text is kept with the note, so the caller can read a real reset time when one was printed.
LIMIT_COOLDOWN_SECONDS = max(60.0, float(os.environ.get("AGENT_BRIDGE_LIMIT_COOLDOWN_SECONDS", "3600")))
RATE_LIMIT_COOLDOWN_SECONDS = max(10.0, float(os.environ.get("AGENT_BRIDGE_RATE_LIMIT_COOLDOWN_SECONDS", "120")))
PROGRESS_TAIL_BYTES = 16_384
# model/effort go into argv and, for Codex effort, into a TOML -c string: keep them to plain tokens.
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._/:@+-]{1,128}")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LOCK = threading.Lock()
_SLOTS: dict[str, threading.Semaphore] = {}
_CALLS: list[threading.Thread] = []
TASKS: dict[str, dict[str, Any]] = {}
_CODEX_HEALTH: dict[str, Any] = {
    "failures": 0,
    "last_failure": 0.0,
    "blocked_until": 0.0,
    "reason": "",
}

# Limit-shaped failures, kept tight: a false positive tells the caller a healthy harness is spent.
# Quota is checked first: a quota error often also says 429/RESOURCE_EXHAUSTED, a rate limit never
# says "quota". Quota means hours (plan or credits spent); a rate limit clears in minutes.
_QUOTA_LIMIT = re.compile(
    "|".join(
        (
            r"quota (?:exceeded|exhausted|reached)",
            r"exceeded (?:your |the )?(?:current )?quota",
            r"out of (?:credit|credits)",
            r"insufficient (?:credit|credits|funds|balance|account funds)",
            r"payment required",
            r"\b402\b",
            r"usage limit",
            r"(?:weekly|daily|monthly|message) limit (?:reached|exceeded)",
            r"limit reached",
            r"upgrade (?:your plan )?to continue",
        )
    ),
    re.IGNORECASE,
)
# "rate limit", "rate-limited", "rate_limit_error" - not the x-ratelimit-* headers OpenCode dumps into every API error.
_RATE_LIMIT = re.compile(r"(?<![-\w])rate[ _-]limit|too many requests|\b429\b|resource[_ ]exhausted", re.IGNORECASE)

_CODEX_BROWSER_FAILURE = re.compile(
    "|".join(
        re.escape(message)
        for message in (
            "browser stage timed out",
            "browser surface did not expose an operational viewport",
            "browser DOM observation did not respond",
            "browser control channel failed",
            "browser is busy with session inspection",
            "browser tab was closed",
            "connector proof did not leave a verified empty composer",
            "did not confirm that the prompt was sent",
            "did not complete the context handoff",
            "displayed an error for this response",
            "ended the turn with 'Something went wrong'",
            "stopped responding after the task started",
            "supports at most 5 simultaneous browser turns",
        )
    ),
    re.IGNORECASE,
)


def _write(message: dict[str, Any]) -> None:
    with _LOCK:
        try:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # The MCP client may disappear while a detached worker is finishing.
            return


def _error(request_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _depth() -> int:
    try:
        return int(os.environ.get("AGENT_BRIDGE_DEPTH", "0"))
    except ValueError:
        return 0


def _child_env(target: str) -> dict[str, str]:
    env = os.environ.copy()
    env["AGENT_BRIDGE_DEPTH"] = str(_depth() + 1)
    env["AGENT_BRIDGE_ORIGIN"] = target
    env["PYTHONUTF8"] = "1"
    return env


def _skill_dirs() -> list[Path]:
    # A project's .agents/ overrides a global skill of the same name.
    return [ROOT / ".agents", GLOBAL_SKILLS_DIR]


def _load_skill(name: str | None) -> str:
    if not name:
        return ""

    candidate = name.strip().replace("\\", "/")
    if candidate.endswith(".md"):
        candidate = candidate[:-3]
    for skills_dir in _skill_dirs():
        skill_path = (skills_dir / f"{candidate}.md").resolve()
        if skills_dir.resolve() not in skill_path.parents:
            raise ValueError("skill must resolve inside a skills directory")
        if skill_path.is_file():
            return skill_path.read_text(encoding="utf-8")
    raise ValueError(f"shared skill not found: {name}")


def _compose_prompt(prompt: str, skill: str | None, target: str, timeout_s: float | None = None) -> str:
    shared = _load_skill(skill)
    sections = [
        "You are receiving a delegated task from another local agent harness.",
        f"Target harness: {target}.",
        "Work in the current directory and return a concise result to the caller.",
        "Do not delegate this task to another harness unless the task explicitly asks for cross-agent consultation.",
        "If you are blocked or the task is ambiguous, reply 'BLOCKED: <what you need>' instead of guessing.",
    ]
    if timeout_s:
        sections.append(
            f"You will be stopped after about {max(1, int(timeout_s // 60))} minutes. If the work will not fit, stop "
            "early and report what is done, what is left, and the next step: being cut off loses your summary."
        )
    if shared:
        sections.extend(["", "Shared skill instructions:", shared])
    sections.extend(["", "Delegated task:", prompt])
    return "\n".join(sections)


def _json_lines(raw: str) -> list[dict[str, Any]]:
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _parse_text(raw: str) -> tuple[str, str | None]:
    return raw.strip(), None


def _parse_json(raw: str, text_key: str) -> tuple[str, str | None]:
    try:
        data = json.loads(raw[raw.index("{") :])
        return str(data[text_key]).strip(), data.get("session_id")
    except (ValueError, KeyError, TypeError):
        return _parse_text(raw)


def _claude_result(raw: str) -> dict[str, Any] | None:
    # stream-json ends with a "result" event carrying the fields --output-format json used to print.
    return next((e for e in reversed(_json_lines(raw)) if e.get("type") == "result" or "result" in e), None)


def _parse_claude(raw: str) -> tuple[str, str | None]:
    result = _claude_result(raw)
    if not result:
        return _parse_text(raw)
    text = result.get("result")
    if text is None:  # error results (e.g. error_max_budget_usd) carry "errors" instead of "result"
        text = "; ".join(map(str, result.get("errors") or [])) or str(result.get("subtype") or "")
    return str(text).strip(), result.get("session_id")


def _parse_gemini(raw: str) -> tuple[str, str | None]:
    return _parse_json(raw, "response")


def _parse_codex(raw: str) -> tuple[str, str | None]:
    events = _json_lines(raw)
    session = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
    messages = [e["item"].get("text", "") for e in events if (e.get("item") or {}).get("type") == "agent_message"]
    return (messages[-1].strip(), session) if messages else _parse_text(raw)


def _parse_opencode(raw: str) -> tuple[str, str | None]:
    events = _json_lines(raw)
    session = next((e["sessionID"] for e in events if e.get("sessionID")), None)
    text = "".join(e["part"].get("text", "") for e in events if e.get("type") == "text" and "part" in e)
    return (text.strip(), session) if text else _parse_text(raw)


# Token/cost accounting, normalised across CLIs: input excludes cache reads, output includes reasoning.
def _usage(input_tokens: int = 0, cached: int = 0, output: int = 0, cost: float | None = None, models=()) -> dict[str, Any]:
    return {
        "input_tokens": int(input_tokens),
        "cached_tokens": int(cached),
        "output_tokens": int(output),
        "cost_usd": round(cost, 6) if cost is not None else None,
        "models": sorted(models),
    }


def _usage_claude(raw: str) -> dict[str, Any] | None:
    # modelUsage, not usage: the top-level usage block reads zero on error results.
    per_model = (_claude_result(raw) or {}).get("modelUsage")
    if not isinstance(per_model, dict) or not per_model:
        return None
    rows = [m for m in per_model.values() if isinstance(m, dict)]
    return _usage(
        sum(m.get("inputTokens", 0) + m.get("cacheCreationInputTokens", 0) for m in rows),
        sum(m.get("cacheReadInputTokens", 0) for m in rows),
        sum(m.get("outputTokens", 0) for m in rows),
        sum(m.get("costUSD", 0.0) for m in rows),
        per_model,
    )


def _usage_codex(raw: str) -> dict[str, Any] | None:
    turns = [e["usage"] for e in _json_lines(raw) if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)]
    if not turns:
        return None
    cached = sum(u.get("cached_input_tokens", 0) for u in turns)
    return _usage(sum(u.get("input_tokens", 0) for u in turns) - cached, cached, sum(u.get("output_tokens", 0) for u in turns))


def _usage_opencode(raw: str) -> dict[str, Any] | None:
    steps = [e["part"] for e in _json_lines(raw) if e.get("type") == "step_finish" and isinstance(e.get("part"), dict)]
    tokens = [s.get("tokens") or {} for s in steps]
    if not tokens:
        return None
    return _usage(
        sum(t.get("input", 0) for t in tokens),
        sum((t.get("cache") or {}).get("read", 0) for t in tokens),
        sum(t.get("output", 0) + t.get("reasoning", 0) for t in tokens),
        sum(float(s.get("cost") or 0) for s in steps),
    )


def _usage_gemini(raw: str) -> dict[str, Any] | None:
    try:
        models = json.loads(raw[raw.index("{") :])["stats"]["models"]
    except (ValueError, KeyError, TypeError):
        return None
    tokens = [m.get("tokens") or {} for m in models.values()]
    return _usage(
        sum(t.get("input", 0) for t in tokens),
        sum(t.get("cached", 0) for t in tokens),
        sum(t.get("candidates", 0) + t.get("thoughts", 0) for t in tokens),
        models=models,
    )


def _progress_line(event: dict[str, Any]) -> str | None:
    """One short line per meaningful event from Codex, OpenCode or Claude stream-json; None for bookkeeping."""
    item = event.get("item") or event.get("part") or {}
    kind = item.get("type") if isinstance(item, dict) else None
    if kind in ("agent_message", "text"):
        return "said: " + str(item.get("text", ""))
    if kind == "command_execution":
        return f"ran: {item.get('command', '')} [{item.get('status', '')}]"
    if kind == "tool":  # OpenCode
        state = item.get("state") or {}
        args = state.get("input") or {}
        detail = state.get("title") or args.get("command") or args.get("filePath") or ""
        return f"{item.get('tool')}: {detail} [{state.get('status', '')}]"
    if event.get("type") == "assistant":  # Claude
        parts = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                args = block.get("input") or {}
                parts.append(f"{block.get('name')}: {args.get('description') or args.get('command') or args.get('file_path') or ''}")
            elif block.get("type") == "text":
                parts.append("said: " + str(block.get("text", "")))
        return " | ".join(parts) or None
    return None


def _progress(task: dict[str, Any], lines: int = 8) -> dict[str, Any]:
    """What a running (or timed-out) delegate did most recently, read from its live transcript."""
    stdout_path = Path(task.get("transcript") or "")
    if not stdout_path.is_file():
        return {"note": "not started yet: waiting for a worker slot"}
    recent: list[str] = []
    # Kiro's text mode prints its tool activity on stderr, so fall back to it when stdout says nothing.
    for path in (stdout_path, Path(task.get("stderr_transcript") or "")):
        if not path.is_file() or not path.stat().st_size:
            continue
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - PROGRESS_TAIL_BYTES))
            raw_lines = _ANSI.sub("", handle.read().decode("utf-8", "replace")).splitlines()
        if size > PROGRESS_TAIL_BYTES:
            raw_lines = raw_lines[1:]  # the first line was cut by the seek
        for raw in raw_lines:
            try:
                event = json.loads(raw)
            except ValueError:
                event = None
            line = _progress_line(event) if isinstance(event, dict) else raw.strip()
            if line:
                recent.append(line if len(line) <= 200 else line[:197] + "...")
        if recent:
            break
    stderr_path = Path(task.get("stderr_transcript") or "")
    last_write = max(p.stat().st_mtime for p in (stdout_path, stderr_path) if p.is_file())
    idle = time.time() - last_write
    progress: dict[str, Any] = {"recent": recent[-lines:], "idle_s": round(idle)}
    if not recent:
        progress["note"] = (
            "Gemini prints nothing until it finishes" if task.get("target") == "gemini" else "no output yet"
        )
    return progress


# Builders take the task dict (mode, session_id, model, effort, max_budget_usd) and return argv.
def _codex(prompt: str, opts: dict[str, Any]) -> list[str]:
    mode, session_id, effort = opts["mode"], opts.get("session_id"), opts.get("effort")
    sandbox = "read-only" if mode == "read_only" else "workspace-write"
    resume = ["resume"] if session_id else []
    return [
        "codex",
        "exec",
        *resume,
        "-m",
        opts.get("model") or CODEX_MODEL,
        "-c",
        f'sandbox_mode="{sandbox}"',
        *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.multi_agent_v2=false",
        "-c",
        "features.unbounded_connection_retries=false",
        *(AutoController.args("codex") if mode == "auto" else []),
        "--json",
        *([session_id] if session_id else []),
        "-",
    ]


def _claude(prompt: str, opts: dict[str, Any]) -> list[str]:
    mode, session_id, model, effort, budget = (
        opts["mode"], opts.get("session_id"), opts.get("model"), opts.get("effort"), opts.get("max_budget_usd")
    )
    if mode == "auto":
        perms = AutoController.args("claude")
    elif mode == "write":
        perms = ["--permission-mode", "acceptEdits"]
    else:
        perms = ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    return [
        "claude",
        "-p",
        # stream-json (which -p requires --verbose for) writes events as they happen, so check_task
        # can show progress; the final "result" event is what --output-format json used to print.
        "--output-format",
        "stream-json",
        "--verbose",
        *perms,
        *(["--model", model] if model else []),
        *(["--effort", effort] if effort else []),
        *(["--max-budget-usd", f"{float(budget):g}"] if budget else []),
        *(["--resume", session_id] if session_id else []),
    ]


def _kiro(prompt: str, opts: dict[str, Any]) -> list[str]:
    mode, model, effort = opts["mode"], opts.get("model"), opts.get("effort")
    if opts.get("session_id"):
        raise ValueError("the Kiro worker is stateless: session_id is not supported")
    tools = "fs_read,fs_write,execute_bash" if mode != "read_only" else "fs_read"
    trust = AutoController.args("kiro") if mode == "auto" else [f"--trust-tools={tools}"]
    return [
        "kiro-cli",
        "chat",
        "--no-interactive",
        "--output-format",
        "text",
        "--wrap",
        "never",
        "--agent",
        "worker",
        *(["--model", model] if model else []),
        *(["--effort", effort] if effort else []),
        *trust,
        prompt,
    ]


def _gemini(prompt: str, opts: dict[str, Any]) -> list[str]:
    mode, session_id, model = opts["mode"], opts.get("session_id"), opts.get("model")
    if opts.get("effort"):
        raise ValueError("Gemini CLI has no effort control: pick a lighter or heavier model= instead")
    approval = (
        AutoController.args("gemini")
        if mode == "auto"
        else ["--approval-mode", "auto_edit" if mode == "write" else "plan"]
    )
    return [
        "gemini",
        "-o",
        "json",
        *approval,
        *(["-m", model] if model else []),
        *(["-r", session_id] if session_id else []),
    ]


def _opencode(prompt: str, opts: dict[str, Any]) -> list[str]:
    mode, session_id, model, effort = opts["mode"], opts.get("session_id"), opts.get("model"), opts.get("effort")
    agent = "plan" if mode == "read_only" else "build"
    auto = AutoController.args("opencode") if mode == "auto" else []
    return [
        "opencode",
        "run",
        "--format",
        "json",
        "--agent",
        agent,
        *auto,
        *(["-m", model] if model else []),
        *(["--variant", effort] if effort else []),
        *(["-s", session_id] if session_id else []),
    ]


@dataclass
class Target:
    label: str
    build: Callable[[str, dict[str, Any]], list[str]]
    parse: Callable[[str], tuple[str, str | None]]
    # gemini and opencode resolve to .CMD shims on Windows, which mangle multi-line args: pipe the prompt.
    stdin: bool = True
    max_parallel: int = 3
    description: str = ""
    # Free-text passthrough to the CLI's model/effort flags; the hints list values known to work on
    # 2026-09-22 and go stale when a harness adds models. Refresh from the CLI's own list command.
    models: str = ""
    efforts: str = ""  # empty: the CLI has no effort control, so the schema does not offer it
    usage: Callable[[str], dict[str, Any] | None] = lambda raw: None


TARGETS: dict[str, Target] = {
    "codex": Target(
        "Codex", _codex, _parse_codex,
        description=(
            "Delegate a focused task to Codex CLI. Codex calls are always detached, nested Codex agents are disabled, "
            "and browser capacity is enforced by the bridge."
        ),
        models=(
            f"default {CODEX_MODEL}. ChatGPT-web routes (subscription, no API billing): chatgpt-web/light, "
            "chatgpt-web/high, chatgpt-web/extra-high. Direct models: gpt-5.6-sol, gpt-5.6-luna, gpt-5.6-terra, "
            "gpt-6-astra. Leave unset to keep the web route."
        ),
        efforts=(
            "minimal, low, medium, high, xhigh. A chatgpt-web/* route already names its effort "
            "(light/high/extra-high): change the route instead."
        ),
        usage=_usage_codex,
    ),
    "claude": Target(
        "Claude Code", _claude, _parse_claude,
        description="Delegate a focused task to Claude Code. Use for implementation, review, or a second opinion.",
        models="opus, sonnet, haiku, or a full id such as claude-opus-5 / claude-sonnet-5 / claude-haiku-4-5.",
        efforts="low, medium, high, xhigh, max.",
        usage=_usage_claude,
    ),
    "kiro": Target(
        "Kiro worker", _kiro, _parse_text, stdin=False,
        description="Delegate a bounded implementation task to the local Kiro worker (pass mode='write'). Stateless: no session_id. Kiro has no bridge tools.",
        models=(
            "auto (default, cheapest), claude-opus-5, claude-sonnet-5, claude-haiku-4.5, gpt-5.6-sol, "
            "gpt-5.6-terra, gpt-5.6-luna, glm-5, minimax-m2.5, deepseek-3.2, qwen3-coder-next. "
            "Credit multipliers differ per model; `kiro-cli chat --list-models` prints the current table."
        ),
        efforts="low, medium, high, xhigh, max.",
    ),
    "gemini": Target(
        "Gemini CLI", _gemini, _parse_gemini,
        description="Delegate to Gemini CLI. Use for video/audio or very large inputs; reference files as @path/to/file in the prompt.",
        models="gemini-3-pro, gemini-3-flash, or another id the installed CLI accepts.",
        usage=_usage_gemini,
    ),
    "opencode": Target(
        "OpenCode", _opencode, _parse_opencode,
        description="Delegate a focused task to OpenCode. Use for implementation, review, or a second opinion.",
        models=(
            "provider/model. Free (cost 0, own limits): opencode/big-pickle, opencode/mimo-v2.6-flash-free, "
            "opencode/nemotron-3.5-lightning-free, opencode/nemotron-3-ultra-free, opencode/ling-3.0-flash-fin-free. "
            "Paid (need account funds): opencode/claude-opus-5, opencode/claude-sonnet-5, opencode/gemini-3.1-pro, "
            "opencode/deepseek-v4-pro. `opencode models` prints every id the current auth exposes."
        ),
        efforts="Provider-specific variant, e.g. minimal, high, max.",
        usage=_usage_opencode,
    ),
}
TOOL_TARGETS: dict[str, str] = {f"ask_{name}": name for name in TARGETS}


def _git(*args: str, cwd: Path | None = None) -> str:
    # stdin=DEVNULL: a child that inherits the MCP stdin pipe deadlocks on Windows while the main thread reads it.
    return subprocess.run(
        ["git", *args],
        cwd=cwd or ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def _add_worktree(task: dict[str, Any]) -> Path:
    path = ROOT / ".worktrees" / task["task_id"]
    task["branch"] = f"bridge/{task['task_id']}"
    task["_base"] = _git("rev-parse", "HEAD").strip()
    # Hide .worktrees/ from the project's git status without touching its tracked .gitignore.
    exclude = Path(_git("rev-parse", "--git-common-dir").strip())
    exclude = (exclude if exclude.is_absolute() else ROOT / exclude) / "info" / "exclude"
    if ".worktrees/" not in (exclude.read_text(encoding="utf-8") if exclude.is_file() else ""):
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n.worktrees/\n")
    _git("worktree", "add", "-q", "-b", task["branch"], str(path))
    task["worktree"] = str(path)
    return path


def _finish_worktree(task: dict[str, Any]) -> None:
    path = Path(task["worktree"])
    uncommitted = [line[3:] for line in _git("status", "--porcelain", "--untracked-files=all", cwd=path).splitlines()]
    committed = _git("diff", "--name-only", task["_base"], task["branch"]).splitlines()
    task["files_changed"] = sorted(set(uncommitted) | set(committed))
    if not task["files_changed"]:
        _git("worktree", "remove", "--force", str(path))
        _git("branch", "-D", task["branch"])
        task["worktree"] = task["branch"] = None


def _kill(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        # The CLIs are shims that spawn the real agent: kill the whole tree.
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _log(task: dict[str, Any], prompt: str) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(ROOT),
        "depth": _depth(),
        **{k: v for k, v in _public(task).items() if k != "output"},
        "prompt": prompt[:500],
        "output_tail": task["output"][-500:],
    }
    with _LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _public(task: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in task.items() if not key.startswith("_")}


def _read_bounded(path: Path) -> tuple[str, bool, int]:
    size = path.stat().st_size if path.exists() else 0
    if not size:
        return "", False, 0
    with path.open("rb") as handle:
        if size <= MAX_PARSE_BYTES:
            data = handle.read()
            return data.decode("utf-8", "replace"), False, size
        head_size = min(65_536, MAX_PARSE_BYTES // 4)
        tail_size = MAX_PARSE_BYTES - head_size
        head = handle.read(head_size)
        handle.seek(max(0, size - tail_size))
        tail = handle.read(tail_size)
    marker = b"\n[... transcript truncated for parsing ...]\n"
    return (head + marker + tail).decode("utf-8", "replace"), True, size


def _trim_result(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_RESULT_CHARS:
        return text, False
    head = min(8_192, MAX_RESULT_CHARS // 4)
    marker = "\n[... result truncated; full delegate transcript is on disk ...]\n"
    tail = max(0, MAX_RESULT_CHARS - head - len(marker))
    return text[:head] + marker + text[-tail:], True


def _effective_model(target: str, model: str | None) -> str:
    """The model a call really runs on: an unset Codex model means the pinned web route, not 'default'."""
    return model or (CODEX_MODEL if target == "codex" else "default")


def _is_codex_web(target: str, model: str | None) -> bool:
    # Only chatgpt-web/* routes drive the ChatGPT browser tab. Native Codex models (gpt-5.6-sol...)
    # have their own limits and never touch the tab, so the browser guards below do not apply to them.
    return target == "codex" and _effective_model(target, model).startswith("chatgpt-web/")


def _codex_admission_error_locked(prompt: str) -> str | None:
    """Browser-tab guards for chatgpt-web/* Codex calls. Caller must hold _LOCK."""
    if len(prompt) > CODEX_MAX_PROMPT_CHARS:
        return (
            f"Codex prompt is {len(prompt):,} characters; the bridge limit is "
            f"{CODEX_MAX_PROMPT_CHARS:,}. Put large context in a file and reference its path."
        )

    now = time.monotonic()
    blocked_until = float(_CODEX_HEALTH["blocked_until"])
    if blocked_until > now:
        remaining = max(1, int(blocked_until - now + 0.999))
        reason = _CODEX_HEALTH["reason"] or "browser transport failure"
        return f"Codex browser circuit is open for about {remaining}s after: {reason}"
    if blocked_until:
        _CODEX_HEALTH["blocked_until"] = 0.0
        _CODEX_HEALTH["reason"] = ""

    outstanding = sum(
        1
        for task in TASKS.values()
        if _is_codex_web(str(task.get("target")), task.get("model")) and task.get("status") == "running"
    )
    if outstanding >= CODEX_MAX_OUTSTANDING:
        return (
            f"Codex capacity is full ({outstanding}/{CODEX_MAX_OUTSTANDING} running or queued); "
            "collect or cancel an existing task before submitting another."
        )
    return None


def _record_codex_health(task: dict[str, Any]) -> None:
    if not _is_codex_web(str(task.get("target")), task.get("model")) or task.get("status") not in ("error", "timeout"):
        return
    match = _CODEX_BROWSER_FAILURE.search(str(task.get("output", "")))
    if not match:
        return

    now = time.monotonic()
    with _LOCK:
        if now - float(_CODEX_HEALTH["last_failure"]) <= CODEX_FAILURE_WINDOW_SECONDS:
            _CODEX_HEALTH["failures"] = int(_CODEX_HEALTH["failures"]) + 1
        else:
            _CODEX_HEALTH["failures"] = 1
        _CODEX_HEALTH["last_failure"] = now
        cooldown = CODEX_REPEAT_COOLDOWN_SECONDS if _CODEX_HEALTH["failures"] >= 2 else CODEX_COOLDOWN_SECONDS
        _CODEX_HEALTH["blocked_until"] = max(float(_CODEX_HEALTH["blocked_until"]), now + cooldown)
        _CODEX_HEALTH["reason"] = match.group(0)


# A usage limit outlives the bridge process (a new MCP session starts a new server), so it is kept on
# disk and reported in the initialize instructions: the caller learns which harness is spent before it
# wastes a delegation on it. Advisory only - the bridge never blocks a call on this.
# Keyed per harness *and* model: every model has its own limits (a paid OpenCode model can be out of
# funds while its free ones work; Codex web routes and native Codex models are metered separately).
def _limit_key(target: str, model: str | None) -> str:
    return f"{target}|{_effective_model(target, model)}"


def _load_limits() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(LIMITS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    limits = {}
    for key, entry in data.items():
        if not isinstance(entry, dict) or float(entry.get("until") or 0) <= now:
            continue
        if "|" not in key:  # written before per-model keys: rebuild from the model it recorded
            model = entry.get("model")
            key = _limit_key(key, None if model in (None, "default") else model)
        if key.split("|", 1)[0] in TARGETS:
            limits[key] = entry
    return limits


_LIMITS: dict[str, dict[str, Any]] = _load_limits()
_OUT_OF_FUNDS = re.compile(
    r"insufficient (?:funds|balance|account funds|credit|credits)|out of (?:credit|credits)|payment required|\b402\b",
    re.IGNORECASE,
)


def _record_limit(task: dict[str, Any]) -> None:
    # Errors only: a timeout's output is the delegate's own recent activity, which may mention quotas.
    if task.get("status") != "error":
        return
    output = str(task.get("output", ""))
    match = _QUOTA_LIMIT.search(output)
    kind, cooldown = "quota exhausted", LIMIT_COOLDOWN_SECONDS
    if match and _OUT_OF_FUNDS.search(output):
        kind = "out of funds"
    if not match:
        match = _RATE_LIMIT.search(output)
        kind, cooldown = "rate limited", RATE_LIMIT_COOLDOWN_SECONDS
    if not match:
        return
    key = _limit_key(task["target"], task.get("model"))
    with _LOCK:
        until = time.time() + cooldown
        previous = _LIMITS.get(key)
        if previous and float(previous.get("until") or 0) > until:
            return  # a short rate limit never shortens a quota note that is still running
        _LIMITS[key] = {
            "until": until,
            "kind": kind,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": _effective_model(task["target"], task.get("model")),
            # The text around the match: CLI errors often end in response headers, not the message.
            "reason": output[max(0, match.start() - 150) : match.end() + 150].strip(),
        }
        try:
            LIMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
            LIMITS_PATH.write_text(json.dumps(_LIMITS, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # A lost note is not worth failing the delegation that produced it.


def _limits_note() -> str:
    now = time.time()
    active = {key: entry for key, entry in _LIMITS.items() if float(entry.get("until") or 0) > now}
    if not active:
        return ""
    parts = []
    for key, entry in sorted(active.items()):
        target, model = key.split("|", 1)
        kind = entry.get("kind", "limited")
        scope = " (other paid models on that account likely too; free models unaffected)" if kind == "out of funds" else ""
        parts.append(
            f"{TARGETS[target].label} model {model}: {kind} at {entry['at']}{scope}; "
            f"assume unusable for about {max(1, round((float(entry['until']) - now) / 60))} more minutes"
        )
    return (
        " USAGE LIMITS REPORTED RECENTLY: " + "; ".join(parts) + ". "
        "Limits are per model: another model on the same harness may still work, so switch model= "
        "or harness rather than retrying the spent one. "
        "This is a heuristic read of the last failure, not a quota query."
    )


def _missing_clis_note() -> str:
    missing = []
    for target in TARGETS.values():
        try:
            binary = target.build("", {"mode": "read_only"})[0]
        except Exception:
            continue
        if not shutil.which(binary):
            missing.append(f"{target.label} ({binary})")
    return f" NOT INSTALLED, do not delegate to: {', '.join(missing)}." if missing else ""


def _evict_finished_tasks() -> None:
    with _LOCK:
        if len(TASKS) < MAX_RETAINED_TASKS:
            return
        for task_id in list(TASKS):
            if len(TASKS) < MAX_RETAINED_TASKS:
                break
            task = TASKS[task_id]
            thread = task.get("_thread")
            if thread and not thread.is_alive():
                evicted = TASKS.pop(task_id, None)
                for key in ("transcript", "stderr_transcript"):
                    path = Path(evicted.get(key) or "") if evicted else None
                    if path and path.is_file():
                        path.unlink(missing_ok=True)


def _execute(task: dict[str, Any], prompt: str, skill: str | None, worktree: bool, timeout_s: float) -> None:
    if _depth() >= MAX_DEPTH:
        raise RuntimeError(
            f"delegation depth limit reached ({_depth()}/{MAX_DEPTH}); return the current result instead of delegating again"
        )
    if task["mode"] not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if task["mode"] == "auto":
        AutoController.spec(task["target"])

    target = TARGETS[task["target"]]
    if task.get("max_budget_usd") and task["target"] != "claude":
        raise ValueError("max_budget_usd is only enforced by Claude Code (--max-budget-usd); other CLIs have no cap flag")
    full_prompt = _compose_prompt(prompt, skill, target.label, timeout_s)
    command = target.build(full_prompt, task)
    executable = shutil.which(command[0])
    if not executable:
        raise RuntimeError(f"{command[0]} is not installed or not on PATH")
    command[0] = executable

    with _LOCK:
        # The ChatGPT tab gets its own small pool; native Codex models use the ordinary per-target one.
        web = _is_codex_web(task["target"], task.get("model"))
        slot = _SLOTS.setdefault(
            "codex:web" if web else task["target"],
            threading.Semaphore(CODEX_MAX_PARALLEL if web else target.max_parallel),
        )
    with slot:
        if task["_cancelled"]:
            return
        cwd = _add_worktree(task) if worktree else ROOT
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        stdout_path = TRANSCRIPT_DIR / f"{task['task_id']}.stdout.log"
        stderr_path = TRANSCRIPT_DIR / f"{task['task_id']}.stderr.log"
        task["transcript"] = str(stdout_path)
        task["stderr_transcript"] = str(stderr_path)
        child_env = _child_env(target.label)
        if task["target"] == "gemini" and task["mode"] == "auto":
            child_env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.PIPE if target.stdin else subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
            )
            task["_proc"] = proc
            if task["_cancelled"]:
                _kill(proc)
            try:
                proc.communicate(full_prompt if target.stdin else None, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill(proc)
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                # Keep what the delegate got through: the recent activity is often enough to resume from.
                recent = _progress(task).get("recent") or []
                task["status"] = "timeout"
                task["output"] = f"{target.label} timed out after {timeout_s:g}s." + (
                    " Last activity:\n" + "\n".join(recent) if recent else " It produced no output."
                )
                stdout, _, _ = _read_bounded(stdout_path)
                task["usage"] = target.usage(_ANSI.sub("", stdout))
                return

    if task["_cancelled"]:
        return
    stdout, stdout_truncated, stdout_bytes = _read_bounded(stdout_path)
    stderr, stderr_truncated, stderr_bytes = _read_bounded(stderr_path)
    task["transcript_bytes"] = stdout_bytes + stderr_bytes
    clean_stdout = _ANSI.sub("", stdout)
    output, session = target.parse(clean_stdout)
    task["session_id"] = session or task["session_id"]
    task["usage"] = target.usage(clean_stdout)  # before the exit check: failed runs cost tokens too
    if proc.returncode != 0:
        detail = output or _ANSI.sub("", stderr).strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"{target.label} failed: {detail[-6000:]}")
    task["status"] = "ok"
    result = output or _ANSI.sub("", stderr).strip() or f"{target.label} completed without textual output"
    task["output"], result_truncated = _trim_result(result)
    task["output_truncated"] = stdout_truncated or stderr_truncated or result_truncated


def _work(task: dict[str, Any], prompt: str, skill: str | None, worktree: bool, timeout_s: float) -> None:
    started = time.monotonic()
    try:
        _execute(task, prompt, skill, worktree, timeout_s)
    except Exception as exc:  # Delegation errors are returned as results.
        task["status"] = "error"
        task["output"] = str(exc)
    if task["_cancelled"]:
        task["status"] = "cancelled"
        task["output"] = "cancelled by caller"
    try:
        if task["worktree"]:
            _finish_worktree(task)
    except Exception as exc:
        task["output"] += f"\n(worktree inspection failed: {exc})"
    if not task["transcript_bytes"]:
        task["transcript_bytes"] = sum(
            Path(task.get(key) or "").stat().st_size
            for key in ("transcript", "stderr_transcript")
            if task.get(key) and Path(task[key]).is_file()
        )
    task["duration_s"] = round(time.monotonic() - started, 1)
    _record_codex_health(task)
    _record_limit(task)
    _log(task, prompt)


def delegate(
    target: str,
    prompt: str,
    skill: str | None = None,
    session_id: str | None = None,
    mode: str = "read_only",
    background: bool = False,
    worktree: bool = False,
    timeout_s: float | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_budget_usd: float | None = None,
) -> dict[str, Any]:
    if target == "codex":
        background = True
    for name, value in (("model", model), ("effort", effort)):
        if value and not _SAFE_TOKEN.fullmatch(value):
            raise ValueError(f"{name} must be a plain identifier such as 'high' or 'provider/model-1.5', got {value!r}")
    if max_budget_usd is not None and float(max_budget_usd) <= 0:
        raise ValueError("max_budget_usd must be positive")
    task: dict[str, Any] = {
        "task_id": uuid.uuid4().hex[:8],
        "target": target,
        "status": "running",
        "mode": mode,
        "model": model or None,
        "effort": effort or None,
        "max_budget_usd": max_budget_usd,
        "usage": None,
        "session_id": session_id,
        "worktree": None,
        "branch": None,
        "files_changed": None,
        "duration_s": 0.0,
        "output": "",
        "output_truncated": False,
        "transcript": None,
        "stderr_transcript": None,
        "transcript_bytes": 0,
        "_cancelled": False,
        "_proc": None,
    }
    timeout = float(timeout_s or (BACKGROUND_TIMEOUT_SECONDS if background else DEFAULT_TIMEOUT_SECONDS))
    task["_thread"] = threading.Thread(target=_work, args=(task, prompt, skill, worktree, timeout), daemon=True)
    _evict_finished_tasks()
    with _LOCK:
        if _is_codex_web(target, model):
            rejection = _codex_admission_error_locked(prompt)
            if rejection:
                raise RuntimeError(rejection)
        TASKS[task["task_id"]] = task
    task["_thread"].start()
    if not background:
        task["_thread"].join()
    return _public(task)


def _task_from_log(task_id: str) -> dict[str, Any] | None:
    """A finished task from an earlier bridge process, rebuilt from the audit log and its transcript."""
    try:
        # ponytail: reads the whole audit log; fine at thousands of calls, rotate the log if it grows past that.
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    entry = None
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("task_id") == task_id:
            entry = candidate
            break
    if not entry:
        return None
    result = {k: v for k, v in entry.items() if k not in ("prompt", "output_tail", "root", "depth")}
    output = entry.get("output_tail", "")
    target = TARGETS.get(str(entry.get("target")))
    transcript = Path(entry.get("transcript") or "")
    if target and entry.get("status") == "ok" and transcript.is_file():
        stdout, _, _ = _read_bounded(transcript)
        output = target.parse(_ANSI.sub("", stdout))[0] or output
    result["output"], result["output_truncated"] = _trim_result(output)
    result["recovered_from"] = "audit log: this task finished under an earlier bridge process"
    return result


def check_task(task_id: str, wait_s: float = 0) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return _task_from_log(task_id) or {
            "task_id": task_id,
            "status": "error",
            "output": "unknown task_id: not running here and not in the audit log",
        }
    if task.get("_cancelled"):
        return _public(task)
    task["_thread"].join(timeout=max(0.0, min(float(wait_s), CHECK_TASK_MAX_WAIT_SECONDS)))
    result = _public(task)
    if task["_thread"].is_alive():
        result["progress"] = _progress(task)
    return result


def cancel_task(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if task and task["_thread"].is_alive():
        task["_cancelled"] = True
        task["status"] = "cancelled"
        task["output"] = "cancelled by caller"
        if task["_proc"]:
            _kill(task["_proc"])
    return check_task(task_id, wait_s=10)


def _cancel_active_tasks() -> None:
    """Cancel every in-flight task without blocking on worker cleanup."""
    with _LOCK:
        tasks = list(TASKS.values())
        active = [task for task in tasks if task["_thread"].is_alive()]
        for task in active:
            task["_cancelled"] = True
            task["status"] = "cancelled"
            task["output"] = "cancelled by caller"
    for task in active:
        if task["_proc"]:
            _kill(task["_proc"])


def list_shared_skills() -> str:
    names = sorted(
        {
            str(path.relative_to(skills_dir)).replace("\\", "/")[:-3]
            for skills_dir in _skill_dirs()
            for path in skills_dir.rglob("*.md")
            if path.name.lower() != "readme.md"
        }
    )
    return "\n".join(names) if names else "No shared skills are defined."


def list_auto_modes() -> str:
    return json.dumps(AutoController.describe(), ensure_ascii=False, indent=2)


# The WMI Provider Host (WmiPrvSE.exe) periodically pegs the CPU during long delegate runs and
# stalls the whole box. It is safe to kill: the WMI service (Winmgmt) respawns provider hosts on
# demand, so this frees CPU without touching the service itself.
WMI_PROVIDER_IMAGE = "WmiPrvSE.exe"


def kill_wmi() -> dict[str, Any]:
    if os.name != "nt":
        return {"status": "error", "killed": 0, "output": "kill_wmi is only supported on Windows"}
    # /F force, /T whole tree, /IM by image name: kills every provider host at once.
    done = subprocess.run(
        ["taskkill", "/F", "/T", "/IM", WMI_PROVIDER_IMAGE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (done.stdout + done.stderr).strip()
    lowered = output.lower()
    killed = output.upper().count("SUCCESS")

    # taskkill exits 128 with "not found" when nothing was running: that is a success for us.
    if killed == 0 and ("not found" in lowered or "no running" in lowered):
        return {"status": "ok", "killed": 0, "output": f"no {WMI_PROVIDER_IMAGE} processes were running"}

    # WmiPrvSE runs under a system account, so an unelevated taskkill is denied. Say so plainly:
    # rerun the harness/terminal as administrator to let it reap the provider hosts.
    if "access is denied" in lowered:
        denied = lowered.count("access is denied")
        detail = (
            f"terminated {killed} but could not kill {denied} {WMI_PROVIDER_IMAGE} process(es): access is denied. "
            "WmiPrvSE runs as a system account; run this MCP/terminal as administrator to kill it."
        )
        return {"status": "error", "killed": killed, "output": detail}

    if done.returncode != 0 and killed == 0:
        return {"status": "error", "killed": 0, "output": output or f"taskkill exit code {done.returncode}"}
    return {"status": "ok", "killed": killed, "output": f"terminated {killed} {WMI_PROVIDER_IMAGE} process(es); Windows will respawn them on demand"}


_ASK_PROPERTIES: dict[str, Any] = {
    "prompt": {"type": "string", "description": "Self-contained task brief: goal, files, constraints, how to verify."},
    "skill": {"type": "string", "description": "Optional shared skill name from .agents, without .md."},
    "session_id": {"type": "string", "description": "Continue an earlier conversation: pass the session_id a previous result returned."},
    "mode": {
        "type": "string",
        "enum": list(MODES),
        "description": (
            "read_only (default) for review/research; write for normal editable work; "
            "auto for trusted autonomous work using the delegate CLI's native no-prompt mode."
        ),
    },
    "model": {"type": "string", "description": "Model the delegate CLI should run. Omit to use that harness's own default."},
    "effort": {"type": "string", "description": "Reasoning effort for the model. Omit for the CLI default."},
    "background": {"type": "boolean", "default": True, "description": "Run detached and return a task_id immediately (default true for MCP calls). Set false only for short calls; foreground execution is capped at 60s."},
    "worktree": {"type": "boolean", "description": "Run in an isolated git worktree on branch bridge/<task_id>. Use for every parallel writer. Only committed files exist there."},
    "timeout_s": {"type": "number", "description": "Override the worker timeout (default 1800s in background; explicit foreground calls are capped at 60s)."},
}
_TASK_ID = {"task_id": {"type": "string", "description": "task_id returned by an ask_* call."}}


def _ask_properties(target: str) -> dict[str, Any]:
    properties = {name: dict(schema) for name, schema in _ASK_PROPERTIES.items()}
    if TARGETS[target].models:
        properties["model"]["description"] += " Known values: " + TARGETS[target].models
    if TARGETS[target].efforts:
        properties["effort"]["description"] += " Known values: " + TARGETS[target].efforts
    else:
        del properties["effort"]
    if target == "claude":
        properties["max_budget_usd"] = {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": (
                "Stop the run once its computed cost passes this many USD (Claude's own --max-budget-usd). "
                "Checked between turns, so one turn can overshoot. Works on subscription too."
            ),
        }
    if target == "codex":
        properties["prompt"]["maxLength"] = CODEX_MAX_PROMPT_CHARS
        properties["prompt"]["description"] += " Large context must be stored in a file and referenced by path."
        properties["background"] = {
            "type": "boolean",
            "const": True,
            "default": True,
            "description": "Codex calls always run detached; collect them with check_task(wait_s<=60).",
        }
    return properties


TOOLS: list[dict[str, Any]] = [
    {
        "name": tool,
        "description": TARGETS[name].description + " Returns JSON: status, task_id, session_id, output, files_changed, worktree, branch.",
        "inputSchema": {"type": "object", "properties": _ask_properties(name), "required": ["prompt"], "additionalProperties": False},
    }
    for tool, name in TOOL_TARGETS.items()
] + [
    {
        "name": "check_task",
        "description": "Get the status or result of a background task. wait_s blocks up to that many seconds (max 60) instead of polling.",
        "inputSchema": {
            "type": "object",
            "properties": {**_TASK_ID, "wait_s": {"type": "number", "minimum": 0, "maximum": 60, "description": "Seconds to wait for completion."}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_task",
        "description": "Stop a running background task and kill its process tree.",
        "inputSchema": {"type": "object", "properties": _TASK_ID, "required": ["task_id"], "additionalProperties": False},
    },
    {
        "name": "list_shared_skills",
        "description": "List reusable instruction files available under .agents.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_auto_modes",
        "description": "Show the native CLI permission mode used by mode='auto' for every delegate harness.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "kill_wmi",
        "description": (
            "Kill the Windows WMI Provider Host (WmiPrvSE.exe) processes that periodically peg the CPU and hang "
            "long-running delegate tasks. Safe and reversible: Windows respawns provider hosts on demand and the "
            "core WMI service (Winmgmt) is left untouched. Requires the MCP/terminal to run as administrator "
            "(WmiPrvSE runs as a system account); returns status='error' with an elevation hint otherwise. "
            "No-op on non-Windows hosts."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any] | str:
    if name in TOOL_TARGETS:
        target = TOOL_TARGETS[name]
        background = True if target == "codex" else bool(arguments.get("background", True))
        timeout_s = arguments.get("timeout_s")
        if not background:
            timeout_s = min(float(timeout_s or DEFAULT_TIMEOUT_SECONDS), MCP_FOREGROUND_MAX_SECONDS)
        return delegate(
            target,
            str(arguments.get("prompt", "")),
            skill=arguments.get("skill"),
            session_id=arguments.get("session_id"),
            mode=arguments.get("mode") or "read_only",
            background=background,
            worktree=bool(arguments.get("worktree")),
            timeout_s=timeout_s,
            model=(arguments.get("model") or "").strip() or None,
            effort=(arguments.get("effort") or "").strip() or None,
            max_budget_usd=arguments.get("max_budget_usd"),
        )
    if name == "check_task":
        return check_task(str(arguments.get("task_id", "")), arguments.get("wait_s") or 0)
    if name == "cancel_task":
        return cancel_task(str(arguments.get("task_id", "")))
    if name == "list_shared_skills":
        return list_shared_skills()
    if name == "list_auto_modes":
        return list_auto_modes()
    if name == "kill_wmi":
        return kill_wmi()
    raise ValueError(f"unknown tool: {name}")


def _answer_tool_call(request_id: Any, name: str, arguments: dict[str, Any]) -> None:
    try:
        outcome = _call_tool(name, arguments)
        if isinstance(outcome, dict):
            text = json.dumps(outcome, ensure_ascii=False, indent=1)
            is_error = outcome["status"] in ("error", "timeout")
        else:
            text, is_error = outcome, False
    except Exception as exc:  # MCP tool errors are returned as tool results.
        text, is_error = str(exc), True
    _write({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")

    if request_id is None:
        return

    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        supported = {"2024-11-05", "2025-03-26", "2025-06-18"}
        protocol = requested if requested in supported else "2024-11-05"
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agent-bridge", "version": "0.4.0"},
                    "instructions": (
                        "Delegate with ask_<harness>. Read .agents/orchestration.md before delegating. "
                        "Pass model=<name> (and effort=<level> where offered) to pick a model inside that harness's "
                        "subscription; omit them for the defaults. Results carry token usage, and check_task on a "
                        "running task returns its recent activity and idle time. "
                        "Default mode is read_only; use mode='write' for normal edits or mode='auto' for trusted no-prompt autonomous work. "
                        "Use worktree=true for parallel writers. "
                        "Pass a returned session_id to continue a conversation. "
                        "background=true returns a task_id for check_task/cancel_task; Codex is always detached. "
                        "Codex prompt size, outstanding work, and browser cooldown are enforced by the server. "
                        "Delegation depth is limited to prevent recursive agent loops."
                        + _limits_note()
                        + _missing_clis_note()
                    ),
                },
            }
        )
        return

    if method == "ping":
        _write({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return

    if method == "tools/list":
        _write({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            _error(request_id, -32602, "invalid tools/call parameters")
            return
        # One thread per call so a slow delegate never blocks the server.
        _CALLS[:] = [thread for thread in _CALLS if thread.is_alive()]
        call = threading.Thread(target=_answer_tool_call, args=(request_id, name, arguments), daemon=True)
        _CALLS.append(call)
        call.start()
        return

    _error(request_id, -32601, f"method not found: {method}")


def main() -> None:
    # MCP stdio is UTF-8; a Windows pipe defaults to cp1252, where a delegate's "→" kills the reply and the caller hangs.
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
            _handle(message)
        except json.JSONDecodeError as exc:
            _error(None, -32700, f"parse error: {exc}")
        except Exception as exc:
            request_id = message.get("id") if isinstance(locals().get("message"), dict) else None
            _error(request_id, -32603, str(exc))

    # Stdin closed: mark every delegate cancelled, including jobs still queued on a worker slot.
    # A queued job sees _cancelled before spawning; a job racing with shutdown sees it immediately after spawn.
    _cancel_active_tasks()
    for call in _CALLS:
        call.join(timeout=2)
    # A request thread may have created a task while EOF was being handled. Sweep once more.
    _cancel_active_tasks()
    for task in list(TASKS.values()):
        task["_thread"].join(timeout=2)


if __name__ == "__main__":
    main()
