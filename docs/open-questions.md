# Open questions for the 10:45 lock

Gaps found while reviewing the master plan and the work split. Each one needs an owner to decide it, ideally at the 10:45 contract lock. When one is settled, move the answer into `master-plan.md` or `contracts.md` and delete it here.

## Contracts and plumbing

1. **How do hooks know which attempt they belong to?** The shim needs `session_id`, `attempt_id`, and the planner's intent (the declared-intent check uses it), and nothing specifies how it gets them. Proposal: the attempt runner sets env vars when it launches the agent (`TOKENEYEZED_SESSION_ID`, `TOKENEYEZED_ATTEMPT_ID`, `TOKENEYEZED_INTENT`), since hooks inherit the agent's environment. *Owners: Julian + Dharshan.*

2. **The `attempts` doc has to exist before the agent starts.** Events reference `attempt_id` in the middle of an attempt, but the diagram writes to the ledger only after a clean review. Proposal: create the doc with `status: "running"` at launch, then set the outcome. "Kept out of memory" should mean excluded from the brief, embeddings, and metrics, not never written, because the audit needs the record. *Owners: Aaron + Julian.*

3. **Budget has to count flagged attempts.** Flagged attempts loop back to goal selection without touching the failure threshold. If the budget counts only clean attempts, a goal that invites gaming can use up attempts forever. *Owner: Julian.*

4. **Baseline B needs event logging.** The observer replay eval runs B's recorded events through the observer, but B has "no observer". Options: a log-only hook that never blocks, or parsing B's `stream-json` output into `events`. B also has to be scored on the visible and validation splits (not just visible), because the gaming check compares the two. *Owners: Gunjan + Dharshan.*

5. **Who writes `test_evals`?** Gunjan owns it, but the held-out score has to be written after every attempt of every run, and the controller is what knows when an attempt ends. Decide whether the controller calls Gunjan's writer or Gunjan's code tails `attempts`. *Owners: Julian + Gunjan.*

6. **When do learned rules take effect?** Dharshan's prompt says to promote rules "never mid-run", but the master plan hot-reloads rules through a change stream. Proposal: promote mid-run, but the pre-gate picks up new rules only at the next attempt boundary. That keeps each attempt internally consistent for the eval. *Owner: Dharshan.*

## Agent handoff

The state already lives in Atlas, so the handoff is mostly a demo that proves it. The pieces that are easy to miss:

- **The code is on disk, not in Atlas.** `attempts.commit` is only a hash. On resume, `git checkout` the last clean attempt's commit and discard any half-finished edits from the killed attempt. If Codex runs on a different laptop, the task repo has to be pushed somewhere first.
- **Resume at an attempt boundary.** SIGTERM loses the in-flight attempt. Mark it `killed`, don't score it, and restart from the last checkpoint.
- **Plumbing:** the Codex adapter in the shim, a dedicated `CODEX_HOME`, `--dangerously-bypass-hook-trust`, and a pinned Codex model (a different model from H, which is worth saying in Q&A).

## Task workspace location

The task repo the attempt agents work in must live **outside this repository**. Claude Code loads `CLAUDE.md` files and skills from parent directories, so an agent launched inside this repo would pick up our team instructions, and it could also reach the scorer and the hidden validation and held-out splits. The master plan's `CLAUDE_CONFIG_DIR` isolation does not cover files in parent directories. *Owners: Julian + Gunjan.*

## Embeddings (provisional)

Provisionally Voyage API called directly. Aaron's 10:30 check confirms or reverses it; the reasoning and the check are in `master-plan.md` under "MongoDB data model". *Owner: Aaron.*

## Codex did not run project hooks in a test (affects the observer)

While adding the session-start sync hook (2026-09-26), `codex exec` (codex-cli 0.157.1) ran **no** hook from a repo's `.codex/hooks.json`: neither `SessionStart` nor a `PreToolUse` probe that only appends a line to a file. This held with `--dangerously-bypass-hook-trust` and with the project marked trusted via `-c 'projects."<path>".trust_level="trusted"'`. The same setup in Claude Code worked. Not yet diagnosed: it may be that project-level hooks need interactive `/hooks` review, a user-level `CODEX_HOME` config, or something else. The master plan already plans to use a dedicated `CODEX_HOME` rather than the repo, so the 10:30 smoke test should confirm that path works before an agent handoff involving Codex depends on it. *Owner: Dharshan.*

