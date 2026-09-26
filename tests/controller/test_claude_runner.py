"""The Claude runner's merge gate (docs/specs/claude-runner.md), on a fake `claude` binary."""

import json
import os
import subprocess
import time

import pytest
from fake_claude import make_runner

from tokeneyezed.controller.runners.claude import (
    ClaudeRunner,
    IsolationError,
    RunnerPaths,
    check_isolation,
)
from tokeneyezed.ports import AttemptKilled


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.json"))
    return make_runner(tmp_path)


def record(tmp_path):
    return json.loads((tmp_path / "record.json").read_text())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it counts as dead.
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(stat.stdout.strip()) and not stat.stdout.strip().startswith("Z")


def test_command_and_environment(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")  # the operator's own Claude Code session
    monkeypatch.setenv("TOKENEYEZED_ATTEMPT_ID", "stale")
    runner.run(session_id="s", attempt_id="s-001", brief="Build it.", intent="Add a renderer")
    argv, env = record(tmp_path)["argv"], record(tmp_path)["env"]

    assert "--bare" not in argv
    for flag, value in [
        ("--model", "test-model"),
        ("--max-turns", "5"),
        ("--permission-mode", "acceptEdits"),
        ("--permission-prompts", "none"),
        ("--output-format", "stream-json"),
    ]:
        assert argv[argv.index(flag) + 1] == value
    assert {"--verbose", "--include-hook-events"} <= set(argv)
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--output-format")]
    assert allowed == ["Bash(python *)", "Bash(pytest *)"]
    assert "Add a renderer" in argv[argv.index("-p") + 1]

    settings = runner.paths.runs_dir / "s" / "s-001.settings.json"
    assert argv[argv.index("--settings") + 1] == str(settings)
    hooks = json.loads(settings.read_text())["hooks"]
    assert {"PreToolUse", "PostToolUse", "Stop"} <= set(hooks)
    assert "-m tokeneyezed.observer.shim" in hooks["PreToolUse"][0]["hooks"][0]["command"]

    assert record(tmp_path)["cwd"] == str(runner.paths.workspace)
    assert env["CLAUDE_CONFIG_DIR"] == str(runner.paths.config_dir)
    assert env["TOKENEYEZED_SESSION_ID"] == "s" and env["TOKENEYEZED_ATTEMPT_ID"] == "s-001"
    assert env["TOKENEYEZED_OBSERVER_TOKEN"] == "test-token"
    assert "CLAUDECODE" not in env


def test_run_commits_the_agents_edits(runner, tmp_path):
    result = runner.run(session_id="s", attempt_id="s-001", brief="b", intent="Add a renderer")

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=runner.paths.workspace, capture_output=True, text=True
    ).stdout.strip()
    assert result.commit == head and result.agent == "claude" and result.exit_code == 0
    assert "renderer.py" in result.diff_summary
    transcript = runner.paths.runs_dir / "s" / "s-001.jsonl"
    assert [json.loads(line)["type"] for line in transcript.read_text().splitlines()] == [
        "system",
        "result",
    ]


def test_timebox_stops_a_hung_agent_and_still_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    runner = make_runner(tmp_path, timebox_seconds=1)

    result = runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")

    assert "stopped at the timebox" in result.diff_summary
    child = int((tmp_path / "record.json.child").read_text())
    assert not alive(child)  # the agent's whole process group was stopped


def test_interrupted_controller_raises_killed_and_leaves_no_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    runner = make_runner(tmp_path)

    def interrupted_wait(proc):
        deadline = time.monotonic() + 5
        while not (tmp_path / "record.json.child").exists() and time.monotonic() < deadline:
            time.sleep(0.05)  # let the fake agent start its child first
        raise KeyboardInterrupt

    runner._wait = interrupted_wait
    with pytest.raises(AttemptKilled):
        runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    child = int((tmp_path / "record.json.child").read_text())
    assert not alive(child)


def test_reset_workspace(runner):
    ws = runner.paths.workspace
    first = runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    (ws / "renderer.py").write_text("half-finished edit\n")
    (ws / "scratch.py").write_text("untracked\n")

    runner.reset_workspace(first.commit)
    assert (ws / "renderer.py").read_text().startswith("def render")
    assert not (ws / "scratch.py").exists()

    runner.reset_workspace(None)  # the task's initial state
    assert not (ws / "renderer.py").exists()


def test_isolation_refuses_unsafe_paths(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    ok = RunnerPaths(workspace=ws, runs_dir=tmp_path / "runs", config_dir=tmp_path / "cfg")
    check_isolation(ok)

    with pytest.raises(IsolationError, match="not a git repository"):
        check_isolation(RunnerPaths(tmp_path / "plain", tmp_path / "runs", tmp_path / "cfg"))
    with pytest.raises(IsolationError, match="I2"):
        check_isolation(RunnerPaths(ws, tmp_path / "runs", tmp_path / "cfg", harness_root=tmp_path))
    with pytest.raises(IsolationError, match="I6"):
        check_isolation(RunnerPaths(ws, ws / "runs", tmp_path / "cfg"))
    with pytest.raises(IsolationError, match="I6"):
        check_isolation(RunnerPaths(ws, tmp_path / "runs", ws / ".claude-config"))


def test_runner_needs_a_pinned_model_and_token(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    paths = RunnerPaths(ws, tmp_path / "runs", tmp_path / "cfg")
    common = dict(max_turns=1, timebox_seconds=1, allowed_tools=(), observer_url="u")
    with pytest.raises(ValueError, match="model"):
        ClaudeRunner(paths, model="", observer_token="t", **common)
    with pytest.raises(ValueError, match="TOKEN"):
        ClaudeRunner(paths, model="m", observer_token="", **common)
