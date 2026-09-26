"""Read-only replay of recorded observer events against the deterministic pre-gate."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .core import Decision, PreGate, validate_event


@dataclass(frozen=True)
class ReplayReport:
    session_id: str
    session_events: int
    pre_events: int
    would_allow: int
    would_block: int
    block_reasons: dict[str, int]


def read_event_log(path: Path) -> Iterable[dict]:
    """Read a JSONL file of neutral observer events, reporting malformed line numbers."""
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"line {line_number} must contain a JSON object")
            yield event


def replay_session(session_id: str, events: Iterable[dict], gate: PreGate) -> ReplayReport:
    """Replay matching events without changing stored events or executing tool calls."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")

    session_events = pre_events = would_allow = would_block = 0
    block_reasons: Counter[str] = Counter()
    for line_number, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            raise ValueError(f"event {line_number} must be an object")
        try:
            validate_event(event)
        except ValueError as exc:
            raise ValueError(f"invalid event {line_number}: {exc}") from exc
        if event["session_id"] != session_id:
            continue
        session_events += 1
        if event["phase"] != "pre":
            continue

        pre_events += 1
        decision: Decision = gate.check(event)
        if decision.action == "block":
            would_block += 1
            block_reasons[decision.reason or "unspecified"] += 1
        else:
            would_allow += 1

    return ReplayReport(
        session_id=session_id,
        session_events=session_events,
        pre_events=pre_events,
        would_allow=would_allow,
        would_block=would_block,
        block_reasons=dict(sorted(block_reasons.items())),
    )
