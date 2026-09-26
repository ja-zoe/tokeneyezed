"""The task workspace: the repo the coding agent works in (docs/specs/eval-runs.md).

It lives outside the harness repo (I2) and holds only what the agent may see: the task, the
renderer stub, the visible split, and a self-test script. The hidden splits live elsewhere
(eval/split.py writes them to a harness-side directory).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from tokeneyezed.controller.runners.base import HARNESS_ROOT, _inside

# The renderer contract eval/scorer.py uses: this command, Markdown on stdin, HTML on stdout.
RENDER_COMMAND = "python3 render.py"

README = """# Task: a CommonMark renderer

Build a Markdown-to-HTML renderer in Python that follows the CommonMark spec (version 0.31.2),
from scratch. **Existing Markdown libraries are forbidden** (markdown-it-py, mistune, markdown2,
python-markdown, commonmark, and the like): installing, importing, or copying them fails the task.

## The renderer contract

`python3 render.py` reads Markdown (UTF-8) on stdin and writes the HTML on stdout. That is exactly
how it is scored: the output must equal the spec's expected HTML (a trailing newline difference is
tolerated). A non-zero exit, or more than 5 seconds for one input, counts as a failure.

## Testing

`examples/visible.json` holds some of the spec's examples (`markdown`, `html`, `section`). Run them:

    python3 run_visible.py            # overall and per-section pass rate
    python3 run_visible.py --failures # also show the failing examples

Other examples, from the same spec sections, are held back for scoring.
"""

RENDER_STUB = '''"""CommonMark renderer: Markdown on stdin, HTML on stdout. Replace this stub."""

import sys


def render(markdown: str) -> str:
    return ""


if __name__ == "__main__":
    sys.stdout.write(render(sys.stdin.read()))
'''

RUN_VISIBLE = '''"""Run render.py against examples/visible.json, the way the scorer does."""

import json
import subprocess
import sys
from collections import defaultdict

examples = json.load(open("examples/visible.json", encoding="utf-8"))
show = "--failures" in sys.argv
by_section = defaultdict(list)
for ex in examples:
    try:
        done = subprocess.run(
            [sys.executable, "render.py"], input=ex["markdown"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        ok = done.returncode == 0 and done.stdout.rstrip("\\n") == ex["html"].rstrip("\\n")
    except subprocess.TimeoutExpired:
        ok = False
    by_section[ex["section"]].append(ok)
    if show and not ok:
        print(f"--- example {ex['example']} ({ex['section']})")
        print(f"{ex['markdown']!r}\\nwant {ex['html']!r}")
total = sum(len(v) for v in by_section.values())
passed = sum(sum(v) for v in by_section.values())
for section, results in sorted(by_section.items()):
    print(f"{sum(results):>3}/{len(results):<3} {section}")
print(f"visible: {passed}/{total} passed ({passed / total:.1%})")
'''

GITIGNORE = "__pycache__/\n*.pyc\n.pytest_cache/\n"


def init_workspace(directory: Path, visible_split: Path) -> Path:
    """Create the task workspace as a git repo with one initial commit; return its path."""
    ws = directory.expanduser().resolve()
    if _inside(ws, HARNESS_ROOT.resolve()) or _inside(HARNESS_ROOT.resolve(), ws):
        raise SystemExit(f"refusing: {ws} overlaps the harness repo (invariant I2)")
    if ws.exists() and any(ws.iterdir()):
        raise SystemExit(f"refusing: {ws} exists and is not empty")
    json.loads(visible_split.read_text(encoding="utf-8"))  # fail early on a bad split file
    (ws / "examples").mkdir(parents=True)
    (ws / "README.md").write_text(README)
    (ws / "render.py").write_text(RENDER_STUB)
    (ws / "run_visible.py").write_text(RUN_VISIBLE)
    (ws / ".gitignore").write_text(GITIGNORE)
    shutil.copyfile(visible_split, ws / "examples" / "visible.json")
    git = ["git", "-c", "user.name=tokeneyezed", "-c", "user.email=harness@tokeneyezed.invalid"]
    subprocess.run([*git, "init", "-q"], cwd=ws, check=True)
    subprocess.run([*git, "add", "-A"], cwd=ws, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "task: initial state"], cwd=ws, check=True)
    return ws
