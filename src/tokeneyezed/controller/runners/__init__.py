"""AttemptRunner implementations, one per coding agent (docs/contracts.md, "Agent adapter")."""

from __future__ import annotations

import os
from pathlib import Path

from tokeneyezed.controller.config import RunConfig
from tokeneyezed.controller.runners.claude import ClaudeRunner, RunnerPaths


def claude_runner_from_env(config: RunConfig) -> ClaudeRunner:
    """Build the Claude runner from configs/ (shared behavior) and .env (machine-specific paths)."""
    workspace = os.environ.get("TOKENEYEZED_WORKSPACE")
    if not workspace:
        raise SystemExit("TOKENEYEZED_WORKSPACE is not set: the task repo the agent works in")
    paths = RunnerPaths(
        workspace=Path(workspace),
        runs_dir=Path(os.environ.get("TOKENEYEZED_RUNS_DIR") or "~/.tokeneyezed/runs"),
        config_dir=Path(os.environ.get("TOKENEYEZED_CLAUDE_CONFIG_DIR") or "~/.claude-tokeneyezed"),
    )
    return ClaudeRunner(
        paths,
        model=config.model,
        max_turns=config.max_turns,
        timebox_seconds=config.timebox_minutes * 60,
        allowed_tools=config.allowed_tools,
        observer_url=os.environ.get("TOKENEYEZED_OBSERVER_URL") or "http://127.0.0.1:8765/event",
        observer_token=os.environ.get("TOKENEYEZED_OBSERVER_TOKEN", ""),
    )
