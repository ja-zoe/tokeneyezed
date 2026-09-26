"""The Codex runner: one headless `codex exec` attempt, with the observer in its hooks.

Spec: docs/specs/codex-runner.md. Established by probes against codex-cli 0.157.1:
- Hooks must be passed as `-c hooks.<Event>=[...]` overrides; a project `.codex/hooks.json` is not
  loaded by `codex exec`. Hooks fire for shell calls (`Bash`) and direct edits (`apply_patch`).
- Code mode runs commands inside a JavaScript `exec` tool, out of the hooks' sight: it is disabled.
- Codex's OS sandbox cannot run on hosts without unprivileged user namespaces, so the attempt runs
  unsandboxed; the observer's pre-gate and the runner's isolation checks are the containment.
- The `--json` stream does not record hook blocks, so blocked calls come from the observer's
  audit log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tokeneyezed.controller.runners.base import HeadlessRunner, IsolationError, RunnerPaths, _inside


class CodexRunner(HeadlessRunner):
    agent = "codex"
    config_env = "CODEX_HOME"

    def __init__(
        self,
        paths: RunnerPaths,
        *,
        model: str,
        timebox_seconds: float,
        observer_url: str,
        observer_token: str,
        audit_log: Path | None = None,
        codex_bin: str = "codex",
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
        if not (self.paths.config_dir / "auth.json").exists():
            home = self.paths.config_dir
            raise ValueError(
                f"CODEX_HOME {home} has no login: run "
                f"`mkdir -p {home} && CODEX_HOME={home} codex login`"
            )
        self.audit_log = audit_log.expanduser().resolve() if audit_log else None
        if self.audit_log and _inside(self.audit_log, self.paths.workspace):
            raise IsolationError(f"audit log {self.audit_log} is inside the workspace (I6)")
        self.codex_bin = codex_bin

    def hook_overrides(self) -> list[str]:
        hook = f'[{{hooks=[{{type="command",command={json.dumps(self.shim_command())}}}]}}]'
        overrides = []
        for event in ("PreToolUse", "PostToolUse", "Stop"):
            overrides += ["-c", f"hooks.{event}={hook}"]
        return overrides

    def command(self, prompt: str, attempt_dir: Path, attempt_id: str) -> list[str]:
        cmd = [self.codex_bin, "exec", "--json", "--ephemeral", "--skip-git-repo-check"]
        cmd += ["--cd", str(self.paths.workspace), "--model", self.model]
        cmd += ["--disable", "code_mode"]  # its JS exec tool runs commands where hooks can't see
        # A clean agent: without these, the account's ChatGPT apps attach as tools (in testing:
        # financial-account and deployment tools), and Codex keeps memories outside the harness.
        cmd += ["--disable", "apps", "--disable", "memories", "--disable", "plugins"]
        cmd += ["-c", 'web_search="disabled"']  # it could fetch an existing implementation
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]  # no usable sandbox; see docstring
        cmd += ["--dangerously-bypass-hook-trust", *self.hook_overrides()]
        return [*cmd, prompt]

    def blocked_calls(self, transcript: Path, attempt_id: str) -> tuple[str, ...]:
        """This attempt's `block:` verdicts from the observer's audit log (JSONL events)."""
        if not self.audit_log or not self.audit_log.exists():
            return ()
        calls = []
        for raw in self.audit_log.read_text().splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            verdict = str(event.get("verdict") or "")
            if event.get("attempt_id") != attempt_id or not verdict.startswith("block"):
                continue
            called = event.get("input") or {}
            what = str(called.get("command") or called.get("file_path") or event.get("tool"))
            what = what.splitlines()[0][:120] if what else "tool call"
            calls.append(f"{what}  ({verdict.removeprefix('block:').strip()})")
        return tuple(calls)
