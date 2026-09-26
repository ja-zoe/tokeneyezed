# Tokeneyezed

Long agent runs forget what they tried, repeat dead ends, and quietly game their tests. Tokeneyezed keeps a real coding agent working toward a hard metric across compactions, crashes, and even a switch from Claude Code to Codex. Every attempt lives in MongoDB Atlas, the plan changes when the metric says it isn't working, and an observer keeps cheating and bad steps out of memory. Each piece is measured against a plain retry loop.

Built for MongoDB's Harness Engineering & Model Wrangling hackathon, Statement Two (Long Horizon Engineering).

> Work in progress. The architecture and plan are in [`docs/master-plan.md`](docs/master-plan.md).

## How it works (short version)

- A **LangGraph controller** seeds one goal per CommonMark spec section and loops: pick a goal, build a bounded brief from Atlas, plan an attempt, run it, score it, update the ledger, replan when a goal stalls. Checkpoints are stored in Atlas with `MongoDBSaver`.
- Each **attempt** is a headless coding agent (`claude -p` or `codex exec`) building a Markdown-to-HTML renderer from scratch.
- An **observer** in the agent's hooks blocks shortcuts, tampering, and destructive commands before they run, sends corrective notes after them, and keeps gamed attempts out of memory.
- **Scoring** uses a three-way split of the CommonMark spec examples: visible to the agent, validation for the harness, and held-out for the reported number.

## Repository layout

```
docs/                    master plan, work split, contracts, open questions, SOW
src/tokeneyezed/
  controller/            agent loop (Julian)
  data/                  MongoDB schema, memory, retrieval (Aaron)
  observer/              hooks shim and checks (Dharshan)
  eval/                  task split, scorer, baseline, charts (Gunjan)
tests/
.claude/skills/          project skills for Claude Code
.agents/skills/          the same skills for Codex
```

## Setup

1. Install [uv](https://docs.astral.sh/uv/), then run `uv sync`.
2. Install the MongoDB CLIs: [`mongosh`](https://www.mongodb.com/docs/mongodb-shell/install/) and the [Atlas CLI](https://www.mongodb.com/docs/atlas/cli/current/install-atlas-cli/) (`atlas auth login` once). We use these instead of the MongoDB MCP server.
3. `cp .env.example .env` and fill it in. Use the **Atlas Hackathon Sandbox** cluster from the invite email; the project has to be built there to be eligible as a finalist.
4. **Skills** are already in the repo (`skills-lock.json` pins them). To update: `npx skills update -p` (needs Node.js).

Agent instructions for everyone's coding assistant are in [`AGENTS.md`](AGENTS.md); `CLAUDE.md` imports it.

## Hackathon requirements (from the resource guide)

- Build in the MongoDB Atlas Hackathon Sandbox.
- All work original, done on the day. The repository must be **public**.
- Submit on the Cerebral Valley platform: the public repo link, a **1-minute demo video** (check that audio plays) showing what we built today, and a short project description. Add all four team members to the submission.
- Finalists: at least one member at MongoDB.local NYC on Sept 30, 10 AM to 4:30 PM.

## What we built vs. what we used

To be filled in before submission (required).

| Built by us | Used |
|---|---|
| | Claude Code, Codex, LangGraph, MongoDB Atlas, Voyage AI |

## Team

Aaron, Julian, Dharshan, Gunjan.
