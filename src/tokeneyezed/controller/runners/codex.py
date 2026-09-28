"""The Codex runner: one headless `codex exec` attempt, with the observer in its hooks.

Spec: docs/specs/codex-runner.md. Established by probes against codex-cli 0.157.1:
- Hooks must be passed as `-c hooks.<Event>=[...]` overrides; a project `.codex/hooks.json` is not
  loaded by `codex exec`. Hooks fire for shell calls (`Bash`) and direct edits (`apply_patch`).
- Code mode runs commands inside a JavaScript `exec` tool, out of the hooks' sight: it is disabled.
- Codex's OS sandbox cannot run on hosts without unprivileged user namespaces, so the attempt runs
  unsandboxed; the observer's pre-gate and the runner's isolation checks are the containment.
- The `--json` stream does not record hook blocks, so blocked calls come from the observer's
  audit log.

Provider: by default Codex uses the ChatGPT login in CODEX_HOME. With provider="openrouter" it uses
OpenRouter's Responses API instead (OPENROUTER_API_KEY; no login needed). Probed with codex-cli
0.157.1 and openai/gpt-5.3-codex: hooks fire and block as usual, but Codex's edits arrive as a
`Bash` call running `apply_patch <<'PATCH'` rather than as the `apply_patch` tool, so the observer
checks them as shell commands (the pre-gate still blocks protected targets and forbidden imports).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tokeneyezed.controller.runners.base import HeadlessRunner, IsolationError, RunnerPaths, _inside

# Codex model providers the runner can use besides the ChatGPT login, as `-c` config overrides.
PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "wire_api": "responses",  # codex-cli 0.157 dropped "chat"
    },
}


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
        provider: str | None = None,  # None: the ChatGPT login; or a key of PROVIDERS
    ) -> None:
        super().__init__(
            paths,
            model=model,
            timebox_seconds=timebox_seconds,
            observer_url=observer_url,
            observer_token=observer_token,
            python=python,
        )
        if provider is not None and provider not in PROVIDERS:
            raise ValueError(f"unknown Codex provider {provider!r}; known: {', '.join(PROVIDERS)}")
        self.provider = provider
        if provider is not None:
            key = PROVIDERS[provider]["env_key"]
            if not os.environ.get(key):
                raise ValueError(f"Codex provider {provider!r} needs {key} in the environment")
            self.paths.config_dir.mkdir(parents=True, exist_ok=True)  # an empty, clean CODEX_HOME
        elif not (self.paths.config_dir / "auth.json").exists():
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

    def provider_overrides(self) -> list[str]:
        if self.provider is None:
            return []
        settings = PROVIDERS[self.provider]
        overrides = ["-c", f"model_provider={json.dumps(self.provider)}"]
        for key, value in settings.items():
            overrides += ["-c", f"model_providers.{self.provider}.{key}={json.dumps(value)}"]
        # Codex needs the key; the shell commands the agent runs must never see it. The exclusion
        # alone isn't enough: shell snapshots replay Codex's whole starting environment into every
        # command (probed on 0.157.1: the key stayed visible until snapshots were disabled).
        exclude = json.dumps([settings["env_key"]])
        overrides += ["-c", f"shell_environment_policy.exclude={exclude}"]
        return [*overrides, "--disable", "shell_snapshot"]

    def environment(self, session_id: str, attempt_id: str, intent: str, spool: Path) -> dict:
        env = super().environment(session_id, attempt_id, intent, spool)
        if self.provider is not None:  # the base strips it; the Codex process itself needs it
            key = PROVIDERS[self.provider]["env_key"]
            env[key] = os.environ[key]
        return env

    def command(self, prompt: str, attempt_dir: Path, attempt_id: str) -> list[str]:
        cmd = [self.codex_bin, "exec", "--json", "--ephemeral", "--skip-git-repo-check"]
        cmd += ["--cd", str(self.paths.workspace), "--model", self.model]
        cmd += self.provider_overrides()
        cmd += ["--disable", "code_mode"]  # its JS exec tool runs commands where hooks can't see
        # A clean agent: without these, the account's ChatGPT apps attach as tools (in testing:
        # financial-account and deployment tools), and Codex keeps memories outside the harness.
        cmd += ["--disable", "apps", "--disable", "memories", "--disable", "plugins"]
        cmd += ["-c", 'web_search="disabled"']  # it could fetch an existing implementation
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]  # no usable sandbox; see docstring
        cmd += ["--dangerously-bypass-hook-trust", *self.hook_overrides()]
        return [*cmd, prompt]

    def blocked_calls(self, transcript: Path, attempt_id: str) -> tuple[str, ...]:
        """This attempt's `block:` verdicts: from the observer's audit log if it writes one
        (--audit-log), otherwise from Atlas's events collection (--mongo)."""
        return format_blocks(self._events(attempt_id), attempt_id)

    def _events(self, attempt_id: str) -> list[dict]:
        if self.audit_log:
            if not self.audit_log.exists():
                return []
            events = []
            for raw in self.audit_log.read_text().splitlines():
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
            return events
        try:
            from tokeneyezed.data.db import get_db

            return list(get_db().events.find({"attempt_id": attempt_id}, {"_id": 0}))
        except Exception:  # no Atlas configured: the feed just shows no BLOCKED lines
            return []


def format_blocks(events: list[dict], attempt_id: str) -> tuple[str, ...]:
    """'<command or path>  (<reason>)' for each of this attempt's blocked tool calls."""
    calls = []
    for event in events:
        verdict = str(event.get("verdict") or "")
        if event.get("attempt_id") != attempt_id or not verdict.startswith("block"):
            continue
        called = event.get("input") or {}
        what = str(called.get("command") or called.get("file_path") or event.get("tool"))
        what = what.splitlines()[0][:120] if what else "tool call"
        calls.append(f"{what}  ({verdict.removeprefix('block:').strip()})")
    return tuple(calls)
