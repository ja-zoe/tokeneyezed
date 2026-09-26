"""The Codex runner's merge gate (docs/specs/codex-runner.md), on a fake `codex` binary."""

import json
import os
import subprocess
import time
import tomllib

import pytest
from fake_codex import make_codex_runner

from tokeneyezed.controller.runners.base import IsolationError, RunnerPaths
from tokeneyezed.controller.runners.codex import CodexRunner
from tokeneyezed.ports import AttemptKilled


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    return make_codex_runner(tmp_path)


def record(tmp_path):
    return json.loads((tmp_path / "record.json").read_text())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(stat.stdout.strip()) and not stat.stdout.strip().startswith("Z")


def test_command_and_environment(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/home/operator/.codex")  # the operator's own Codex
    monkeypatch.setenv("CODEX_THREAD_ID", "operator-thread")
    runner.run(session_id="s", attempt_id="s-001", brief="Build it.", intent="Add a renderer")
    argv, env = record(tmp_path)["argv"], record(tmp_path)["env"]

    assert argv[:1] == ["exec"] and "Add a renderer" in argv[-1]
    assert {"--json", "--ephemeral", "--dangerously-bypass-hook-trust"} <= set(argv)
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"code_mode", "apps", "memories", "plugins"} <= disabled
    assert argv[argv.index("--model") + 1] == "test-codex-model"
    assert argv[argv.index("--cd") + 1] == str(runner.paths.workspace)

    # Each hook override must parse as TOML and point at the shim, or the observer is silently off.
    overrides = [tomllib.loads(argv[i + 1]) for i, a in enumerate(argv) if a == "-c"]
    assert {"web_search": "disabled"} in overrides
    hooks = {}
    for override in overrides:
        hooks.update(override.get("hooks", {}))
    assert set(hooks) == {"PreToolUse", "PostToolUse", "Stop"}
    for groups in hooks.values():
        assert groups[0]["hooks"][0] == {"type": "command", "command": runner.shim_command()}

    assert env["CODEX_HOME"] == str(runner.paths.config_dir)
    assert "CODEX_THREAD_ID" not in env
    assert env["TOKENEYEZED_AGENT"] == "codex" and env["TOKENEYEZED_ATTEMPT_ID"] == "s-001"
    assert env["TOKENEYEZED_OBSERVER_TOKEN"] == "test-token"


def test_run_commits_and_reads_blocks_for_this_attempt_only(runner, tmp_path):
    events = [
        {
            "attempt_id": "s-001",
            "tool": "bash",
            "input": {"command": "pip install mistune"},
            "verdict": "block: honeypot: forbidden",
        },
        {
            "attempt_id": "s-001",
            "tool": "bash",
            "input": {"command": "pytest -q"},
            "verdict": "allow",
        },
        {
            "attempt_id": "s-000",
            "tool": "bash",
            "input": {"command": "rm -rf /"},
            "verdict": "block: destructive",
        },
    ]
    runner.audit_log.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")

    result = runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")

    assert result.agent == "codex" and "renderer.py" in result.diff_summary
    assert result.blocked == ("pip install mistune  (honeypot: forbidden)",)


def test_no_audit_log_means_no_blocked_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    runner = make_codex_runner(tmp_path)
    runner.audit_log = None
    assert runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i").blocked == ()


def test_timebox_and_kill_stop_the_whole_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_CODEX_MODE", "hang")
    runner = make_codex_runner(tmp_path / "a", timebox_seconds=1)
    result = runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    assert "stopped at the timebox" in result.diff_summary
    assert not alive(int((tmp_path / "record.json.child").read_text()))

    runner = make_codex_runner(tmp_path / "b")
    (tmp_path / "record.json.child").unlink()

    def interrupted_wait(proc):
        deadline = time.monotonic() + 5
        while not (tmp_path / "record.json.child").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    runner._wait = interrupted_wait
    with pytest.raises(AttemptKilled):
        runner.run(session_id="s", attempt_id="s-002", brief="b", intent="i")
    assert not alive(int((tmp_path / "record.json.child").read_text()))


def test_reset_workspace(runner):
    ws = runner.paths.workspace
    first = runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    (ws / "renderer.py").write_text("half-finished\n")
    (ws / "scratch.py").write_text("untracked\n")
    runner.reset_workspace(first.commit)
    assert (ws / "renderer.py").read_text().startswith("def render")
    assert not (ws / "scratch.py").exists()


def test_refuses_missing_login_and_audit_log_in_workspace(tmp_path):
    runner = make_codex_runner(tmp_path)
    common = dict(model="m", timebox_seconds=1, observer_url="u", observer_token="t")
    paths = runner.paths
    (paths.config_dir / "auth.json").unlink()
    with pytest.raises(ValueError, match="no login"):
        CodexRunner(paths, **common)
    (paths.config_dir / "auth.json").write_text("{}")
    with pytest.raises(IsolationError, match="I6"):
        CodexRunner(paths, audit_log=paths.workspace / "audit.jsonl", **common)
    with pytest.raises(IsolationError, match="I6"):
        CodexRunner(
            RunnerPaths(paths.workspace, paths.runs_dir, paths.workspace / ".codex-home"), **common
        )
