# Observer: first integration slice

Implemented: contract-shaped events, Claude/Codex payload adapters, deterministic pre-gate,
authenticated loopback HTTP endpoint, event-writer injection, and outage backfill.
No new dependencies. Post/stop events are logged and allowed for now.

## Runner integration

Run the service in the harness environment (not the agent's task environment):

```text
python -m tokeneyezed.observer.service --workspace /absolute/task-repo --audit-log /absolute/harness/events.jsonl --protect /absolute/scorer
```

Set `TOKENEYEZED_OBSERVER_TOKEN` to the same nonempty value in the service and
hook environment. The runner also supplies:

- `TOKENEYEZED_SESSION_ID` and `TOKENEYEZED_ATTEMPT_ID`: harness identity, not native agent IDs.
- `TOKENEYEZED_OBSERVER_URL`: `http://127.0.0.1:8765/event`.
- `TOKENEYEZED_OBSERVER_SPOOL`: absolute writable JSONL path outside the task workspace.

The Claude hook command is `<absolute-harness-python> -m tokeneyezed.observer.shim`.
The Codex hook command adds `--agent codex` (and should match both `PreToolUse`
and `PostToolUse` tools `Bash|apply_patch`); the runner must supply the same
`TOKENEYEZED_SESSION_ID`, `TOKENEYEZED_ATTEMPT_ID`, observer URL, token, and spool
environment variables for both adapters. `TOKENEYEZED_AGENT=codex` is also accepted.
Install the harness into that Python environment so imports work from the task directory.
Wire it to PreToolUse and PostToolUse with all-tool matching, and optionally Stop,
in the external hook settings owned by the runner. The adapter consumes JSON stdin.
Allowed calls exit 0 with no stdout; blocked pre calls exit 2 with a stderr reason.
Service, malformed-response, and audit-write failures block pre calls. Post/stop
failures allow execution and spool the event for backfill; spool failures emit a diagnostic.
Use separate spool files per hook process if your runner launches hooks concurrently.

`make_server(gate, token, writer)` accepts `MongoEventWriter`, which calls Aaron's
`data.writes.insert_event` helper and converts the event timestamp to a UTC datetime.
Use `--mongo` instead of `--audit-log` for Atlas storage. Export `MONGODB_URI` and
optionally `TOKENEYEZED_DB` in the service environment; the service does not load
an agent workspace's `.env`. The pre-gate also blocks source edits and writes that
import the forbidden Markdown implementations, even if they are already installed.
Mongo writes have a two-second deadline, shorter than
the shim timeout. Storage failures return HTTP 503 and trigger the shim's existing
phase-specific outage policy and spool. Do not give the database URI to the agent.

```text
python -m tokeneyezed.observer.service --workspace /absolute/task-repo --mongo --protect /absolute/scorer
```

The subprocess integration test exercises shim stdin/exit codes, HTTP, the real
data writer, and a fake database, including storage outages. It is not a live
Atlas or coding-agent admission test.

## Scope and verification

This is a conservative pattern gate, **not a security sandbox**. It checks known
library shortcuts, destructive commands, explicit protected shell paths, and edit
targets (including path traversal and resolved symlinks). Normal pytest commands
are allowed; protected files can be read with read tools. Arbitrary Python, shell
indirection, aliases, and malicious subprocesses can evade text patterns. The
runner must enforce actual filesystem isolation and protect tests/configs at the
OS level. The shared token prevents accidental unauthenticated requests; it is
not an isolation boundary against a process inheriting the hook environment.

Unit tests and an HTTP integration test cover both blocked and allowed behavior.
The Codex adapter normalizes `Bash` and `apply_patch`; patch text is passed through
the same protected-path and forbidden-import checks as Claude edits. The runner's
live hook/honeypot admission has passed, but repeat admission with a protected-file
patch before claiming live tamper protection.

## Offline replay

`tokeneyezed replay --session B-... --events runs/B/events.jsonl --workspace /absolute/task-repo`
reads newline-delimited neutral events with the fields in `docs/contracts.md`. It
filters to the selected session and re-evaluates only `pre` events against the
deterministic gate. It does not change stored verdicts, write to MongoDB, or run
the recorded tool calls. Pass each protected harness path with `--protect`.
Baseline capture must produce this same neutral JSONL shape; the baseline runner
does not install the blocking observer.

Still to build: post-checks, source-based gaming review, and real-agent smoke tests. The
shared contracts remain draft; this module does not change them.

## Learned rules

`tokeneyezed rules learn` reads the latest 10,000 events from Atlas and clusters repeated
blocked pre-tool inputs by tool and block reason. Each literal input pattern is replayed over
previously blocked, allowed, and unlabeled pre-tool events. A pattern becomes active only with at
least two blocked matches and no allowed or unlabeled matches; other supported patterns remain
candidates. Re-running the command replays existing rules too, retiring active rules that no longer
meet those criteria.
Only active rules are loaded by the Mongo-backed observer at startup, so restart the observer
after learning to apply promotions or retirements. This is a deterministic, history-based filter,
not proof that a pattern will generalize safely to unseen inputs.

## Reviewer port

`GamingReviewer` in `observer/reviewer.py` implements the shared
`tokeneyezed.ports.Reviewer` interface from the controller branch:
`review(result, score, previous) -> Review`. Pass an instance as `Ports.reviewer`
in the controller runtime context. No node changes or database access are needed.
The reviewer is stateless; `previous` must be the same goal's last clean score.

Defaults flag a visible gain of at least 0.05 when validation gains at most 0.005,
both overall and in sections present in both scores. These configurable fractions
are heuristic thresholds, not proof of cheating. With no previous score, a valid,
successful attempt is admitted without a divergence judgment. Failed attempts and
invalid rates (including NaN or missing section fields) are flagged for audit.

The port supplies a diff summary rather than source code, so this reviewer does
not claim to detect hardcoded examples. The controller is responsible for keeping
flagged attempts out of compaction and the clean parent chain. An integration test
checks those paths with the real reviewer and the controller's in-memory fakes.

This change depends on `controller/graph-skeleton` (shared ports and controller).
The shared contract test adds GamingReviewer alongside FakeReviewer without
altering the interface or its assertions; coordinate that registration with Julian.
