"""A stand-in `claude` executable and a scratch task workspace, for runner tests without an agent.

The fake records its argv, cwd, and environment, then behaves per FAKE_CLAUDE_MODE:
  edit  write renderer.py in its cwd, print stream-json lines, exit 0
  hang  start a child `sleep`, record its pid, and sleep (for the timebox and kill paths)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tokeneyezed.controller.runners.claude import ClaudeRunner, RunnerPaths

FAKE = r"""
import json, os, subprocess, sys, time
record_path = os.environ.get("FAKE_CLAUDE_RECORD")
if record_path:
    with open(record_path, "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}, f)
mode = os.environ.get("FAKE_CLAUDE_MODE", "edit")
if mode == "edit":
    with open("renderer.py", "w") as f:
        f.write("def render(md):\n    return md\n")
    print(json.dumps({"type": "system", "subtype": "init"}))
    print(json.dumps({"type": "result", "subtype": "success"}))
elif mode == "hang":
    child = subprocess.Popen(["sleep", "60"])
    with open(record_path + ".child", "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
"""


def make_fake_claude(directory: Path) -> Path:
    path = directory / "claude"
    path.write_text(f"#!{sys.executable}\n{FAKE}")
    path.chmod(0o755)
    return path


def make_workspace(directory: Path) -> Path:
    ws = directory / "workspace"
    ws.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "task: initial state"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args], cwd=ws, check=True
        )
    return ws


def make_runner(directory: Path, timebox_seconds: float = 30) -> ClaudeRunner:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bin").mkdir(exist_ok=True)
    paths = RunnerPaths(
        workspace=make_workspace(directory),
        runs_dir=directory / "runs",
        config_dir=directory / "claude-config",
    )
    return ClaudeRunner(
        paths,
        model="test-model",
        max_turns=5,
        timebox_seconds=timebox_seconds,
        allowed_tools=("Bash(python *)", "Bash(pytest *)"),
        observer_url="http://127.0.0.1:8765/event",
        observer_token="test-token",
        claude_bin=str(make_fake_claude(directory / "bin")),
    )
