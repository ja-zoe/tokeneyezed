"""The Claude Code runner: one headless `claude -p` attempt, with the observer in its hooks.

Spec: docs/specs/claude-runner.md. The lifecycle, isolation, and git are shared (base.py); this
module is only what is specific to Claude Code: its command line, its hook settings file, and
reading blocked calls from its stream-json transcript.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tokeneyezed.controller.runners.base import (
    HeadlessRunner,
    IsolationError,
    RunnerPaths,
    check_isolation,
)
from tokeneyezed.controller.runners.usage import read_claude_usage

__all__ = ["ClaudeRunner", "IsolationError", "RunnerPaths", "blocked_calls", "check_isolation"]

# The only built-in tools the agent gets. No web access (it could fetch an existing CommonMark
# implementation), and the same set for B, H, and H-mem.
AGENT_TOOLS = "Bash,Read,Edit,Write,Grep,Glob"


class ClaudeRunner(HeadlessRunner):
    agent = "claude"
    config_env = "CLAUDE_CONFIG_DIR"

    def __init__(
        self,
        paths: RunnerPaths,
        *,
        model: str,
        max_turns: int,
        timebox_seconds: float,
        allowed_tools: tuple[str, ...],
        observer_url: str,
        observer_token: str,
        claude_bin: str = "claude",
        python: str = sys.executable,
    ) -> None:
        super().__init__(
            paths,
            model=model,
            timebox_seconds=timebox_seconds,
            observer_url=observer_url,
            observer_token=observer_token,
            python=python,
        )
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.claude_bin = claude_bin

    def hook_settings(self) -> dict:
        shim = {"type": "command", "command": self.shim_command()}
        return {
            # Claude Code's own cross-session memory would give every run (the baseline too) a
            # memory outside the harness; the harness's memory must be the only one.
            "autoMemoryEnabled": False,
            "hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": [shim]}],
                "PostToolUse": [{"matcher": "*", "hooks": [shim]}],
                "Stop": [{"hooks": [shim]}],
            },
        }

    def prepare(self, attempt_dir: Path, attempt_id: str) -> None:
        settings = attempt_dir / f"{attempt_id}.settings.json"
        settings.write_text(json.dumps(self.hook_settings(), indent=2))

    def command(self, prompt: str, attempt_dir: Path, attempt_id: str) -> list[str]:
        settings = attempt_dir / f"{attempt_id}.settings.json"
        cmd = [self.claude_bin, "-p", prompt, "--settings", str(settings)]
        cmd += ["--model", self.model, "--max-turns", str(self.max_turns)]
        cmd += ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]
        cmd += ["--allowedTools", *self.allowed_tools]
        # A clean agent: only the coding tools, no MCP servers (the account's claude.ai
        # connectors, e.g. Gmail and Drive, attach otherwise), no skills, nothing persisted.
        cmd += ["--tools", AGENT_TOOLS, "--strict-mcp-config", "--disable-slash-commands"]
        cmd += ["--no-session-persistence", "--no-chrome"]
        cmd += ["--output-format", "stream-json", "--verbose", "--include-hook-events"]
        return cmd

    def blocked_calls(self, transcript: Path, attempt_id: str) -> tuple[str, ...]:
        return blocked_calls(transcript)

    def usage(self, transcript: Path) -> dict[str, int] | None:
        return read_claude_usage(transcript)


def blocked_calls(transcript: Path) -> tuple[str, ...]:
    """Tool calls the pre-gate blocked, read from the stream-json transcript's hook events.

    Format (claude 2.1.283, confirmed in the live smoke test): the assistant's `tool_use` comes
    first, then a `system`/`hook_response` for `PreToolUse:<Tool>`; a block is exit code 2 with the
    reason on stderr. Each block is reported as "<command or path>  (<reason>)".
    """
    calls: list[str] = []
    last_use: dict[str, dict] = {}  # tool name -> input of its most recent tool_use
    for raw in transcript.read_text().splitlines():
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if message.get("type") == "assistant":
            for part in message.get("message", {}).get("content", []):
                if part.get("type") == "tool_use":
                    last_use[part.get("name", "")] = part.get("input") or {}
        elif (
            message.get("subtype") == "hook_response"
            and message.get("hook_event") == "PreToolUse"
            and message.get("exit_code") == 2
        ):
            tool = message.get("hook_name", "").partition(":")[2]
            called = last_use.get(tool, {})
            what = called.get("command") or called.get("file_path") or tool
            reason = (message.get("stderr") or "").strip()
            calls.append(f"{what}  ({reason})" if reason else what)
    return tuple(calls)
