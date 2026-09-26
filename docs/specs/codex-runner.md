# Spec: Codex attempt runner (for the agent handoff)

**Status:** approved 2026-09-26 · **Owner:** Julian · **Branch:** `controller/codex-runner`

## Problem

The agent handoff (resume a session on a different agent) needs a second `AttemptRunner`. Only the Claude runner exists, and this morning Codex ran none of our hooks, which would leave a Codex attempt with no observer.

## What the probes established (codex-cli 0.157.1, `gpt-5.6-luna`, 2026-09-26)

| Finding | Evidence |
|---|---|
| This morning's "no hooks" had two causes: a project `.codex/hooks.json` is not loaded by `codex exec` here, and **code mode** runs commands inside a JavaScript `exec` tool (`tools.exec_command(...)`) | Session log: the probe's `echo hello` ran as a `custom_tool_call` named `exec` |
| **Hooks passed as `-c hooks.<Event>=[...]` overrides fire**, with `--disable code_mode` and `--dangerously-bypass-hook-trust` | `SessionStart`, `PreToolUse`, `PostToolUse` all logged by a probe hook |
| **Blocking works**: a `PreToolUse` hook exiting 2 with a stderr reason stops the call, and the agent is told why | `echo TOKENEYEZED_HONEYPOT > marker.txt` blocked; no `PostToolUse`, no file |
| **Edits are covered**: Codex's direct edit tool fires hooks as `tool_name: "apply_patch"`, with the patch text (file paths in `*** Add File:` / `*** Update File:` headers) in `tool_input.command`; shell commands arrive as `tool_name: "Bash"` | Hook log for `note2.txt` |
| **Codex's OS sandbox cannot run on this machine** (it needs unprivileged user namespaces): every command and write fails with `RTM_NEWADDR: Operation not permitted` | First probe |
| Codex's `--json` stream does **not** record hook blocks | No block event in the stream; only the agent's own message mentions it |

## Approach

### Shared runner base (refactor, no behavior change for Claude)

Move what both runners share out of `ClaudeRunner` into `HeadlessRunner`: isolation checks (I2, I6), the harness commit after each attempt, `reset_workspace`, the wall-clock timebox, and stopping the agent's process group on kill. `ClaudeRunner` keeps its command, environment, and transcript parsing; its existing tests are the check that nothing changed.

### `CodexRunner` (`controller/runners/codex.py`)

```
codex exec --json --ephemeral --skip-git-repo-check --cd <workspace>
  --model <[models].codex> --disable code_mode
  --dangerously-bypass-approvals-and-sandbox   # the OS sandbox can't run here; the observer is the gate
  --dangerously-bypass-hook-trust
  -c hooks.PreToolUse=[...shim...] -c hooks.PostToolUse=[...] -c hooks.Stop=[...]
  <brief + intent>
```

- **A clean agent via a dedicated `CODEX_HOME`** (default `~/.codex-tokeneyezed`, logged in once, like the Claude config dir): no personal `config.toml`, skills, memories, or `AGENTS.md`. Hooks come only from the `-c` overrides, so nothing on disk can add or remove them.
- **No sandbox** is the same position as the Claude runner: containment comes from the observer's pre-gate plus the runner's isolation checks. This is why hooks firing is non-negotiable, and it gets its own admission test below.
- **Timebox:** wall clock only (Codex has no turn limit flag).
- **Blocked calls come from the observer's audit log** (`TOKENEYEZED_OBSERVER_AUDIT_LOG`): its `block:` verdicts for this attempt's id. That source works for any agent; when the observer writes to the `events` collection, both runners can switch to it.
- The environment carries the same observer variables as the Claude runner, plus `TOKENEYEZED_AGENT=codex`.

### Wiring

- `runner_for(agent, config)` picks the runner by agent name; `tokeneyezed attempt --agent codex` runs one Codex attempt; `resume --agent codex` uses it once the real ports are wired.

### Needs Dharshan (observer shim, not in this branch)

The shim is Claude-shaped today: it hardcodes `agent: "claude"` and maps unknown tools to `other`. For Codex it needs `TOKENEYEZED_AGENT` for the agent field and `apply_patch` mapped to `edit`/`write`, with target paths read from the patch headers so the tampering check sees them. Until then, Codex honeypot commands are blocked (they arrive as `Bash`), but **Codex edits to protected files are not checked**.

### Out of scope

The shim's Codex adapter (Dharshan), and the heartbeat.

## Tests (the merge gate)

With a fake `codex` binary:
- [x] The command has `--disable code_mode`, the three hook overrides pointing at the shim, the pinned model, `--json`, `--ephemeral`; `CODEX_HOME` is the dedicated dir; the observer variables and `TOKENEYEZED_AGENT=codex` are set; the operator's own `CODEX_*` variables are stripped.
- [x] Blocked calls are read from the audit log for this attempt only.
- [x] The Claude runner's tests pass unchanged after the refactor; `CodexRunner` passes the same run / timebox / kill / reset / isolation tests and the `AttemptRunner` contract test.
- [x] `uv run pytest -rs`, `uv run ruff check`, `uv run ruff format --check` pass.

**Live admission test (done 2026-09-26; passed after the amendments below):** Dharshan's observer running, one real `tokeneyezed attempt --agent codex` with the honeypot brief. Passes if the audit log shows the `pip install` blocked, it never ran, the edit is committed, the feed shows `BLOCKED`, and the dedicated `CODEX_HOME` holds nothing but the login.

Ship: PR from this branch, squash-merged after Julian approves.

## Amendments from the live admission test (2026-09-26)

- **A dedicated `CODEX_HOME` alone is not a clean agent.** In the first admission run Codex attached the account's ChatGPT apps as tools, including financial-account tools (`check_account_balance`, `manage_account`), document and deployment tools, plus memories and plugins. The runner now also passes `--disable apps --disable memories --disable plugins -c web_search="disabled"`. With them the agent lists 11 tools and none of the account's apps; the admission test passed again with them, and no memories were stored. Codex's bundled system skills (e.g. `imagegen`, `openai-docs`) remain: `--disable skills` is not a valid flag.
- **With code mode disabled, a request to use the `exec` tool ran as an ordinary hooked `Bash` call.** That's good evidence the hook-blind JavaScript path is gone, though the agent chose how to carry out the request.
- **Correction to "Needs Dharshan":** without a Codex adapter, the observer does not skip Codex edits, it **blocks** them (`apply_patch` arrives as `other`, and the gate fails closed: "unknown tool requires an explicit adapter"). The agent then writes files through the shell instead (`printf ... > renderer.py`, `python3 -c`), where only explicitly protected shell paths are checked. So the adapter is still needed, for Codex to use its edit tool and for its edits to be tamper-checked.
- The harness commits everything in the workspace, so the task repo needs a `.gitignore` for Python caches (a `__pycache__/*.pyc` was committed in the scratch workspace).
