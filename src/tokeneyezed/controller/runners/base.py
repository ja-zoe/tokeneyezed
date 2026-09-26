"""What every headless-agent runner shares: isolation, the attempt's lifecycle, and git.

A runner subclass supplies only what differs per agent: its command line, its config-dir variable,
any files it needs before launch, and where its blocked tool calls are recorded. Everything here
is agent-agnostic (docs/contracts.md, "Agent adapter"). The harness (not the agent) commits after
every attempt, so each attempt has a commit to score and to reset to.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tokeneyezed.ports import AttemptKilled, AttemptResult

HARNESS_ROOT = Path(__file__).resolve().parents[4]
GRACE_SECONDS = 10  # after SIGTERM, before SIGKILL
GIT_IDENTITY = ["-c", "user.name=tokeneyezed", "-c", "user.email=harness@tokeneyezed.invalid"]
# The operator's own agent and harness variables never reach the agent (e.g. when the harness is
# launched from a Claude Code or Codex session); each runner sets exactly the ones it needs.
STRIPPED_ENV_PREFIXES = ("CLAUDE", "CODEX_", "TOKENEYEZED_")


class IsolationError(ValueError):
    """The runner's paths would let the agent see or edit what it must not."""


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


@dataclass(frozen=True)
class RunnerPaths:
    workspace: Path  # the task repo the agent works in
    runs_dir: Path  # transcripts, hook settings, spools
    config_dir: Path  # the agent's dedicated config dir (CLAUDE_CONFIG_DIR, CODEX_HOME)
    harness_root: Path = HARNESS_ROOT

    def resolved(self) -> RunnerPaths:
        return RunnerPaths(
            *(p.expanduser().resolve() for p in (self.workspace, self.runs_dir, self.config_dir)),
            harness_root=self.harness_root.resolve(),
        )


def check_isolation(paths: RunnerPaths) -> RunnerPaths:
    """Refuse paths that break I2 or I6; return them resolved."""
    p = paths.resolved()
    if not (p.workspace / ".git").exists():
        raise IsolationError(f"workspace {p.workspace} is not a git repository")
    if _inside(p.workspace, p.harness_root) or _inside(p.harness_root, p.workspace):
        raise IsolationError(
            f"workspace {p.workspace} overlaps the harness repo {p.harness_root}: the agent would "
            "load our instructions and could reach the scorer and hidden splits (I2)"
        )
    for name, path in (("runs dir", p.runs_dir), ("agent config dir", p.config_dir)):
        if _inside(path, p.workspace):
            raise IsolationError(
                f"{name} {path} is inside the workspace, where the agent can edit it (I6)"
            )
    return p


class HeadlessRunner:
    agent = ""  # set by each subclass
    config_env = ""  # the variable that points the agent at its dedicated config dir

    def __init__(
        self,
        paths: RunnerPaths,
        *,
        model: str,
        timebox_seconds: float,
        observer_url: str,
        observer_token: str,
        python: str = sys.executable,
    ) -> None:
        if not model:
            raise ValueError(f"no pinned model: set [models].{self.agent} in configs/base.toml")
        if not observer_token:
            raise ValueError("TOKENEYEZED_OBSERVER_TOKEN is not set (must match the observer's)")
        self.paths = check_isolation(paths)
        self.model = model
        self.timebox_seconds = timebox_seconds
        self.observer_url = observer_url
        self.observer_token = observer_token
        self.python = python

    # -- what each agent supplies --------------------------------------------------------------

    def command(self, prompt: str, attempt_dir: Path, attempt_id: str) -> list[str]:
        raise NotImplementedError

    def prepare(self, attempt_dir: Path, attempt_id: str) -> None:
        """Write anything the agent needs before launch (e.g. a hook settings file)."""

    def blocked_calls(self, transcript: Path, attempt_id: str) -> tuple[str, ...]:
        """Tool calls the observer's pre-gate blocked during this attempt."""
        return ()

    def shim_command(self) -> str:
        return f"{shlex.quote(self.python)} -m tokeneyezed.observer.shim"

    # -- git in the task workspace ------------------------------------------------------------

    def _git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *GIT_IDENTITY, *args],
            cwd=self.paths.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def _root_commit(self) -> str:
        return self._git("rev-list", "--max-parents=0", "HEAD").splitlines()[-1]

    def reset_workspace(self, commit: str | None) -> None:
        self._git("reset", "--hard", commit or self._root_commit())
        self._git("clean", "-fd")

    # -- one attempt ------------------------------------------------------------------------

    def environment(self, session_id: str, attempt_id: str, intent: str, spool: Path) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(STRIPPED_ENV_PREFIXES)}
        env.update(
            {
                self.config_env: str(self.paths.config_dir),
                "TOKENEYEZED_AGENT": self.agent,
                "TOKENEYEZED_SESSION_ID": session_id,
                "TOKENEYEZED_ATTEMPT_ID": attempt_id,
                "TOKENEYEZED_INTENT": intent,
                "TOKENEYEZED_OBSERVER_URL": self.observer_url,
                "TOKENEYEZED_OBSERVER_TOKEN": self.observer_token,
                "TOKENEYEZED_OBSERVER_SPOOL": str(spool),
            }
        )
        return env

    def _wait(self, proc: subprocess.Popen) -> bool:
        """Wait for the agent; True if the timebox expired."""
        try:
            proc.wait(timeout=self.timebox_seconds)
            return False
        except subprocess.TimeoutExpired:
            return True

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        """SIGTERM the agent's whole process group, then SIGKILL if it lingers."""
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except ProcessLookupError:
            pass

    def run(self, *, session_id: str, attempt_id: str, brief: str, intent: str) -> AttemptResult:
        attempt_dir = self.paths.runs_dir / session_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        self.prepare(attempt_dir, attempt_id)
        transcript = attempt_dir / f"{attempt_id}.jsonl"
        spool = attempt_dir / f"{attempt_id}.spool.jsonl"
        prompt = f"{brief}\n\nYour intent for this attempt: {intent}"
        base = self._git("rev-parse", "HEAD")

        with transcript.open("w") as out, (attempt_dir / f"{attempt_id}.stderr").open("w") as err:
            proc = subprocess.Popen(
                self.command(prompt, attempt_dir, attempt_id),
                cwd=self.paths.workspace,
                env=self.environment(session_id, attempt_id, intent, spool),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,  # its own process group, so we can stop all of it
            )
            try:
                timed_out = self._wait(proc)
            except BaseException as exc:  # Ctrl-C / SIGTERM on the controller
                self._stop(proc)
                raise AttemptKilled(f"controller interrupted during {attempt_id}") from exc
            if timed_out:
                self._stop(proc)

        commit, diff_summary = self._commit(attempt_id, intent, base)
        return AttemptResult(
            agent=self.agent,
            commit=commit,
            diff_summary=diff_summary + ("\n(stopped at the timebox)" if timed_out else ""),
            exit_code=proc.returncode,
            blocked=self.blocked_calls(transcript, attempt_id),
        )

    def _commit(self, attempt_id: str, intent: str, base: str) -> tuple[str, str]:
        self._git("add", "-A")
        self._git("commit", "--allow-empty", "-q", "-m", f"attempt {attempt_id}: {intent}")
        commit = self._git("rev-parse", "HEAD")
        stat = self._git("diff", "--stat=100", base, commit)
        return commit, stat or "no changes"
