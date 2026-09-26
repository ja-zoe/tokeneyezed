"""AttemptRunner implementations, one per coding agent (docs/contracts.md, "Agent adapter")."""

from __future__ import annotations

import os
from pathlib import Path

from tokeneyezed.controller.config import RunConfig, agent_model
from tokeneyezed.controller.runners.base import HeadlessRunner, RunnerPaths
from tokeneyezed.controller.runners.claude import ClaudeRunner
from tokeneyezed.controller.runners.codex import CodexRunner

CONFIG_DIRS = {  # agent -> (env var for its dedicated config dir, default)
    "claude": ("TOKENEYEZED_CLAUDE_CONFIG_DIR", "~/.claude-tokeneyezed"),
    "codex": ("TOKENEYEZED_CODEX_HOME", "~/.codex-tokeneyezed"),
}


def runner_for(agent: str, config: RunConfig) -> HeadlessRunner:
    """Build an agent's runner from configs/ (shared behavior) and .env (machine-specific paths)."""
    if agent not in CONFIG_DIRS:
        raise SystemExit(f"no runner for agent {agent!r}; known agents: {', '.join(CONFIG_DIRS)}")
    workspace = os.environ.get("TOKENEYEZED_WORKSPACE")
    if not workspace:
        raise SystemExit("TOKENEYEZED_WORKSPACE is not set: the task repo the agent works in")
    config_var, config_default = CONFIG_DIRS[agent]
    paths = RunnerPaths(
        workspace=Path(workspace),
        runs_dir=Path(os.environ.get("TOKENEYEZED_RUNS_DIR") or "~/.tokeneyezed/runs"),
        config_dir=Path(os.environ.get(config_var) or config_default),
    )
    # Codex can run on OpenRouter instead of a ChatGPT login (a machine choice, like the paths);
    # its model is still pinned in configs/base.toml, as [models].codex_openrouter (I7).
    codex_provider = os.environ.get("TOKENEYEZED_CODEX_PROVIDER") or None
    model_key = f"codex_{codex_provider}" if agent == "codex" and codex_provider else agent
    common = dict(
        model=agent_model(config, model_key),
        timebox_seconds=config.timebox_minutes * 60,
        observer_url=os.environ.get("TOKENEYEZED_OBSERVER_URL") or "http://127.0.0.1:8765/event",
        observer_token=os.environ.get("TOKENEYEZED_OBSERVER_TOKEN", ""),
    )
    if agent == "claude":
        return ClaudeRunner(
            paths, max_turns=config.max_turns, allowed_tools=config.allowed_tools, **common
        )
    audit = os.environ.get("TOKENEYEZED_OBSERVER_AUDIT_LOG")
    return CodexRunner(
        paths, audit_log=Path(audit) if audit else None, provider=codex_provider, **common
    )


def claude_runner_from_env(config: RunConfig) -> ClaudeRunner:
    return runner_for("claude", config)  # type: ignore[return-value]
