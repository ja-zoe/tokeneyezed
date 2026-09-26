"""Claude/Codex hook adapters and HTTP client; invoke with python -m ...observer.shim."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .core import Decision, validate_event
from .postcheck import INTENT_HEADER, encode_intent_header

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
CODEX_TOOLS = {"Bash": "bash", "apply_patch": "edit"}


def to_event(payload: dict, env: dict, agent: str | None = None) -> dict:
    phase = PHASES[payload["hook_event_name"]]
    agent = agent or env.get("TOKENEYEZED_AGENT", "claude")
    if agent not in ("claude", "codex"):
        raise ValueError(f"unsupported agent adapter: {agent}")
    native_tool = payload.get("tool_name")
    tool_input = payload.get("tool_input", {})
    if agent == "codex" and native_tool == "apply_patch":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        tool = CODEX_TOOLS[native_tool]
        tool_input = {"patch": command} if isinstance(command, str) else {}
    else:
        tool = (CODEX_TOOLS if agent == "codex" else TOOLS).get(native_tool, "other")
    tool_response = next(
        (
            payload[key]
            for key in ("tool_response", "tool_output", "output")
            if payload.get(key) is not None
        ),
        None,
    )
    event = {
        "session_id": env["TOKENEYEZED_SESSION_ID"],
        "attempt_id": env["TOKENEYEZED_ATTEMPT_ID"],
        "agent": agent,
        "phase": phase,
        "tool": tool,
        "input": tool_input,
        "output_summary": json.dumps(tool_response)[:2000] if tool_response is not None else None,
        "verdict": None,
        "ts": datetime.now(UTC).isoformat(),
    }
    validate_event(event)
    return event


def from_decision(
    decision: Decision, phase: str, agent: str = "claude"
) -> tuple[int, str, str]:
    if decision.action == "block" and phase == "pre":
        return 2, "", decision.reason
    if decision.action == "note" and phase == "post":
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": decision.reason,
            }
        }
        if agent == "codex":
            response.update({"decision": "block", "reason": decision.reason})
        return (
            0,
            json.dumps(response),
            "",
        )
    return 0, "", ""


def request_decision(
    event: dict, url: str, token: str, timeout: float = 3, intent: str = ""
) -> Decision:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if event["phase"] == "post" and intent:
        headers[INTENT_HEADER] = encode_intent_header(intent)
    request = urllib.request.Request(
        url,
        data=json.dumps(event).encode(),
        method="POST",
        headers=headers,
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


def handle(
    payload: dict, env: dict, send=request_decision, agent: str | None = None
) -> tuple[int, str, str]:
    # Unknown/malformed hook phases fail closed, never silently become post events.
    phase = PHASES.get(payload.get("hook_event_name"), "pre")
    event = None
    try:
        event = to_event(payload, env, agent)
        if send is request_decision:
            decision = send(
                event,
                env["TOKENEYEZED_OBSERVER_URL"],
                env["TOKENEYEZED_OBSERVER_TOKEN"],
                intent=env.get("TOKENEYEZED_INTENT", ""),
            )
        else:
            decision = send(
                event, env["TOKENEYEZED_OBSERVER_URL"], env["TOKENEYEZED_OBSERVER_TOKEN"]
            )
        return from_decision(decision, phase, event["agent"])
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=("claude", "codex"),
        default=os.environ.get("TOKENEYEZED_AGENT", "claude"),
    )
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.read(1_048_577))
        if not isinstance(payload, dict):
            raise ValueError("expected object")
    except Exception:
        print("invalid hook JSON", file=sys.stderr)
        return 2
    code, stdout, stderr = handle(payload, dict(os.environ), agent=args.agent)
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
