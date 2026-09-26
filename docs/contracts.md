# Contracts (DRAFT until the 10:45 lock)

Four contracts that everything else depends on. Each has one owner. Until the lock, the examples below are illustrations, not agreed shapes. After the lock, the owner replaces each section with the final shape, and any later change needs a heads-up to the whole team.

| Contract | Owner | Consumers |
|---|---|---|
| Neutral event format (`events` documents) | Dharshan | Aaron (storage), Julian (hook wiring) |
| `attempts` and `goals` documents | Aaron | Julian (writes them), Dharshan (post-checks), Gunjan (dashboard) |
| Scorer output JSON | Gunjan | Julian (writes it into `attempts`) |
| Agent adapter (runner backend + shim adapter) | Dharshan (shim) + Julian (runner) | Anyone adding a new coding agent |

The field lists in `master-plan.md` ("MongoDB data model") are the starting point.

## Event (example)

```json
{"session_id": "H-0926", "attempt_id": "a-017", "agent": "claude",
 "phase": "pre", "tool": "bash", "input": "pip install markdown-it-py",
 "output_summary": null, "verdict": "block: honeypot", "ts": "..."}
```

## Goal (locked by Aaron)

One document per spec section per session, in `goals`. Written only by `MongoGoalStore` (`src/tokeneyezed/data/goals.py`).

```json
{"goal_id": "H-0926:Emphasis and strong emphasis", "session_id": "H-0926",
 "section": "Emphasis and strong emphasis",
 "status": "open", "priority": 18,
 "completion_criteria": {"val_pass": 0.85},
 "strategy_notes": "3 attempts without improvement; best validation 0.40",
 "replan_count": 1, "last_replanned_at": "...", "created_at": "...", "completed_at": null}
```

| Field | Meaning |
|---|---|
| `goal_id` | `<session_id>:<section>`. |
| `status` | `open` or `complete`. |
| `priority` | Lower goes first; seeded in config order. A replan does **not** change it: the goal stays next, with a new strategy. |
| `completion_criteria.val_pass` | The goal completes when its section's validation pass rate reaches this. |
| `strategy_notes` | The latest replan note, or `null` before the first replan. The brief shows it to the planner. |
| `replan_count`, `last_replanned_at` | How often and when the goal was replanned. |

## Attempt (locked by Aaron)

One document per attempt, in `attempts` (the ledger). Written only by `MongoLedger` (`src/tokeneyezed/data/ledger.py`): `open_attempt` before the agent starts (events reference its id mid-attempt), then `close_attempt` or `mark_running_as_killed`.

```json
{"attempt_id": "H-0926-017-3fa9c1", "number": 17, "session_id": "H-0926",
 "goal_id": "H-0926:Emphasis and strong emphasis", "agent": "claude",
 "intent": "Replace regex emphasis with a delimiter stack; don't touch link parsing",
 "parent_attempt": "H-0926-014-9be2d0", "status": "closed",
 "diff_summary": "inline.py: delimiter stack", "commit": "3f9c2e1",
 "visible_pass": 0.81, "val_pass": 0.58,
 "per_section": {"Emphasis and strong emphasis": {"visible": 0.90, "val": 0.71}},
 "outcome": "improved", "observer_flags": [],
 "embedding": [0.012, "... 1024 floats"],
 "created_at": "...", "closed_at": "..."}
```

| Field | Meaning |
|---|---|
| `status` | `running` (opened; no scores yet), `closed` (scored), or `killed` (the agent died before finishing; never scored). |
| `number` | Session-wide attempt counter; orders attempts (the last clean commit is the highest-numbered one). |
| `outcome` | Set by the controller on close: `improved` counts as a success; `flagged` means the gaming review flagged it; `killed` is set by `mark_running_as_killed`. Any other value counts as a failed attempt in the brief. |
| `embedding` | Voyage `voyage-4`, 1024 dimensions, over `intent` + `diff_summary`. Absent on flagged and killed attempts. If Voyage failed, it is absent and `needs_embedding: true` marks the attempt for `backfill_embeddings()`. |
| `per_section` | The scorer's output shape. The brief ranks "best attempt" by the goal's own section score. |

**Invariant I8:** a `flagged` attempt stays in `attempts` for the audit, but is never embedded, compacted into `memory`, or shown in the brief.

## Scorer output (to be defined by Gunjan)

Must supply at least `visible_pass`, `val_pass`, and `per_section` in the shape the `attempts` document uses. The held-out score goes only to `test_evals`, never into `attempts`.

## Agent adapter

This is what makes the harness agent-agnostic. The controller, memory, scorer, and observer logic never know which coding agent ran an attempt. Everything agent-specific lives in two small pieces per agent, and adding an agent means writing those two pieces and passing its admission test.

### Admission requirements

An agent can plug in only if it has all of these. An agent without blocking pre-tool hooks can only be observed after the fact, which loses the pre-gate, so it doesn't qualify.

| Requirement | Why | Claude Code | Codex |
|---|---|---|---|
| Headless mode that takes the brief as input and runs unattended | The controller launches attempts with nobody watching | `claude -p` | `codex exec` |
| A turn or time limit | Attempts are timeboxed | `--max-turns` | Timebox enforced by the runner |
| Pre-tool hook that can **block** with a reason before the tool runs | The pre-gate (honeypot, tampering, destructive, learned rules) | `PreToolUse` | `PreToolUse` |
| Post-tool hook that can inject a note into the agent's context | Corrective notes from post-checks | `PostToolUse` | `PostToolUse` |
| Hooks cover shell commands **and** file edits | Tampering is usually an edit, not a command | Yes | To confirm in the smoke test |
| Hook config supplied from outside the task workspace | The agent can't edit what it can't see | `--settings <file>` | Dedicated `CODEX_HOME` |
| Isolated config directory | No personal hooks, instructions, or plugins in a run | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` |
| Model can be pinned | B, H, and H-mem must match | `--model` | `--model` / config |

### Runner backend (Julian)

One per agent, same interface:

```
launch(brief, workspace, attempt_env, timebox) -> handle
wait(handle) -> {exit_code, transcript_path, killed: bool}
kill(handle)   # SIGTERM; the controller marks the attempt "killed" and does not score it
```

- `attempt_env` carries the attempt's identity into the hooks, which inherit the agent's environment: `TOKENEYEZED_SESSION_ID`, `TOKENEYEZED_ATTEMPT_ID`, `TOKENEYEZED_INTENT` (proposed; see open question 1).
- Reads the pinned model from shared config; never hardcodes it.
- Writes the agent's raw output stream to `runs/<session_id>/<attempt_id>.jsonl`.

### Shim adapter (Dharshan)

One per agent, same interface. The hook command runs the shim, which calls the observer:

```
to_event(native_hook_payload, env) -> neutral event   # see "Event" above
from_decision(decision) -> native hook output          # exit code + stdout/stderr
```

Tool names are normalized so observer checks never branch on the agent:

| Neutral `tool` | Claude Code | Codex |
|---|---|---|
| `bash` | `Bash` | shell / `Bash` |
| `edit` | `Edit`, `MultiEdit` | `apply_patch` |
| `write` | `Write` | `apply_patch` (new file) |
| `read` | `Read`, `Grep`, `Glob` | (to confirm) |
| `other` | anything else | anything else |

The observer returns one of three decisions, and each adapter maps it to its agent's output:

| Decision | Meaning | Output (both agents, per the master plan) |
|---|---|---|
| `allow` | Let the tool run | Exit 0, no output |
| `block(reason)` | Pre phase only: stop the tool, tell the agent why | Exit 2, reason on stderr |
| `note(text)` | Post phase only: let it stand, add a corrective note | `additionalContext` JSON on stdout |

**Failure handling:** if the shim can't reach the observer, it fails **closed** in the pre phase (block, with a reason saying the observer is unavailable) and **open** in the post phase (allow, and log the event to a local file for backfill). This matches the master plan's rule that deterministic checks fail closed.

### Admission test

The 10:30 headless-hook smoke test in `master-plan.md`, run against the new agent: hooks fire for commands and edits, a block really prevents the command, and no personal config leaks into the run. An agent that passes is supported.

## CommonMark spec example (for reference)

From the official `spec.json`. Each example carries its section tag, which is what the split stratifies on and what goals are keyed by.

```json
{"example": 360, "section": "Emphasis and strong emphasis",
 "markdown": "_foo_bar\n", "html": "<p>_foo_bar</p>\n"}
```
