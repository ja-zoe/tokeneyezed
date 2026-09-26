"""A stand-in `codex` executable for runner tests without an agent (see fake_claude.py).

Records its argv, cwd, and environment, then behaves per FAKE_CODEX_MODE:
  edit  write renderer.py in its cwd, print --json-style events, exit 0
  hang  start a child `sleep`, record its pid, and sleep
"""

from __future__ import annotations

import sys
from pathlib import Path

from fake_claude import make_workspace

from tokeneyezed.controller.runners.base import RunnerPaths
from tokeneyezed.controller.runners.codex import CodexRunner

FAKE = r"""
import json, os, subprocess, sys, time
record_path = os.environ.get("FAKE_CODEX_RECORD")
if record_path:
    with open(record_path, "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}, f)
mode = os.environ.get("FAKE_CODEX_MODE", "edit")
if mode == "edit":
    with open("renderer.py", "w") as f:
        f.write("def render(md):\n    return md\n")
    print(json.dumps({"type": "thread.started"}))
    print(json.dumps({"type": "turn.completed"}))
elif mode == "hang":
    child = subprocess.Popen(["sleep", "60"])
    with open(record_path + ".child", "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
"""


def make_codex_runner(directory: Path, timebox_seconds: float = 30) -> CodexRunner:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bin").mkdir(exist_ok=True)
    fake = directory / "bin" / "codex"
    fake.write_text(f"#!{sys.executable}\n{FAKE}")
    fake.chmod(0o755)
    home = directory / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text("{}")  # stands in for a login
    return CodexRunner(
        RunnerPaths(
            workspace=make_workspace(directory), runs_dir=directory / "runs", config_dir=home
        ),
        model="test-codex-model",
        timebox_seconds=timebox_seconds,
        observer_url="http://127.0.0.1:8765/event",
        observer_token="test-token",
        audit_log=directory / "audit.jsonl",
        codex_bin=str(fake),
    )
