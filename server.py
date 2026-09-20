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


# The bridge is installed once per machine; the project is wherever the harness launched it.
ROOT = Path(os.environ.get("AGENT_BRIDGE_ROOT") or os.getcwd()).resolve()
GLOBAL_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
LOG_PATH = Path(__file__).resolve().parent / "logs" / "calls.jsonl"
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
MODES = ("read_only", "write")

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


def _compose_prompt(prompt: str, skill: str | None, target: str) -> str:
    shared = _load_skill(skill)
    sections = [
        "You are receiving a delegated task from another local agent harness.",
        f"Target harness: {target}.",
        "Work in the current directory and return a concise result to the caller.",
        "Do not delegate this task to another harness unless the task explicitly asks for cross-agent consultation.",
        "If you are blocked or the task is ambiguous, reply 'BLOCKED: <what you need>' instead of guessing.",
    ]
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


def _parse_claude(raw: str) -> tuple[str, str | None]:
    return _parse_json(raw, "result")


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


def _codex(prompt: str, mode: str, session_id: str | None) -> list[str]:
    sandbox = "workspace-write" if mode == "write" else "read-only"
    resume = ["resume"] if session_id else []
    return [
        "codex",
        "exec",
        *resume,
        "-m",
        CODEX_MODEL,
        "-c",
        f'sandbox_mode="{sandbox}"',
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.multi_agent_v2=false",
        "-c",
        "features.unbounded_connection_retries=false",
        "--json",
        *([session_id] if session_id else []),
        "-",
    ]


def _claude(prompt: str, mode: str, session_id: str | None) -> list[str]:
    perms = ["--permission-mode", "acceptEdits"] if mode == "write" else ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    return ["claude", "-p", "--output-format", "json", *perms, *(["--resume", session_id] if session_id else [])]


def _kiro(prompt: str, mode: str, session_id: str | None) -> list[str]:
    if session_id:
        raise ValueError("the Kiro worker is stateless: session_id is not supported")
    tools = "fs_read,fs_write,execute_bash" if mode == "write" else "fs_read"
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
        f"--trust-tools={tools}",
        prompt,
    ]


def _gemini(prompt: str, mode: str, session_id: str | None) -> list[str]:
    approval = "auto_edit" if mode == "write" else "plan"
    return ["gemini", "-o", "json", "--approval-mode", approval, *(["-r", session_id] if session_id else [])]


def _opencode(prompt: str, mode: str, session_id: str | None) -> list[str]:
    agent = "build" if mode == "write" else "plan"
    return ["opencode", "run", "--format", "json", "--agent", agent, *(["-s", session_id] if session_id else [])]


@dataclass
class Target:
    label: str
    build: Callable[[str, str, str | None], list[str]]
    parse: Callable[[str], tuple[str, str | None]]
    # gemini and opencode resolve to .CMD shims on Windows, which mangle multi-line args: pipe the prompt.
    stdin: bool = True
    max_parallel: int = 3
    description: str = ""


TARGETS: dict[str, Target] = {
    "codex": Target(
        "Codex", _codex, _parse_codex, max_parallel=CODEX_MAX_PARALLEL,
        description=(
            "Delegate a focused task to Codex CLI. Codex calls are always detached, nested Codex agents are disabled, "
            "and browser capacity is enforced by the bridge."
        ),
    ),
    "claude": Target(
        "Claude Code", _claude, _parse_claude,
        description="Delegate a focused task to Claude Code. Use for implementation, review, or a second opinion.",
    ),
    "kiro": Target(
        "Kiro worker", _kiro, _parse_text, stdin=False,
        description="Delegate a bounded implementation task to the local Kiro worker (pass mode='write'). Stateless: no session_id. Kiro has no bridge tools.",
    ),
    "gemini": Target(
        "Gemini CLI", _gemini, _parse_gemini,
        description="Delegate to Gemini CLI. Use for video/audio or very large inputs; reference files as @path/to/file in the prompt.",
    ),
    "opencode": Target(
        "OpenCode", _opencode, _parse_opencode,
        description="Delegate a focused task to OpenCode. Use for implementation, review, or a second opinion.",
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


def _codex_admission_error_locked(prompt: str) -> str | None:
    """Return a deterministic rejection reason. Caller must hold _LOCK."""
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
        if task.get("target") == "codex" and task.get("status") == "running"
    )
    if outstanding >= CODEX_MAX_OUTSTANDING:
        return (
            f"Codex capacity is full ({outstanding}/{CODEX_MAX_OUTSTANDING} running or queued); "
            "collect or cancel an existing task before submitting another."
        )
    return None


def _record_codex_health(task: dict[str, Any]) -> None:
    if task.get("target") != "codex" or task.get("status") not in ("error", "timeout"):
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

    target = TARGETS[task["target"]]
    full_prompt = _compose_prompt(prompt, skill, target.label)
    command = target.build(full_prompt, task["mode"], task["session_id"])
    executable = shutil.which(command[0])
    if not executable:
        raise RuntimeError(f"{command[0]} is not installed or not on PATH")
    command[0] = executable

    with _LOCK:
        slot = _SLOTS.setdefault(task["target"], threading.Semaphore(target.max_parallel))
    with slot:
        if task["_cancelled"]:
            return
        cwd = _add_worktree(task) if worktree else ROOT
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        stdout_path = TRANSCRIPT_DIR / f"{task['task_id']}.stdout.log"
        stderr_path = TRANSCRIPT_DIR / f"{task['task_id']}.stderr.log"
        task["transcript"] = str(stdout_path)
        task["stderr_transcript"] = str(stderr_path)
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=_child_env(target.label),
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
                task["status"] = "timeout"
                task["output"] = f"{target.label} timed out after {timeout_s:g}s"
                return

    if task["_cancelled"]:
        return
    stdout, stdout_truncated, stdout_bytes = _read_bounded(stdout_path)
    stderr, stderr_truncated, stderr_bytes = _read_bounded(stderr_path)
    task["transcript_bytes"] = stdout_bytes + stderr_bytes
    output, session = target.parse(_ANSI.sub("", stdout))
    task["session_id"] = session or task["session_id"]
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
) -> dict[str, Any]:
    if target == "codex":
        background = True
    task: dict[str, Any] = {
        "task_id": uuid.uuid4().hex[:8],
        "target": target,
        "status": "running",
        "mode": mode,
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
        if target == "codex":
            rejection = _codex_admission_error_locked(prompt)
            if rejection:
                raise RuntimeError(rejection)
        TASKS[task["task_id"]] = task
    task["_thread"].start()
    if not background:
        task["_thread"].join()
    return _public(task)


def check_task(task_id: str, wait_s: float = 0) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if not task:
        return {"task_id": task_id, "status": "error", "output": "unknown task_id (tasks do not survive a bridge restart)"}
    if task.get("_cancelled"):
        return _public(task)
    task["_thread"].join(timeout=max(0.0, min(float(wait_s), CHECK_TASK_MAX_WAIT_SECONDS)))
    return _public(task)


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
    for task in tasks:
        if not task["_thread"].is_alive():
            continue
        task["_cancelled"] = True
        task["status"] = "cancelled"
        task["output"] = "cancelled by caller"
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


_ASK_PROPERTIES: dict[str, Any] = {
    "prompt": {"type": "string", "description": "Self-contained task brief: goal, files, constraints, how to verify."},
    "skill": {"type": "string", "description": "Optional shared skill name from .agents, without .md."},
    "session_id": {"type": "string", "description": "Continue an earlier conversation: pass the session_id a previous result returned."},
    "mode": {"type": "string", "enum": list(MODES), "description": "read_only (default) for review/research; write to let the delegate edit files."},
    "background": {"type": "boolean", "default": True, "description": "Run detached and return a task_id immediately (default true for MCP calls). Set false only for short calls; foreground execution is capped at 60s."},
    "worktree": {"type": "boolean", "description": "Run in an isolated git worktree on branch bridge/<task_id>. Use for every parallel writer. Only committed files exist there."},
    "timeout_s": {"type": "number", "description": "Override the worker timeout (default 1800s in background; explicit foreground calls are capped at 60s)."},
}
_TASK_ID = {"task_id": {"type": "string", "description": "task_id returned by an ask_* call."}}


def _ask_properties(target: str) -> dict[str, Any]:
    properties = {name: dict(schema) for name, schema in _ASK_PROPERTIES.items()}
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
        )
    if name == "check_task":
        return check_task(str(arguments.get("task_id", "")), arguments.get("wait_s") or 0)
    if name == "cancel_task":
        return cancel_task(str(arguments.get("task_id", "")))
    if name == "list_shared_skills":
        return list_shared_skills()
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
                    "serverInfo": {"name": "agent-bridge", "version": "0.3.0"},
                    "instructions": (
                        "Delegate with ask_<harness>. Read .agents/orchestration.md before delegating. "
                        "Default mode is read_only; pass mode='write' plus worktree=true for parallel writers. "
                        "Pass a returned session_id to continue a conversation. "
                        "background=true returns a task_id for check_task/cancel_task; Codex is always detached. "
                        "Codex prompt size, outstanding work, and browser cooldown are enforced by the server. "
                        "Delegation depth is limited to prevent recursive agent loops."
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
