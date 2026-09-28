"""The Codex runner's merge gate (docs/specs/codex-runner.md), on a fake `codex` binary."""

import json
import os
import subprocess
import time
import tomllib
from pathlib import Path

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


def test_blocked_calls_come_from_atlas_events_without_an_audit_log(runner, monkeypatch):
    # With the observer storing events in Atlas (--mongo) there is no audit log to read.
    import tokeneyezed.data.db as db_module

    queries = []

    class Events:
        def find(self, query, projection):
            queries.append(query)
            return [
                {
                    "attempt_id": "s-001",
                    "tool": "bash",
                    "input": {"command": "pip install mistune"},
                    "verdict": "block: honeypot",
                },
                {
                    "attempt_id": "s-001",
                    "tool": "bash",
                    "input": {"command": "ls"},
                    "verdict": "allow",
                },
            ]

    monkeypatch.setattr(db_module, "get_db", lambda: type("DB", (), {"events": Events()})())
    runner.audit_log = None
    assert runner.blocked_calls(Path("unused"), "s-001") == ("pip install mistune  (honeypot)",)
    assert queries == [{"attempt_id": "s-001"}]


def openrouter_runner(tmp_path, monkeypatch) -> CodexRunner:
    base = make_codex_runner(tmp_path)
    home = tmp_path / "openrouter-home"  # no auth.json: OpenRouter needs no ChatGPT login
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    return CodexRunner(
        RunnerPaths(base.paths.workspace, base.paths.runs_dir, home),
        model="openai/gpt-5.3-codex",
        timebox_seconds=30,
        observer_url="http://127.0.0.1:8765/event",
        observer_token="test-token",
        codex_bin=base.codex_bin,
        provider="openrouter",
    )


def test_openrouter_provider_command(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    runner = openrouter_runner(tmp_path, monkeypatch)
    runner.run(session_id="s", attempt_id="s-001", brief="Build it.", intent="Add a renderer")
    argv, env = record(tmp_path)["argv"], record(tmp_path)["env"]

    def merge(into: dict, new: dict) -> None:
        for key, value in new.items():
            if isinstance(value, dict) and isinstance(into.get(key), dict):
                merge(into[key], value)
            else:
                into[key] = value

    overrides: dict = {}
    for i, a in enumerate(argv):
        if a == "-c":
            merge(overrides, tomllib.loads(argv[i + 1]))  # every override must be valid TOML
    assert overrides["model_provider"] == "openrouter"
    assert overrides["model_providers"]["openrouter"] == {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "wire_api": "responses",
    }
    assert argv[argv.index("--model") + 1] == "openai/gpt-5.3-codex"
    # everything that keeps the agent clean and hooked is unchanged
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"code_mode", "apps", "memories", "plugins"} <= disabled
    assert set(overrides["hooks"]) == {"PreToolUse", "PostToolUse", "Stop"}
    assert env["OPENROUTER_API_KEY"] == "sk-or-test"
    assert env["CODEX_HOME"] == str(runner.paths.config_dir)


def test_openrouter_needs_the_key_not_a_login(tmp_path, monkeypatch):
    runner = openrouter_runner(tmp_path, monkeypatch)
    assert not (runner.paths.config_dir / "auth.json").exists()  # and that's fine
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        CodexRunner(
            runner.paths,
            model="m",
            timebox_seconds=1,
            observer_url="u",
            observer_token="t",
            provider="openrouter",
        )


def test_default_provider_adds_no_overrides(runner):
    assert runner.provider is None and runner.provider_overrides() == []


def test_unknown_provider_is_rejected(tmp_path):
    base = make_codex_runner(tmp_path)
    with pytest.raises(ValueError, match="unknown Codex provider"):
        CodexRunner(
            base.paths,
            model="m",
            timebox_seconds=1,
            observer_url="u",
            observer_token="t",
            provider="nope",
        )


def test_runner_for_picks_the_pinned_openrouter_model(tmp_path, monkeypatch):
    from pathlib import Path

    from tokeneyezed.controller.config import load_config
    from tokeneyezed.controller.runners import runner_for

    base = make_codex_runner(tmp_path)
    configs = Path(__file__).resolve().parents[2] / "configs"
    config = load_config(configs / "h.toml")
    monkeypatch.setenv("TOKENEYEZED_WORKSPACE", str(base.paths.workspace))
    monkeypatch.setenv("TOKENEYEZED_CODEX_HOME", str(tmp_path / "or-home"))
    monkeypatch.setenv("TOKENEYEZED_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("TOKENEYEZED_CODEX_PROVIDER", "openrouter")
    monkeypatch.setenv("TOKENEYEZED_OBSERVER_TOKEN", "test-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    runner = runner_for("codex", config)
    assert runner.provider == "openrouter"
    assert runner.model == config.models["codex_openrouter"] == "openai/gpt-5.3-codex"


def test_refuses_to_start_when_the_shim_cannot_import(runner, tmp_path, monkeypatch):
    # Hooks that crash are treated as "allow" by the agent, so a broken shim must stop the attempt.
    from tokeneyezed.controller.runners.base import ShimUnavailable

    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    fake_python = tmp_path / "no-harness-python"
    fake_python.write_text(
        "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'tokeneyezed'\" >&2\nexit 1\n"
    )
    fake_python.chmod(0o755)
    runner.python = str(fake_python)
    head = runner._git("rev-parse", "HEAD")
    with pytest.raises(ShimUnavailable, match="fail open"):
        runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    assert not (tmp_path / "record.json").exists()  # the agent never ran
    assert runner._git("rev-parse", "HEAD") == head  # and nothing was committed


def test_harness_secrets_never_reach_the_agent(runner, tmp_path, monkeypatch):
    # The CLI loads .env; the agent has a shell. MONGODB_URI would let it query Atlas (I1, I3).
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://user:secret@cluster")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    env = record(tmp_path)["env"]
    assert not {"MONGODB_URI", "VOYAGE_API_KEY", "OPENROUTER_API_KEY"} & set(env)


def test_openrouter_key_reaches_codex_but_not_its_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(tmp_path / "record.json"))
    runner = openrouter_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://user:secret@cluster")
    runner.run(session_id="s", attempt_id="s-001", brief="b", intent="i")
    argv, env = record(tmp_path)["argv"], record(tmp_path)["env"]
    assert env["OPENROUTER_API_KEY"] == "sk-or-test" and "MONGODB_URI" not in env
    policies = [tomllib.loads(argv[i + 1]) for i, a in enumerate(argv) if a == "-c"]
    assert {"shell_environment_policy": {"exclude": ["OPENROUTER_API_KEY"]}} in policies
    # without this, shell snapshots replay the key into every command anyway
    assert "shell_snapshot" in {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
