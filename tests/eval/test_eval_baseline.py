"""End-to-end tests for run B: a real fake-claude binary, real git, real scoring.

The fake `claude` records its argv/env, then edits render.py like an agent
would: attempt 1 leaves the echo stub (scores 0 on these splits), attempt 2
writes the correct renderer (scores 1.0). Mongo runs against tests/mongo_fakes
(the repo's standard in-memory pymongo stand-in), never a live cluster.

Branches left uncovered on purpose (unreachable without doubles): the
SIGKILL-after-grace path in run_attempt (needs an agent that ignores SIGTERM
for GRACE_SECONDS=10s), the ProcessLookupError race (agent exits between the
timeout and the killpg), and get_db() from MONGODB_URI (needs a live Atlas
cluster; the injected-db path covers everything after the connection).
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from mongo_fakes import FakeDB, embedder

from tokeneyezed.controller.cli import main
from tokeneyezed.controller.config import RunConfig, load_config
from tokeneyezed.eval.baseline import FEEDBACK, BaselinePaths, run_baseline

# The fake agent: logs one JSON line per call, then "improves" render.py on its
# second call. `#!` + sys.executable keeps it venv-independent.
FAKE_CLAUDE = f"""\
#!{sys.executable}
import json, os, sys, time
with open(os.environ["FAKE_BASELINE_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
        "env": {{k: v for k, v in os.environ.items()}}}}) + "\\n")
if os.environ.get("FAKE_BASELINE_MODE") == "hang":
    time.sleep(60)
elif os.path.exists("attempt-1-done"):
    with open("render.py", "w") as f:
        f.write("import sys\\n")
        f.write('sys.stdout.write("<p>" + sys.stdin.read().strip() + "</p>\\\\n")\\n')
else:
    with open("attempt-1-done", "w") as f:
        f.write("")
"""

VISIBLE = [{"example": 1, "section": "Paragraphs", "markdown": "hi\n", "html": "<p>hi</p>\n"}]
VALIDATION = [{"example": 2, "section": "Paragraphs", "markdown": "yo\n", "html": "<p>yo</p>\n"}]

CONFIG = RunConfig(
    name="B",
    agent="claude",
    models={"claude": "test-model"},
    memory=False,
    max_attempts=2,
    max_turns=3,
    failure_threshold=2,
    target_val_pass=0.85,
    timebox_minutes=15,
    allowed_tools=("Bash(python3 *)",),
    sections=("Paragraphs",),
)


def fake_claude(tmp_path: Path, monkeypatch) -> Path:
    """Install the fake `claude` on PATH and route its log to tmp; return the log path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_BASELINE_LOG", str(log))
    monkeypatch.delenv("FAKE_BASELINE_MODE", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    return log


def make_paths(tmp_path: Path) -> BaselinePaths:
    """A git task workspace with the echo stub, plus harness-side splits and runs/config dirs.

    The workspace's own tests/visible.json is a decoy the echo stub would pass:
    scores of 0.0 on attempt 1 prove the loop reads the harness-side copy instead.
    """
    ws = tmp_path / "workspace"
    hidden = tmp_path / "hidden"
    (ws / "tests").mkdir(parents=True)
    hidden.mkdir()
    (ws / "render.py").write_text("import sys\nsys.stdout.write(sys.stdin.read())\n")
    (ws / "README.md").write_text("Build the renderer.\n")
    decoy = [{"example": 1, "section": "Paragraphs", "markdown": "hi\n", "html": "hi\n"}]
    (ws / "tests" / "visible.json").write_text(json.dumps(decoy))
    (hidden / "visible.json").write_text(json.dumps(VISIBLE))
    (hidden / "validation.json").write_text(json.dumps(VALIDATION))
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "task: initial state"],
    ):
        subprocess.run(["git", *args], cwd=ws, check=True, env=env)
    return BaselinePaths(
        workspace=ws,
        splits_dir=hidden,
        runs_dir=tmp_path / "runs",
        config_dir=tmp_path / "claude-config",
    )


def test_two_attempts_improve_score_feedback_and_logs(tmp_path, monkeypatch, capsys) -> None:
    """The whole loop: run, commit, score, log; the feedback line carries the last pass rate."""
    log = fake_claude(tmp_path, monkeypatch)
    paths = make_paths(tmp_path)
    docs = run_baseline(CONFIG, paths, claude_bin="claude", session_id="B-test")

    assert [d["number"] for d in docs] == [1, 2]
    # 0.0, not the decoy's 1.0: visible is scored from the harness-side copy.
    assert docs[0]["visible_pass"] == 0.0 and docs[0]["val_pass"] == 0.0
    assert docs[1]["visible_pass"] == 1.0 and docs[1]["val_pass"] == 1.0
    assert docs[0]["parent_attempt"] is None
    assert docs[1]["parent_attempt"] == "B-test-001"
    assert not docs[0]["timed_out"] and docs[0]["exit_code"] == 0
    assert docs[1]["scorer_version"] and docs[1]["counts"]["validation"]["total"] == 1

    # attempts.jsonl holds exactly the returned docs, one JSON per line.
    lines = (paths.runs_dir / "B-test" / "attempts.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == docs

    # The agent saw the base prompt alone, then the prompt plus the feedback line.
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    prompts = [call["argv"][call["argv"].index("-p") + 1] for call in calls]
    assert prompts[0] == "Build the renderer."
    assert prompts[1] == "Build the renderer.\n\n" + FEEDBACK.format(0.0)
    assert "Previous attempt passed 0% of visible tests." in prompts[1]

    # The invocation mirrors the approved runner, minus hooks and observer wiring.
    argv = calls[0]["argv"]
    for flag, value in [
        ("--model", "test-model"),
        ("--max-turns", "3"),
        ("--permission-mode", "acceptEdits"),
        ("--output-format", "stream-json"),
        ("--allowedTools", "Bash(python3 *)"),
    ]:
        assert argv[argv.index(flag) + 1] == value, flag
    assert "--include-hook-events" not in argv
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    assert settings == {"autoMemoryEnabled": False}  # memory off, and no hooks (observer-blind)

    # The agent env: harness vars stripped, only its dedicated config dir set.
    env = calls[0]["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(paths.config_dir)
    leaked = [k for k in env if k.startswith("TOKENEYEZED_")]
    assert leaked == []
    assert calls[0]["cwd"] == str(paths.workspace)

    # The harness committed each attempt in the task repo.
    subjects = subprocess.run(
        ["git", "log", "--format=%s"], cwd=paths.workspace, capture_output=True, text=True
    ).stdout.splitlines()
    assert subjects[:2] == ["attempt B-test-002", "attempt B-test-001"]
    assert "render.py" in docs[1]["diff_summary"]

    # One progress line per attempt for the operator's pane.
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("B #01 visible 0.00 val 0.00")
    assert out[1].startswith("B #02 visible 1.00 val 1.00")

    # Transcripts and stderr land under runs/<session>/.
    assert (paths.runs_dir / "B-test" / "B-test-001.jsonl").exists()
    assert (paths.runs_dir / "B-test" / "B-test-001.stderr").exists()


def test_timed_out_attempt_is_stopped_counted_and_scored(tmp_path, monkeypatch) -> None:
    """The timebox SIGTERMs the agent's process group; the attempt still counts, scored as-is."""
    fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_BASELINE_MODE", "hang")
    paths = make_paths(tmp_path)
    docs = run_baseline(CONFIG, paths, claude_bin="claude", timebox_seconds=0.5)
    assert docs[0]["session_id"].startswith("B-")  # default id: <run name>-<timestamp>
    assert len(docs) == 2  # a timeout never ends the run early
    assert all(d["timed_out"] for d in docs)
    assert docs[0]["exit_code"] != 0  # killed by SIGTERM, not a clean exit
    assert "(stopped at the timebox)" in docs[0]["diff_summary"]
    assert docs[0]["visible_pass"] == 0.0  # the untouched echo stub, scored as-is


def test_mongo_docs_open_and_close_with_agent_b(tmp_path, monkeypatch) -> None:
    """With a database, every attempt lands in `attempts` via data/writes.py, agent="B"."""
    log = fake_claude(tmp_path, monkeypatch)
    paths = make_paths(tmp_path)
    (paths.workspace / "PROMPT.md").write_text("Prompt wins.\n")  # preferred over README.md
    db = FakeDB()
    docs = run_baseline(
        CONFIG, paths, claude_bin="claude", session_id="B-mongo", db=db, embedder=embedder()
    )
    first = json.loads(log.read_text().splitlines()[0])
    assert first["argv"][first["argv"].index("-p") + 1] == "Prompt wins."
    stored = list(db["attempts"].find({"session_id": "B-mongo"}))
    assert [d["attempt_id"] for d in stored] == ["B-mongo-001", "B-mongo-002"]
    for mongo_doc, doc in zip(stored, docs, strict=True):
        assert mongo_doc["agent"] == "B"
        assert mongo_doc["status"] == "closed"
        assert mongo_doc["outcome"] == "baseline"
        assert mongo_doc["goal_id"] == "B-mongo:baseline"
        assert mongo_doc["visible_pass"] == doc["visible_pass"]
        assert mongo_doc["per_section"] == doc["per_section"]
        assert mongo_doc["observer_flags"] == []  # B never sees the observer


def test_cli_baseline_runs_the_loop_from_env_paths(tmp_path, monkeypatch) -> None:
    """`tokeneyezed baseline --config <run.toml>` drives run_baseline end to end."""
    fake_claude(tmp_path, monkeypatch)
    paths = make_paths(tmp_path)
    configs = tmp_path / "configs"
    configs.mkdir()
    base = load_config("configs/b.toml")  # mirror the real base, with a 2-attempt budget
    configs.joinpath("base.toml").write_text(
        f"""
max_attempts = 2
max_turns = {base.max_turns}
failure_threshold = {base.failure_threshold}
target_val_pass = {base.target_val_pass}
timebox_minutes = {base.timebox_minutes}
allowed_tools = ["Bash(python3 *)"]
sections = ["Paragraphs"]
[models]
claude = "test-model"
"""
    )
    configs.joinpath("b.toml").write_text(
        'extends = "base.toml"\nname = "B"\nagent = "claude"\nmemory = false\n'
    )
    monkeypatch.setenv("TOKENEYEZED_WORKSPACE", str(paths.workspace))
    monkeypatch.setenv("TOKENEYEZED_SPLITS_DIR", str(paths.splits_dir))
    monkeypatch.setenv("TOKENEYEZED_RUNS_DIR", str(paths.runs_dir))
    monkeypatch.setenv("TOKENEYEZED_CLAUDE_CONFIG_DIR", str(paths.config_dir))

    assert main(["baseline", "--config", str(configs / "b.toml"), "--session-id", "B-cli"]) == 0
    lines = (paths.runs_dir / "B-cli" / "attempts.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["visible_pass"] == 1.0


def test_cli_baseline_without_splits_says_run_split_first(tmp_path, monkeypatch) -> None:
    fake_claude(tmp_path, monkeypatch)
    ws = tmp_path / "empty-ws"
    ws.mkdir()
    monkeypatch.setenv("TOKENEYEZED_WORKSPACE", str(ws))
    monkeypatch.setenv("TOKENEYEZED_SPLITS_DIR", str(tmp_path / "no-splits"))
    with pytest.raises(SystemExit, match="run `tokeneyezed split` first"):
        main(["baseline", "--config", "configs/b.toml"])


def test_cli_baseline_without_task_text_says_init_workspace(tmp_path, monkeypatch) -> None:
    """A workspace with no PROMPT.md/README.md means `workspace init` never ran."""
    fake_claude(tmp_path, monkeypatch)
    paths = make_paths(tmp_path)
    (paths.workspace / "README.md").unlink()
    monkeypatch.setenv("TOKENEYEZED_WORKSPACE", str(paths.workspace))
    monkeypatch.setenv("TOKENEYEZED_SPLITS_DIR", str(paths.splits_dir))
    with pytest.raises(SystemExit, match="workspace init"):
        main(["baseline", "--config", "configs/b.toml"])
