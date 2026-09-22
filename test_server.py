"""Run: python test_server.py  (also works under pytest)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="bridge-test-"))
REPO = TMP / "repo"
FAKE = TMP / "fake_cli.py"
FAKE.write_text(
    """
import json, pathlib, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
if "--sleep" in args:
    time.sleep(float(args[args.index("--sleep") + 1]))
if "--write" in args:
    pathlib.Path(args[args.index("--write") + 1]).write_text("x")
if "--fail" in args:
    message = args[args.index("--error-text") + 1] if "--error-text" in args else "boom"
    if "--stdout-before-fail" in args:
        print(json.dumps({"result": "real structured failure"}))
    sys.stderr.write(message)
    sys.exit(3)
print(json.dumps({"session_id": "sess-1", "result": f"args={args} prompt={prompt}"}))
""",
    encoding="utf-8",
)

REPO.mkdir()
for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *cmd], cwd=REPO, check=True, capture_output=True
    )
server.ROOT = REPO
server.LOG_PATH = TMP / "calls.jsonl"
server.TRANSCRIPT_DIR = TMP / "transcripts"


def fake(*extra: str, max_parallel: int = 3) -> str:
    name = f"fake{len(server.TARGETS)}"
    server.TARGETS[name] = server.Target(
        label="Fake",
        build=lambda prompt, mode, session_id, model: [
            sys.executable,
            str(FAKE),
            *extra,
            f"--mode={mode}",
            *([f"--model={model}"] if model else []),
            *(["--resume", session_id] if session_id else []),
        ],
        parse=server._parse_claude,
        stdin=True,
        max_parallel=max_parallel,
    )
    return name


def _root_seen_by_a_fresh_server(cwd: Path, env_root: Path | None = None) -> Path:
    env = {k: v for k, v in os.environ.items() if k != "AGENT_BRIDGE_ROOT"}
    if env_root:
        env["AGENT_BRIDGE_ROOT"] = str(env_root)
    code = f"import sys; sys.path.insert(0, r'{Path(__file__).resolve().parent}'); import server; print(server.ROOT)"
    done = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return Path(done.stdout.strip()).resolve()


def test_project_root_is_the_directory_the_harness_launched_from():
    assert _root_seen_by_a_fresh_server(TMP) == TMP.resolve()


def test_project_root_can_be_overridden_by_env():
    assert _root_seen_by_a_fresh_server(TMP, env_root=REPO) == REPO.resolve()


def test_project_skill_overrides_global_skill_of_the_same_name():
    original = server.GLOBAL_SKILLS_DIR
    server.GLOBAL_SKILLS_DIR = TMP / "global-skills"
    try:
        server.GLOBAL_SKILLS_DIR.mkdir(exist_ok=True)
        (server.GLOBAL_SKILLS_DIR / "review.md").write_text("global", encoding="utf-8")
        (server.GLOBAL_SKILLS_DIR / "only-global.md").write_text("global only", encoding="utf-8")
        (REPO / ".agents").mkdir(exist_ok=True)
        (REPO / ".agents" / "review.md").write_text("project", encoding="utf-8")
        assert server._load_skill("review") == "project"
        assert server._load_skill("only-global") == "global only"
        assert server.list_shared_skills().splitlines() == ["only-global", "review"]
    finally:
        server.GLOBAL_SKILLS_DIR = original


def test_skill_name_cannot_escape_the_skill_directories():
    try:
        server._load_skill("../server")
    except ValueError:
        return
    raise AssertionError("path traversal was not rejected")


def test_worktree_dir_is_hidden_from_the_project_git_status():
    result = server.delegate(fake("--write", "kept.txt"), "x", mode="write", worktree=True)
    assert result["worktree"], result
    assert ".worktrees" not in server._git("status", "--porcelain")


def test_audit_log_records_the_project_root():
    result = server.delegate(fake(), "x")
    entries = [json.loads(line) for line in server.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    assert next(e for e in entries if e["task_id"] == result["task_id"])["root"] == str(REPO)


def test_delegate_returns_structured_result():
    result = server.delegate(fake(), "hello")
    assert result["status"] == "ok", result
    assert "hello" in result["output"]
    assert result["session_id"] == "sess-1"
    assert result["task_id"] and isinstance(result["duration_s"], float)


def test_mode_defaults_to_read_only():
    assert "--mode=read_only" in server.delegate(fake(), "x")["output"]


def test_session_id_is_passed_to_the_cli():
    assert "'--resume', 'abc'" in server.delegate(fake(), "x", session_id="abc")["output"]


def test_cli_failure_is_status_error_with_stderr():
    result = server.delegate(fake("--fail"), "x")
    assert result["status"] == "error", result
    assert "boom" in result["output"]


def test_cli_failure_prefers_parsed_stdout_over_stderr_noise():
    result = server.delegate(fake("--fail", "--stdout-before-fail", "--error-text", "harmless transport warning"), "x")
    assert result["status"] == "error", result
    assert "real structured failure" in result["output"]
    assert "harmless transport warning" not in result["output"]


def test_timeout_kills_the_process():
    started = time.monotonic()
    result = server.delegate(fake("--sleep", "30"), "x", timeout_s=1)
    assert result["status"] == "timeout", result
    assert time.monotonic() - started < 10


def test_background_returns_immediately_then_check_task_gets_result():
    started = time.monotonic()
    first = server.delegate(fake("--sleep", "1"), "bg", background=True)
    assert first["status"] == "running", first
    assert time.monotonic() - started < 0.9
    done = server.check_task(first["task_id"], wait_s=15)
    assert done["status"] == "ok" and "bg" in done["output"], done


def test_mcp_call_defaults_to_background():
    target = fake("--sleep", "1")
    tool = "ask_test_background_default"
    server.TOOL_TARGETS[tool] = target
    try:
        started = time.monotonic()
        first = server._call_tool(tool, {"prompt": "bg"})
        assert first["status"] == "running", first
        assert time.monotonic() - started < 0.9
        assert server.check_task(first["task_id"], wait_s=5)["status"] == "ok"
    finally:
        server.TOOL_TARGETS.pop(tool, None)


def test_explicit_mcp_foreground_call_is_hard_capped():
    target = fake("--sleep", "3")
    tool = "ask_test_foreground_cap"
    server.TOOL_TARGETS[tool] = target
    previous = server.MCP_FOREGROUND_MAX_SECONDS
    server.MCP_FOREGROUND_MAX_SECONDS = 0.2
    try:
        started = time.monotonic()
        result = server._call_tool(tool, {"prompt": "x", "background": False, "timeout_s": 30})
        assert result["status"] == "timeout", result
        assert time.monotonic() - started < 5
    finally:
        server.MCP_FOREGROUND_MAX_SECONDS = previous
        server.TOOL_TARGETS.pop(tool, None)


def test_check_task_wait_is_hard_capped():
    previous = server.CHECK_TASK_MAX_WAIT_SECONDS
    server.CHECK_TASK_MAX_WAIT_SECONDS = 0.2
    first = server.delegate(fake("--sleep", "3"), "x", background=True)
    try:
        started = time.monotonic()
        result = server.check_task(first["task_id"], wait_s=30)
        assert result["status"] == "running", result
        assert time.monotonic() - started < 1
    finally:
        server.CHECK_TASK_MAX_WAIT_SECONDS = previous
        server.cancel_task(first["task_id"])


def test_cancel_task_stops_a_running_task():
    first = server.delegate(fake("--sleep", "30"), "x", background=True)
    time.sleep(0.5)
    started = time.monotonic()
    assert server.cancel_task(first["task_id"])["status"] == "cancelled"
    assert server.check_task(first["task_id"], wait_s=10)["status"] == "cancelled"
    assert time.monotonic() - started < 10


def test_disconnect_cleanup_cancels_running_and_queued_tasks():
    target = fake("--sleep", "30", max_parallel=1)
    first = server.delegate(target, "running", background=True)
    second = server.delegate(target, "queued", background=True)
    deadline = time.monotonic() + 5
    while server.TASKS[first["task_id"]]["_proc"] is None and time.monotonic() < deadline:
        time.sleep(0.05)

    server._cancel_active_tasks()

    assert server.check_task(first["task_id"], wait_s=10)["status"] == "cancelled"
    assert server.check_task(second["task_id"], wait_s=10)["status"] == "cancelled"
    assert server.TASKS[second["task_id"]]["_proc"] is None


def test_check_task_unknown_id_is_error():
    assert server.check_task("nope")["status"] == "error"


def test_every_call_is_written_to_the_audit_log():
    result = server.delegate(fake(), "audit me")
    entries = [json.loads(line) for line in server.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    entry = next(e for e in entries if e["task_id"] == result["task_id"])
    assert entry["status"] == "ok" and entry["target"].startswith("fake")
    assert "audit me" in entry["prompt"] and "ts" in entry and "duration_s" in entry


def test_worktree_isolates_writes_and_reports_changed_files():
    result = server.delegate(fake("--write", "new.txt"), "x", mode="write", worktree=True)
    assert result["status"] == "ok", result
    assert result["files_changed"] == ["new.txt"], result
    assert (Path(result["worktree"]) / "new.txt").is_file()
    assert not (REPO / "new.txt").exists()
    assert result["branch"] == f"bridge/{result['task_id']}"


def test_worktree_without_changes_is_cleaned_up():
    result = server.delegate(fake(), "x", worktree=True)
    assert result["files_changed"] == [] and result["worktree"] is None, result
    assert not (REPO / ".worktrees" / result["task_id"]).exists()
    assert f"bridge/{result['task_id']}" not in server._git("branch", "--list")


def test_git_does_not_hang_while_the_server_is_blocked_reading_stdin():
    # On Windows a child that inherits a pipe with a pending read on it deadlocks; the MCP stdin is such a pipe.
    code = (
        "import sys, threading, time; from pathlib import Path;"
        f"sys.path.insert(0, r'{Path(__file__).resolve().parent}'); import server;"
        f"server.ROOT = Path(r'{REPO}');"
        "threading.Thread(target=sys.stdin.readline, daemon=True).start(); time.sleep(0.5);"
        "print(server._git('rev-parse', 'HEAD').strip())"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        server._kill(child)
        raise AssertionError("git hung on the inherited stdin pipe")
    finally:
        child.stdin.close()
    assert len(child.stdout.read().strip()) == 40


def test_max_parallel_caps_concurrent_runs_per_target():
    name = fake("--sleep", "1", max_parallel=1)
    started = time.monotonic()
    tasks = [server.delegate(name, "x", background=True) for _ in range(2)]
    for task in tasks:
        assert server.check_task(task["task_id"], wait_s=20)["status"] == "ok"
    assert time.monotonic() - started >= 2


def test_depth_limit_is_status_error(monkeypatch=None):
    import os

    os.environ["AGENT_BRIDGE_DEPTH"] = str(server.MAX_DEPTH)
    try:
        result = server.delegate(fake(), "x")
    finally:
        del os.environ["AGENT_BRIDGE_DEPTH"]
    assert result["status"] == "error" and "depth" in result["output"]


def test_slow_tool_call_does_not_block_the_server():
    written: list[dict] = []
    original = server._write
    server._write = written.append
    try:
        name = fake("--sleep", "1")
        server.TOOL_TARGETS["ask_slow"] = name
        server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_slow", "arguments": {"prompt": "x"}}})
        server._handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert [m["id"] for m in written] == [2], written
        deadline = time.monotonic() + 5
        while len(written) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        body = json.loads(written[1]["result"]["content"][0]["text"])
        assert written[1]["id"] == 1 and body["status"] == "running"
        assert server.check_task(body["task_id"], wait_s=5)["status"] == "ok"
    finally:
        server._write = original


def test_non_ascii_results_survive_the_stdio_pipe():
    # On Windows a piped stdout defaults to cp1252: a delegate's "→" killed the answering thread and the caller hung.
    request = b'{"jsonrpc": "2.0", "id": 1, "method": "\\u2192"}\n'
    done = subprocess.run([sys.executable, str(Path(server.__file__))], input=request, capture_output=True, timeout=20)
    reply = json.loads(done.stdout.decode("utf-8"))
    assert reply["id"] == 1 and "→" in reply["error"]["message"], done.stderr.decode("utf-8", "replace")


def test_tools_list_exposes_the_new_arguments_and_task_tools():
    tools = {t["name"]: t for t in server.TOOLS}
    assert {
        "ask_codex",
        "ask_claude",
        "ask_kiro",
        "ask_gemini",
        "ask_opencode",
        "check_task",
        "cancel_task",
        "list_shared_skills",
        "list_auto_modes",
    } <= set(tools)
    props = tools["ask_codex"]["inputSchema"]["properties"]
    assert {"prompt", "skill", "session_id", "mode", "background", "worktree", "timeout_s"} <= set(props)
    assert props["mode"]["enum"] == ["read_only", "write", "auto"]
    assert props["background"]["default"] is True
    assert props["background"]["const"] is True
    assert props["prompt"]["maxLength"] == server.CODEX_MAX_PROMPT_CHARS
    wait_schema = tools["check_task"]["inputSchema"]["properties"]["wait_s"]
    assert wait_schema["maximum"] == 60


def test_codex_command_maps_mode_and_resume():
    build = server.TARGETS["codex"].build
    command = build("p", "read_only", None, None)
    assert 'sandbox_mode="read-only"' in command
    assert command[command.index("-m") + 1] == server.CODEX_MODEL == "chatgpt-web/high"
    assert "features.multi_agent=false" in command
    assert "features.multi_agent_v2=false" in command
    assert "features.unbounded_connection_retries=false" in command
    assert 'sandbox_mode="workspace-write"' in build("p", "write", None, None)
    auto = build("p", "auto", None, None)
    assert 'sandbox_mode="workspace-write"' in auto
    assert 'approval_policy="never"' in auto
    assert "--approve-for-me" not in auto
    resumed = build("p", "read_only", "abc", None)
    assert resumed[:3] == ["codex", "exec", "resume"] and "abc" in resumed and resumed[-1] == "-"


def test_auto_modes_map_to_native_cli_flags():
    claude = server.TARGETS["claude"].build("p", "auto", None, None)
    assert ["--permission-mode", "auto"] == claude[claude.index("--permission-mode") : claude.index("--permission-mode") + 2]
    assert ["--permission-prompts", "none"] == claude[
        claude.index("--permission-prompts") : claude.index("--permission-prompts") + 2
    ]
    assert "--dangerously-skip-permissions" not in claude

    kiro = server.TARGETS["kiro"].build("p", "auto", None, None)
    assert "--trust-all-tools" in kiro
    assert not any(arg.startswith("--trust-tools=") for arg in kiro)
    assert kiro[kiro.index("--agent") + 1] == "worker"

    gemini = server.TARGETS["gemini"].build("p", "auto", None, None)
    assert ["--approval-mode", "yolo"] == gemini[gemini.index("--approval-mode") : gemini.index("--approval-mode") + 2]

    opencode = server.TARGETS["opencode"].build("p", "auto", None, None)
    assert opencode[opencode.index("--agent") + 1] == "build"
    assert "--auto" in opencode


def test_gemini_auto_mode_trusts_the_headless_workspace():
    task = {
        "task_id": "gemauto",
        "target": "gemini",
        "status": "running",
        "mode": "auto",
        "session_id": None,
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
    original_popen = subprocess.Popen
    seen = {}

    class FakeProc:
        pid = 1
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return None

    def fake_popen(*args, **kwargs):
        seen.update(kwargs["env"])
        return FakeProc()

    subprocess.Popen = fake_popen
    original_read = server._read_bounded
    server._read_bounded = lambda path: ("{}", False, 2)
    try:
        server._execute(task, "x", None, False, 1)
    finally:
        subprocess.Popen = original_popen
        server._read_bounded = original_read
    assert seen["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_list_auto_modes_describes_every_delegate():
    modes = json.loads(server.list_auto_modes())
    assert set(modes) == {"codex", "claude", "kiro", "gemini", "opencode"}
    assert modes["codex"]["cli_args"] == ["-c", 'approval_policy="never"']
    assert modes["kiro"]["cli_args"] == ["--trust-all-tools"]


def test_parsers_extract_output_and_session_id():
    codex = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t-1"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"command_execution","aggregated_output":"noise"}}',
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"PONG"}}',
        ]
    )
    assert server._parse_codex(codex) == ("PONG", "t-1")
    opencode = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_1","part":{"type":"step-start"}}',
            '{"type":"text","sessionID":"ses_1","part":{"type":"text","text":"PONG"}}',
        ]
    )
    assert server._parse_opencode(opencode) == ("PONG", "ses_1")
    assert server._parse_gemini('{"session_id": "g-1", "response": "PONG"}') == ("PONG", "g-1")
    assert server._parse_claude('{"session_id": "c-1", "result": "PONG"}') == ("PONG", "c-1")
    assert server._parse_claude("not json") == ("not json", None)


def test_bounded_transcript_reader_keeps_head_and_tail():
    path = TMP / "large.log"
    path.write_text("HEAD" + ("x" * 10000) + "TAIL", encoding="utf-8")
    previous = server.MAX_PARSE_BYTES
    server.MAX_PARSE_BYTES = 1024
    try:
        text, truncated, size = server._read_bounded(path)
    finally:
        server.MAX_PARSE_BYTES = previous
    assert truncated is True
    assert size > len(text)
    assert text.startswith("HEAD") and text.endswith("TAIL")
    assert "transcript truncated for parsing" in text


def test_codex_call_is_forced_into_background():
    original_target = server.TARGETS["codex"]
    original_slot = server._SLOTS.pop("codex", None)
    original_health = dict(server._CODEX_HEALTH)
    server.TARGETS["codex"] = server.Target(
        label="Fake Codex",
        build=lambda prompt, mode, session_id, model: [sys.executable, str(FAKE), "--sleep", "1"],
        parse=server._parse_claude,
        max_parallel=1,
    )
    server._CODEX_HEALTH.update(failures=0, last_failure=0.0, blocked_until=0.0, reason="")
    try:
        started = time.monotonic()
        first = server.delegate("codex", "x", background=False)
        assert first["status"] == "running", first
        assert time.monotonic() - started < 0.9
        assert server.check_task(first["task_id"], wait_s=5)["status"] == "ok"
    finally:
        server.TARGETS["codex"] = original_target
        server._SLOTS.pop("codex", None)
        if original_slot is not None:
            server._SLOTS["codex"] = original_slot
        server._CODEX_HEALTH.clear()
        server._CODEX_HEALTH.update(original_health)


def test_codex_prompt_limit_is_enforced_before_process_start():
    previous = server.CODEX_MAX_PROMPT_CHARS
    original_health = dict(server._CODEX_HEALTH)
    server.CODEX_MAX_PROMPT_CHARS = 5
    server._CODEX_HEALTH.update(failures=0, last_failure=0.0, blocked_until=0.0, reason="")
    try:
        try:
            server.delegate("codex", "123456", background=True)
        except RuntimeError as exc:
            assert "limit" in str(exc) and "file" in str(exc)
        else:
            raise AssertionError("oversized Codex prompt was admitted")
    finally:
        server.CODEX_MAX_PROMPT_CHARS = previous
        server._CODEX_HEALTH.clear()
        server._CODEX_HEALTH.update(original_health)


def test_codex_outstanding_capacity_is_enforced_atomically():
    original_target = server.TARGETS["codex"]
    original_slot = server._SLOTS.pop("codex", None)
    original_limit = server.CODEX_MAX_OUTSTANDING
    original_health = dict(server._CODEX_HEALTH)
    server.TARGETS["codex"] = server.Target(
        label="Fake Codex",
        build=lambda prompt, mode, session_id, model: [sys.executable, str(FAKE), "--sleep", "30"],
        parse=server._parse_claude,
        max_parallel=1,
    )
    server.CODEX_MAX_OUTSTANDING = 2
    server._CODEX_HEALTH.update(failures=0, last_failure=0.0, blocked_until=0.0, reason="")
    tasks = []
    try:
        tasks = [server.delegate("codex", "x", background=True) for _ in range(2)]
        try:
            server.delegate("codex", "x", background=True)
        except RuntimeError as exc:
            assert "capacity is full" in str(exc)
        else:
            raise AssertionError("third Codex task was admitted")
    finally:
        for task in tasks:
            server.cancel_task(task["task_id"])
        for task in tasks:
            server.TASKS[task["task_id"]]["_thread"].join(timeout=5)
        server.TARGETS["codex"] = original_target
        server.CODEX_MAX_OUTSTANDING = original_limit
        server._SLOTS.pop("codex", None)
        if original_slot is not None:
            server._SLOTS["codex"] = original_slot
        server._CODEX_HEALTH.clear()
        server._CODEX_HEALTH.update(original_health)


def test_codex_browser_failure_opens_circuit():
    original_health = dict(server._CODEX_HEALTH)
    server._CODEX_HEALTH.update(failures=0, last_failure=0.0, blocked_until=0.0, reason="")
    try:
        server._record_codex_health(
            {"target": "codex", "status": "error", "output": "ChatGPT browser stage timed out: send_prompt"}
        )
        try:
            server.delegate("codex", "x", background=True)
        except RuntimeError as exc:
            assert "circuit is open" in str(exc) and "browser stage timed out" in str(exc)
        else:
            raise AssertionError("Codex task was admitted while the circuit was open")
    finally:
        server._CODEX_HEALTH.clear()
        server._CODEX_HEALTH.update(original_health)


def test_kill_wmi_is_registered_as_a_no_argument_tool():
    tools = {t["name"]: t for t in server.TOOLS}
    assert "kill_wmi" in tools
    assert tools["kill_wmi"]["inputSchema"]["properties"] == {}
    assert tools["kill_wmi"]["inputSchema"]["additionalProperties"] is False


def test_kill_wmi_reports_how_many_provider_hosts_it_terminated():
    original = subprocess.run
    calls = {}

    class FakeCompleted:
        returncode = 0
        stdout = 'SUCCESS: The process "WmiPrvSE.exe" with PID 111 has been terminated.\n' \
                 'SUCCESS: The process "WmiPrvSE.exe" with PID 222 has been terminated.\n'
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls["cmd"] = cmd
        return FakeCompleted()

    server.os.name = "nt"
    subprocess.run = fake_run
    try:
        result = server.kill_wmi()
    finally:
        subprocess.run = original
    assert calls["cmd"] == ["taskkill", "/F", "/T", "/IM", "WmiPrvSE.exe"]
    assert result["status"] == "ok" and result["killed"] == 2


def test_kill_wmi_treats_no_running_processes_as_success():
    original = subprocess.run

    class FakeCompleted:
        returncode = 128
        stdout = ""
        stderr = 'ERROR: The process "WmiPrvSE.exe" not found.\n'

    server.os.name = "nt"
    subprocess.run = lambda *a, **k: FakeCompleted()
    try:
        result = server.kill_wmi()
    finally:
        subprocess.run = original
    assert result["status"] == "ok" and result["killed"] == 0


def test_kill_wmi_routes_through_call_tool():
    original = subprocess.run

    class FakeCompleted:
        returncode = 0
        stdout = 'SUCCESS: The process "WmiPrvSE.exe" with PID 111 has been terminated.\n'
        stderr = ""

    server.os.name = "nt"
    subprocess.run = lambda *a, **k: FakeCompleted()
    try:
        result = server._call_tool("kill_wmi", {})
    finally:
        subprocess.run = original
    assert result["status"] == "ok" and result["killed"] == 1


def test_kill_wmi_reports_access_denied_as_needs_elevation():
    original = subprocess.run

    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = (
            "ERROR: The process with PID 8184 (child process of PID 1296) could not be terminated.\n"
            "Reason: Access is denied.\n"
        )

    server.os.name = "nt"
    subprocess.run = lambda *a, **k: FakeCompleted()
    try:
        result = server.kill_wmi()
    finally:
        subprocess.run = original
    assert result["status"] == "error" and result["killed"] == 0
    assert "administrator" in result["output"].lower()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}\n{traceback.format_exc()}")
    print(f"\n{failed} failed")
    sys.exit(1 if failed else 0)


def test_model_reaches_every_cli_and_defaults_stay_put():
    expected = {
        "codex": ("-m", "gpt-5.6-sol"),
        "claude": ("--model", "claude-opus-5"),
        "kiro": ("--model", "gpt-5.6-luna"),
        "gemini": ("-m", "gemini-3-flash"),
        "opencode": ("-m", "opencode/claude-sonnet-5"),
    }
    for name, (flag, model) in expected.items():
        command = server.TARGETS[name].build("p", "read_only", None, model)
        assert command[command.index(flag) + 1] == model, name
        default = server.TARGETS[name].build("p", "read_only", None, None)
        # Only Codex pins a default model; the rest fall through to the CLI's own choice.
        assert (flag in default) is (name == "codex"), name
    assert server.TARGETS["codex"].build("p", "read_only", None, None)[
        server.TARGETS["codex"].build("p", "read_only", None, None).index("-m") + 1
    ] == server.CODEX_MODEL


def test_model_is_advertised_and_forwarded_by_the_tool_call(monkeypatch):
    for name in ("codex", "claude", "kiro", "gemini", "opencode"):
        description = server._ask_properties(name)["model"]["description"]
        assert "Known values:" in description, name
    assert "chatgpt-web/high" in server._ask_properties("codex")["model"]["description"]
    assert "provider/model" in server._ask_properties("opencode")["model"]["description"]

    seen = {}
    monkeypatch.setattr(server, "delegate", lambda target, prompt, **kwargs: seen.update(kwargs) or {"status": "ok"})
    server._call_tool("ask_kiro", {"prompt": "p", "model": "  claude-opus-5  "})
    assert seen["model"] == "claude-opus-5"
    server._call_tool("ask_kiro", {"prompt": "p", "model": "   "})
    assert seen["model"] is None


def test_usage_limit_is_recorded_and_surfaced_at_session_start(monkeypatch):
    monkeypatch.setattr(server, "LIMITS_PATH", TMP / "limits.json")
    monkeypatch.setattr(server, "_LIMITS", {})
    server._record_limit({"target": "kiro", "status": "ok", "output": "quota exceeded"})
    assert not server._LIMITS  # A successful run never marks a harness spent.
    server._record_limit({"target": "kiro", "status": "error", "output": "compile error: limit of 3 args"})
    assert not server._LIMITS  # "limit" alone is not a quota failure.

    # Taken from a real OpenCode reply: the shape a spent account actually arrives in.
    server._record_limit(
        {
            "target": "opencode",
            "status": "error",
            "output": '{"error":{"message":"Upstream request failed: Insufficient account funds","statusCode":402}}',
        }
    )
    assert "opencode" in server._LIMITS
    server._LIMITS.clear()

    server._record_limit(
        {"target": "kiro", "status": "error", "model": "claude-opus-5", "output": "Error: monthly limit reached"}
    )
    assert "kiro" in server._LIMITS
    note = server._limits_note()
    assert "Kiro worker" in note and "claude-opus-5" in note

    # The note survives a bridge restart, which is when the caller actually reads it.
    monkeypatch.setattr(server, "_LIMITS", server._load_limits())
    assert "Kiro worker" in server._limits_note()

    server._LIMITS["kiro"]["until"] = time.time() - 1
    assert server._limits_note() == ""
    # An expired note is dropped on load instead of lingering in the file forever.
    server.LIMITS_PATH.write_text(json.dumps(server._LIMITS), encoding="utf-8")
    assert server._load_limits() == {}
