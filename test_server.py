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
    sys.stderr.write("boom")
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


def fake(*extra: str, max_parallel: int = 3) -> str:
    name = f"fake{len(server.TARGETS)}"
    server.TARGETS[name] = server.Target(
        label="Fake",
        build=lambda prompt, mode, session_id: [
            sys.executable,
            str(FAKE),
            *extra,
            f"--mode={mode}",
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


def test_cancel_task_stops_a_running_task():
    first = server.delegate(fake("--sleep", "30"), "x", background=True)
    time.sleep(0.5)
    started = time.monotonic()
    assert server.cancel_task(first["task_id"])["status"] == "cancelled"
    assert server.check_task(first["task_id"], wait_s=10)["status"] == "cancelled"
    assert time.monotonic() - started < 10


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
        deadline = time.monotonic() + 15
        while len(written) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        body = json.loads(written[1]["result"]["content"][0]["text"])
        assert written[1]["id"] == 1 and body["status"] == "ok"
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
    assert {"ask_codex", "ask_claude", "ask_kiro", "ask_gemini", "ask_opencode", "check_task", "cancel_task", "list_shared_skills"} <= set(tools)
    props = tools["ask_codex"]["inputSchema"]["properties"]
    assert {"prompt", "skill", "session_id", "mode", "background", "worktree", "timeout_s"} <= set(props)


def test_codex_command_maps_mode_and_resume():
    build = server.TARGETS["codex"].build
    assert 'sandbox_mode="read-only"' in build("p", "read_only", None)
    assert 'sandbox_mode="workspace-write"' in build("p", "write", None)
    resumed = build("p", "read_only", "abc")
    assert resumed[:3] == ["codex", "exec", "resume"] and "abc" in resumed and resumed[-1] == "-"


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
