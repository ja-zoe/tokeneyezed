# Spec: real `claude -p` attempt runner

**Status:** approved 2026-09-26 · **Owner:** Julian · **Branch:** `controller/claude-runner`

## Problem

The loop only runs on fakes. The `AttemptRunner` port needs a real implementation that launches a headless Claude Code attempt in the task workspace with Dharshan's observer in its hooks, so the harness produces real attempts (unblocking Aaron's brief builder and compactor, Dharshan's post-checks, and Gunjan's scoring) and the demo's honeypot beat can happen on cue.

## Approach

### `ClaudeRunner` (`controller/runners/claude.py`), implementing `AttemptRunner`

**`run(session_id, attempt_id, brief, intent)`:**
1. Write this attempt's hook settings file **outside the workspace**, wiring `<harness python> -m tokeneyezed.observer.shim` to `PreToolUse` and `PostToolUse` (all tools) and `Stop`, as the observer README specifies.
2. Launch, with `cwd` = task workspace:
   ```
   claude -p <brief + intent>
     --settings <hook settings file> --model <pinned> --max-turns <max_turns>
     --permission-mode acceptEdits --allowedTools <python/pytest bash patterns>
     --permission-prompts none
     --output-format stream-json --verbose --include-hook-events
   ```
   Never `--bare` (it skips hooks). Environment: a dedicated `CLAUDE_CONFIG_DIR` (default `~/.claude-tokeneyezed`, logged in once) so nobody's personal hooks, CLAUDE.md, or plugins leak in, plus the observer's variables: `TOKENEYEZED_SESSION_ID`, `TOKENEYEZED_ATTEMPT_ID`, `TOKENEYEZED_INTENT`, `TOKENEYEZED_OBSERVER_URL`, `TOKENEYEZED_OBSERVER_TOKEN`, and a per-attempt `TOKENEYEZED_OBSERVER_SPOOL`.
3. The raw stream goes to `runs/<session_id>/<attempt_id>.jsonl` (outside the workspace).
4. **Timebox:** a wall-clock limit on top of `--max-turns`. When it expires, the agent gets SIGTERM and the attempt still counts (it is scored as-is). **Kill:** if the controller itself is interrupted (Ctrl-C / SIGTERM), the runner terminates the agent and raises `AttemptKilled`, which the graph and resume path already handle.
5. **The harness commits, not the agent:** after the agent exits, `git add -A && git commit` in the workspace, so every attempt has a commit to score and to reset to. `diff_summary` is the commit's file stat plus a short summary of changed lines.

**`reset_workspace(commit)`:** `git reset --hard <commit>` and `git clean -fd` in the workspace; `None` means the workspace's root commit (the task's initial state). This discards work by design, and only ever runs in the configured task workspace.

### Isolation checks (make invariants I2 and I6 real)

The runner refuses to start unless: the workspace is a git repo **outside this harness repo** (I2); and the hook settings file, spool, transcripts, and `CLAUDE_CONFIG_DIR` are all **outside the workspace** (I6). The I2 and I6 skips in `tests/test_invariants.py` become real checks against the runner's validation.

### Blocked tool calls in the result (small port change)

The demo's honeypot beat needs the feed to show `BLOCKED pip install markdown-it-py (honeypot)`. Add `blocked: tuple[str, ...] = ()` to `AttemptResult` (defaults keep every existing implementation valid), filled from the transcript's hook events, and print it in the live feed. The exact shape of hook events in `stream-json` gets confirmed in the live smoke test before the parser is written; fallback is Dharshan's audit log. This is a change to `ports.py`, so the team gets a heads-up.

### Single-attempt command (smoke tests and the honeypot beat)

`tokeneyezed attempt --config configs/h.toml --intent "..." [--brief-file f]` (listed with every other command in `docs/commands.md`) runs **one** real attempt with the real runner and fakes for everything else, printing the live feed. It is how we smoke-test the runner, and how the honeypot beat runs on cue: a short attempt whose brief asks for `pip install markdown-it-py`.

### Machine-specific settings

Paths differ per laptop, so they come from the environment (`.env.example`), not `configs/`: `TOKENEYEZED_WORKSPACE`, `TOKENEYEZED_RUNS_DIR`, `CLAUDE_CONFIG_DIR`, `TOKENEYEZED_OBSERVER_URL`, `TOKENEYEZED_OBSERVER_TOKEN`. Shared behavior (`max_turns`, the timebox, allowed tools, the model) stays in `configs/base.toml`.

### Out of scope

The Codex runner, starting the observer service automatically (run it with Dharshan's command for now), the real Planner, and wiring the full real-port set into `tokeneyezed run`.

## Tests (the merge gate)

Automated, with a **fake `claude` executable** on `PATH` that behaves like the real one (edits a file in its cwd, emits `stream-json` lines including a blocked hook event, exits):
- [ ] The command has every required flag, never `--bare`, and points `--settings` at a file outside the workspace; the environment carries the observer variables.
- [ ] `run` writes the transcript, commits the agent's edits, and returns a commit, a diff summary, and the blocked calls.
- [ ] The timebox terminates a hung agent and the attempt still returns a result; an interrupted controller raises `AttemptKilled` and leaves no agent process behind.
- [ ] `reset_workspace` discards uncommitted edits and returns to the given commit (or the root commit for `None`).
- [ ] Isolation: a workspace inside the harness repo, or settings/spool/transcripts/config dir inside the workspace, are refused (I2, I6 now enforced).
- [ ] `ClaudeRunner` (on the fake binary) passes the `AttemptRunner` contract test.
- [ ] `uv run pytest -rs`, `uv run ruff check`, `uv run ruff format --check` pass.

**Live smoke test (manual, one real attempt, before merging):** a scratch task workspace, Dharshan's observer service running, and `tokeneyezed attempt` with a brief that asks for `pip install markdown-it-py` and a small file edit. Passes if: the hook events reach the observer's audit log, the install is blocked and never runs, the edit is committed, the feed shows `BLOCKED`, and the stream's init event shows no personal config (CLAUDE.md, hooks, plugins). This doubles as the Claude half of Dharshan's hook admission test.

Ship: PR from this branch, squash-merged after Julian approves.
