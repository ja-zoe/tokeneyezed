# Tokeneyezed: agent instructions

Instructions for the coding agents the team uses to build this repo (Claude Code, Codex). They are **not** for the agents that the harness launches to solve the task; those run in a separate task workspace (see "Isolation rules").

## What this is

A hackathon project for MongoDB's Harness Engineering & Model Wrangling hackathon (Statement Two: Long Horizon Engineering). A LangGraph controller runs a headless coding agent (`claude -p` / `codex exec`) in a loop against goals, one per CommonMark spec section. An observer in the agent's hooks gates tool calls and decides what reaches memory, and all state lives in MongoDB Atlas.

Read before working:
- `docs/master-plan.md`: architecture, data model, eval plan, timeline, headless hook traps. The source of truth.
- `docs/work-split.md`: who owns what, and each workstream's build order.
- `docs/contracts.md`: the three shared shapes (event, attempt/goal, scorer output). DRAFT until the 10:45 lock.
- `docs/open-questions.md`: decisions still pending.
- `docs/sow.md`: Aaron's original SOW, for background.

## Workstreams and code ownership

Each workstream has its own package, so four people can work in parallel without merge conflicts. Stay inside your own package unless you've coordinated with its owner.

| Package | Workstream | Owner |
|---|---|---|
| `src/tokeneyezed/controller/` | Agent loop: LangGraph controller, planner, attempt runner, resume, Codex handoff | Julian |
| `src/tokeneyezed/data/` | MongoDB schema, indexes, write helpers, brief builder, compactor, embeddings | Aaron |
| `src/tokeneyezed/observer/` | Shim, pre-gate, post-checks, gaming review, rule learner | Dharshan |
| `src/tokeneyezed/eval/` | CommonMark split, scorer, baseline runner, `test_evals`, charts | Gunjan |

- Nobody writes raw Mongo calls outside `data/`. Use Aaron's helpers (`insert_event`, `write_attempt`, ...) so the schema can't drift.
- `src/tokeneyezed/ports.py` is shared, not owned by one workstream: it defines the interfaces every workstream implements or calls. Each port's contract test is in `tests/contracts/`; add your real implementation to its list there.
- Contract changes (anything in `docs/contracts.md`, `src/tokeneyezed/ports.py`, or `tests/contracts/`) need a heads-up to the whole team, not a silent edit.

## Isolation rules (these protect the eval, don't break them)

- **The task workspace lives outside this repo.** Claude Code loads `CLAUDE.md` and skills from parent directories, so an attempt agent started inside this repo would inherit our instructions and could reach the scorer and hidden splits.
- **The validation and held-out splits must never be reachable from the task workspace.** Gitignoring them isn't enough.
- **The harness never reads `test_evals`.** Only the dashboard and the final report do.
- **Hook configs live outside the task workspace** and are passed with `--settings` (Claude) or a dedicated `CODEX_HOME` (Codex).
- **Attempt agents run with a dedicated `CLAUDE_CONFIG_DIR` / `CODEX_HOME`**, so nobody's personal config contaminates a run. Never use `claude --bare`; it skips hooks and refuses subscription login.
- **One pinned model string**, read from shared config, for B, H, and H-mem.

These rules are catalogued in `tests/INVARIANTS.md` and checked by `tests/test_invariants.py`. Run `uv run pytest -rs` before pushing. When you build a piece an invariant depends on, replace its skip with a real check that also proves it can catch a violation.

## Git conventions

`main` is the integration branch. It must always pass `uv run pytest` and `uv run ruff check`.

- **Straight to `main`:** small doc and plan updates (`docs/`, `README.md`, this file). Pull first, keep the commit focused, and say what changed in the message.
- **Everything else goes on a branch.** That means code, tests, dependency changes, and contract changes. Name it `<workstream>/<short-kebab-description>`, where the workstream is `controller`, `data`, `observer`, or `eval` (for example `controller/attempt-runner`, `observer/pre-gate`). Cross-cutting work uses `shared/<description>`.
- **Keep branches short-lived.** Merge small pieces often rather than one big branch at the end of the day.
- **Sync at task boundaries, not mid-task.** At the start of each task and again before opening a PR: commit or stash, run `git fetch origin && git rebase origin/main`, then re-run `uv run pytest -rs`. Never pull into a dirty tree in the middle of a change.
- **The session-start hook tells you when to sync.** `scripts/sync-check.sh` runs when a Claude Code or Codex session starts (`.claude/settings.json`, `.codex/hooks.json`). It fetches, never merges, and reports how far behind `main` you are and any diff to the shared contracts (`docs/contracts.md`, `tests/INVARIANTS.md`, `tests/contracts/`, `src/tokeneyezed/ports.py`). If it reports contract changes, check your work against them before continuing. Verified in Claude Code. **Not yet working in Codex:** in a test, Codex 0.157.1 ran none of this project's hooks, so Codex users should run `scripts/sync-check.sh` by hand at task start until that's resolved.
- **CI is the gate.** `.github/workflows/ci.yml` runs ruff and pytest on every PR and on `main`. Don't merge a red PR.
- **Merging:** open a PR and squash-merge it once CI is green. You may merge your own PR if it only touches your own package. If it touches another workstream's package, `docs/contracts.md`, or `tests/INVARIANTS.md`, get that owner's OK first.
- **Never** force-push `main`, commit `.env` or other secrets, or commit run artifacts (`runs/` is gitignored).
- Commit messages: imperative subject line ("Add pre-gate honeypot check"), with a body when the why isn't obvious.

## Conventions

- Python 3.12, managed with `uv` (`uv sync`, `uv run ...`, `uv add <pkg>`). Don't add a dependency without a clear reason.
- Lint and format with `ruff` (`uv run ruff check`, `uv run ruff format`). Tests with `uv run pytest`.
- Secrets go in `.env` (gitignored). Add new variables to `.env.example` with a comment.
- Observer: deterministic checks fail **closed**, model-based checks fail **open**.

## Skills and tooling

Project skills are installed in `.claude/skills/` (Claude Code) and `.agents/skills/` (Codex), pinned in `skills-lock.json`:
- MongoDB (`mongodb/agent-skills`): `mongodb-schema-design`, `mongodb-search-and-ai`, `mongodb-connection`, `mongodb-query-optimizer`.
- LangGraph (`langchain-ai/langchain-skills`): `langgraph-fundamentals`, `langgraph-persistence`.

Use them. In particular, read `mongodb-search-and-ai` before building vector or Atlas Search indexes, and `langgraph-persistence` before touching checkpointing. Don't edit the vendored skill files; `npx skills update -p` overwrites them.

**Use CLIs, not MCP servers.** Talk to MongoDB with `mongosh "$MONGODB_URI"` and manage Atlas with the `atlas` CLI. Where a skill tells you to call a MongoDB MCP tool (`aggregate`, `create-index`, `collection-schema`, `explain`, `atlas-get-performance-advisor`, ...), run the equivalent `mongosh` or `atlas` command instead.
