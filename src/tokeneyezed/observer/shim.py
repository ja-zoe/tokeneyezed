"""Claude hook adapter and HTTP client; invoke with python -m ...observer.shim."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .core import Decision, validate_event

PHASES = {"PreToolUse": "pre", "PostToolUse": "post", "Stop": "stop"}
TOOLS = {
    "Bash": "bash",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "write",
    "Read": "read",
    "Grep": "read",
    "Glob": "read",
}


def to_event(payload: dict, env: dict) -> dict:
    phase = PHASES[payload["hook_event_name"]]
    event = {
        "session_id": env["TOKENEYEZED_SESSION_ID"],
        "attempt_id": env["TOKENEYEZED_ATTEMPT_ID"],
        "agent": "claude",
        "phase": phase,
        "tool": TOOLS.get(payload.get("tool_name"), "other"),
        "input": payload.get("tool_input", {}),
        "output_summary": json.dumps(payload["tool_response"])[:2000]
        if "tool_response" in payload
        else None,
        "verdict": None,
        "ts": datetime.now(UTC).isoformat(),
    }
    validate_event(event)
    return event


def from_decision(decision: Decision, phase: str) -> tuple[int, str, str]:
    if decision.action == "block" and phase == "pre":
        return 2, "", decision.reason
    if decision.action == "note" and phase == "post":
        return (
            0,
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": decision.reason,
                    }
                }
            ),
            "",
        )
    return 0, "", ""


def request_decision(event: dict, url: str, token: str, timeout: float = 3) -> Decision:
    request = urllib.request.Request(
        url,
        data=json.dumps(event).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read(65537))
    if body.get("action") not in ("allow", "block", "note"):
        raise ValueError("invalid observer response")
    reason = body.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("invalid observer reason")
    if (body["action"] == "block" and event["phase"] != "pre") or (
        body["action"] == "note" and event["phase"] != "post"
    ):
        raise ValueError("decision does not match hook phase")
    return Decision(body["action"], reason)


def handle(payload: dict, env: dict, send=request_decision) -> tuple[int, str, str]:
    # Unknown/malformed hook phases fail closed, never silently become post events.
    phase = PHASES.get(payload.get("hook_event_name"), "pre")
    event = None
    try:
        event = to_event(payload, env)
        decision = send(event, env["TOKENEYEZED_OBSERVER_URL"], env["TOKENEYEZED_OBSERVER_TOKEN"])
        return from_decision(decision, phase)
    except Exception:
        warning = "observer unavailable or invalid hook payload"
        try:
            path = Path(env["TOKENEYEZED_OBSERVER_SPOOL"])
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "error": warning}) + "\n")
        except Exception:
            warning += "; audit backfill could not be written"
        if phase == "pre":
            return 2, "", warning
        return 0, "", warning


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        payload = json.loads(sys.stdin.read(1_048_577))
        if not isinstance(payload, dict):
            raise ValueError("expected object")
    except Exception:
        print("invalid hook JSON", file=sys.stderr)
        return 2
    code, stdout, stderr = handle(payload, dict(os.environ))
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
