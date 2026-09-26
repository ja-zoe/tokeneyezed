"""Token usage of one attempt, read from the agent's stream-json transcript.

This is the harness's headline metric (long-horizon, large-context work), so it comes from what the
agent itself reports, never from our own estimate. Claude Code's stream-json emits an `assistant`
event per content block with `message.id` and `message.usage`; a message that spans several blocks
repeats the same id, so each id counts once (its last report wins). Every model call re-reads the
whole conversation, so:

    context window at a call = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    peak_context             = the largest of those over the attempt
    tokens_processed         = every call's context plus its output, summed

`tokens_processed` is what the model actually had to read and write; most of it is cache reads once
an attempt is a few turns in, which is why it dwarfs the size of any single window.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def _count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_claude_usage(transcript: Path) -> dict[str, int] | None:
    """Usage totals for the attempt, or None if the transcript reports none.

    None means "unknown", not zero: a missing or unparsable transcript must not look like a cheap
    attempt on the dashboard.
    """
    try:
        lines = transcript.read_text().splitlines()
    except OSError:
        return None
    by_message: dict[str, dict[str, Any]] = {}
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        usage = message.get("usage")
        if isinstance(usage, dict):
            by_message[message.get("id") or f"line-{len(by_message)}"] = usage
    calls = [{key: _count(u, key) for key in _KEYS} for u in by_message.values()]
    calls = [c for c in calls if any(c.values())]  # a login error reports all zeros: no usage
    if not calls:
        return None
    contexts = [
        c["input_tokens"] + c["cache_creation_input_tokens"] + c["cache_read_input_tokens"]
        for c in calls
    ]
    output = sum(c["output_tokens"] for c in calls)
    return {
        "calls": len(calls),
        "peak_context": max(contexts),
        "tokens_processed": sum(contexts) + output,
        "input_tokens": sum(c["input_tokens"] + c["cache_creation_input_tokens"] for c in calls),
        "cache_read_tokens": sum(c["cache_read_input_tokens"] for c in calls),
        "output_tokens": output,
    }
