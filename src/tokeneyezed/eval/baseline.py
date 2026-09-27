"""Run B: the naive retry loop — no LangGraph, no observer, no memory, no goals (STEP 3).

For attempt in 1..max_attempts: run the agent on the workspace's task text
(PROMPT.md when present, else the README.md `tokeneyezed workspace init`
writes; plus, from attempt 2 on, one feedback line with the previous visible
pass rate), score with the scorer's attempt mode, and log one attempts-shaped
JSON per attempt to runs/<session_id>/attempts.jsonl. When a Mongo database is
available (MONGODB_URI, or an injected db), each attempt is also opened and
closed in Aaron's `attempts` collection via data/writes.py, with agent="B".

Scoring reads the harness-side copy of the visible split (splits_dir/visible.json,
written by `tokeneyezed split`), so an agent editing its workspace copy cannot
inflate visible_pass — the same rule as the SpecScorer port (eval/scoring.py).

The invocation mirrors the approved runner (docs/specs/claude-runner.md): same
pinned model, --max-turns from the run config, --permission-mode acceptEdits,
--output-format stream-json, a dedicated CLAUDE_CONFIG_DIR, and a per-attempt
wall-clock timebox enforced with SIGTERM on the agent's process group (a
timed-out attempt still counts and is scored as-is). It differs from run H
exactly where the comparison needs it to: no hook settings and no observer env
vars — B is observer-blind. The settings file still pins autoMemoryEnabled to
false, because Claude Code's own cross-session memory would smuggle a memory
into the memoryless runs (same rule as ClaudeRunner.hook_settings).

Comparisons are at equal attempt counts, never wall-clock, so the loop always
runs the full attempt budget from the config (invariant I7: max_attempts,
max_turns, model, and timebox come from configs/base.toml, never from here).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from tokeneyezed.controller.config import RunConfig, agent_model
from tokeneyezed.controller.runners.base import (
    GIT_IDENTITY,
    GRACE_SECONDS,
    STRIPPED_ENV_PREFIXES,
)
from tokeneyezed.controller.runners.claude import AGENT_TOOLS
from tokeneyezed.data import sessions, writes
from tokeneyezed.eval import scorer

FEEDBACK = "Previous attempt passed {:.0%} of visible tests. Improve render.py."


def baseline_settings() -> dict:
    """Claude settings for run B: auto memory off, and nothing else — no hooks."""
    return {"autoMemoryEnabled": False}


def baseline_command(
    claude_bin: str,
    prompt: str,
    model: str,
    max_turns: int,
    allowed_tools: tuple[str, ...],
    settings: Path,
) -> list[str]:
    """The `claude -p` argv: ClaudeRunner.command minus its hook flag (no observer)."""
    cmd = [claude_bin, "-p", prompt, "--settings", str(settings)]
    cmd += ["--model", model, "--max-turns", str(max_turns)]
    cmd += ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    cmd += ["--allowedTools", *allowed_tools]
    cmd += ["--tools", AGENT_TOOLS, "--strict-mcp-config", "--disable-slash-commands"]
    cmd += ["--no-session-persistence", "--no-chrome"]
    cmd += ["--output-format", "stream-json", "--verbose"]
    return cmd


@dataclass(frozen=True)
class BaselinePaths:
    """Where one baseline session reads and writes (all validated by the caller)."""

    workspace: Path  # the task repo the agent works in (a git repo with the task text)
    splits_dir: Path  # harness-side splits: visible.json + validation.json, outside the workspace
    runs_dir: Path  # transcripts, settings, attempts.jsonl under runs_dir/<session_id>/
    config_dir: Path  # the agent's dedicated CLAUDE_CONFIG_DIR


def _git(workspace: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _agent_env(config_dir: Path) -> dict:
    """The agent's env: operator/harness vars stripped, only the config dir set (no observer)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(STRIPPED_ENV_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def run_attempt(
    command: list[str],
    workspace: Path,
    env: dict,
    transcript: Path,
    stderr: Path,
    timebox_seconds: float,
) -> tuple[int | None, bool]:
    """One timeboxed agent run; returns (exit_code, timed_out).

    SIGTERM goes to the agent's whole process group at the timebox, SIGKILL after
    GRACE_SECONDS — the same stop the shared runner base uses. The same stop runs if the
    baseline itself is interrupted (Ctrl-C, SIGTERM): the agent is in its own session, so
    without it the agent kept working, and billing, after the run was stopped.
    """
    with transcript.open("w") as out, stderr.open("w") as err:
        proc = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timebox_seconds)
            return proc.returncode, False
        except subprocess.TimeoutExpired:
            pass
        except BaseException:
            _stop_group(proc)
            raise
        _stop_group(proc)
        return proc.returncode, True


def _stop_group(proc: subprocess.Popen) -> None:
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


def run_baseline(
    config: RunConfig,
    paths: BaselinePaths,
    *,
    claude_bin: str = "claude",
    session_id: str | None = None,
    timebox_seconds: float | None = None,
    db=None,
    embedder=None,
) -> list[dict]:
    """The whole run B; returns the attempt docs it logged (one per attempt).

    `db` is Aaron's Mongo database: pass one to force it, or leave it None to use
    MONGODB_URI when set and skip Mongo entirely otherwise. `timebox_seconds`
    exists for tests and smoke runs only — a real run must take the config's
    timebox_minutes so B, H, and H-mem stay comparable (I7).
    """
    session_id = session_id or f"{config.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    session_dir = paths.runs_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    settings = session_dir / "settings.json"
    settings.write_text(json.dumps(baseline_settings(), indent=2))
    if db is None and os.environ.get("MONGODB_URI"):
        from tokeneyezed.data.db import get_db

        db = get_db()

    model = agent_model(config, config.agent)
    prompt_file = paths.workspace / "PROMPT.md"
    if not prompt_file.exists():
        prompt_file = paths.workspace / "README.md"  # `tokeneyezed workspace init` layout
    base_prompt = prompt_file.read_text().rstrip("\n")
    timebox = config.timebox_minutes * 60 if timebox_seconds is None else timebox_seconds
    env = _agent_env(paths.config_dir)
    jsonl = session_dir / "attempts.jsonl"

    docs: list[dict] = []
    if db is not None:  # the report labels sessions by config.name (B, H, H-mem)
        sessions.start_session(session_id, config=asdict(config), agent=config.agent, db=db)
    try:
        _loop(
            config,
            paths,
            claude_bin,
            session_id,
            session_dir,
            settings,
            db,
            embedder,
            model,
            base_prompt,
            timebox,
            env,
            jsonl,
            docs,
        )
    except KeyboardInterrupt:
        if db is not None:
            sessions.end_session(
                session_id,
                status=sessions.KILLED,
                reason="interrupted",
                attempt_count=len(docs),
                db=db,
            )
        raise
    if db is not None:
        sessions.end_session(
            session_id,
            status=sessions.FINISHED,
            reason="budget spent",
            attempt_count=len(docs),
            db=db,
        )
    return docs


def _loop(
    config,
    paths,
    claude_bin,
    session_id,
    session_dir,
    settings,
    db,
    embedder,
    model,
    base_prompt,
    timebox,
    env,
    jsonl,
    docs,
) -> None:
    previous_visible = None
    parent_attempt = None
    for number in range(1, config.max_attempts + 1):
        attempt_id = f"{session_id}-{number:03d}"
        intent = "naive retry" if previous_visible is None else FEEDBACK.format(previous_visible)
        prompt = (
            base_prompt
            if previous_visible is None
            else (f"{base_prompt}\n\n{FEEDBACK.format(previous_visible)}")
        )
        if db is not None:
            writes.open_attempt(
                session_id=session_id,
                attempt_id=attempt_id,
                number=number,
                goal_id=f"{session_id}:baseline",
                agent="B",
                intent=intent,
                parent_attempt=parent_attempt,
                db=db,
            )

        command = baseline_command(
            claude_bin, prompt, model, config.max_turns, config.allowed_tools, settings
        )
        base = _git(paths.workspace, "rev-parse", "HEAD")
        exit_code, timed_out = run_attempt(
            command,
            paths.workspace,
            env,
            session_dir / f"{attempt_id}.jsonl",
            session_dir / f"{attempt_id}.stderr",
            timebox,
        )
        _git(paths.workspace, "add", "-A")
        _git(paths.workspace, "commit", "--allow-empty", "-q", "-m", f"attempt {attempt_id}")
        commit = _git(paths.workspace, "rev-parse", "HEAD")
        diff_summary = _git(paths.workspace, "diff", "--stat=100", base, commit)
        if timed_out:
            diff_summary += "\n(stopped at the timebox)"

        score = scorer.score_attempt(
            str(paths.splits_dir / "visible.json"),  # the harness-side copy, never the agent's
            str(paths.splits_dir / "validation.json"),
            str(paths.workspace),
        )
        doc = {
            "session_id": session_id,
            "attempt_id": attempt_id,
            "number": number,
            "agent": "B",
            "intent": intent,
            "parent_attempt": parent_attempt,
            "commit": commit,
            "diff_summary": diff_summary,
            "exit_code": exit_code,
            "timed_out": timed_out,
            **score,
        }
        with jsonl.open("a") as f:
            f.write(json.dumps(doc) + "\n")
        if db is not None:
            writes.close_attempt(
                attempt_id=attempt_id,
                diff_summary=diff_summary,
                commit=commit,
                visible_pass=score["visible_pass"],
                val_pass=score["val_pass"],
                per_section=score["per_section"],
                outcome="baseline",
                observer_flags=[],
                db=db,
                embedder=embedder,
            )
        docs.append(doc)
        print(
            f"{config.name} #{number:02d} visible {score['visible_pass']:.2f} "
            f"val {score['val_pass']:.2f} (commit {commit[:7]})",
            flush=True,
        )
        previous_visible = score["visible_pass"]
        parent_attempt = attempt_id
