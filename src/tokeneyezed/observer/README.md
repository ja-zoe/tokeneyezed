# Observer: first integration slice

Implemented: contract-shaped events, Claude payload adapter, deterministic pre-gate,
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
Install the harness into that Python environment so imports work from the task directory.
Wire it to PreToolUse and PostToolUse with all-tool matching, and optionally Stop,
in the external hook settings owned by the runner. The adapter consumes JSON stdin.
Allowed calls exit 0 with no stdout; blocked pre calls exit 2 with a stderr reason.
Service, malformed-response, and audit-write failures block pre calls. Post/stop
failures allow execution and spool the event for backfill; spool failures emit a diagnostic.
Use separate spool files per hook process if your runner launches hooks concurrently.

`make_server(gate, token, insert_event)` accepts Aaron's eventual storage helper.
The CLI currently uses a local JSONL audit writer. No Mongo calls are made here.

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
Real headless Claude hook admission has not been verified. Codex is deliberately
not advertised as supported until its installed hook payloads and edit blocking
pass the repository's admission test. The core accepts normalized patch targets
for a future adapter.

Still to build: post-checks, score-based gaming review, learned-rule replay/loading,
Mongo writer wiring, and real-agent smoke tests. The shared contracts remain draft;
this module does not change them.
