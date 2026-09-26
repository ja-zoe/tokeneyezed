"""Read-only queries behind the dashboard. Raw Mongo reads stay in data/, so the GUI imports these.

Runs are derived from `attempts` and `goals`, because nothing writes `sessions` documents yet; a
`sessions` document, when there is one, only adds config (agent, model, ablation flags).

Held-out scores come from `eval.held_out`, which owns that collection (invariant I3): the dashboard
and the final report are its only readers, the harness never touches it.

Only equality filters, `find` and `distinct` are used, so a small in-memory fake can stand in for a
database in tests (see tests/test_dashboard.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from tokeneyezed.data.db import get_db
from tokeneyezed.eval.held_out import held_out_by_attempt

RUN_NAME_ORDER = ("B", "H", "H-mem")  # the three configurations the comparison is built around
_SESSION_PREFIX = re.compile(r"^(?P<name>H-mem|H|B)(?=-)", re.IGNORECASE)
_NO_EMBEDDING = {"embedding": 0}  # 1024 floats per attempt; the dashboard never needs them


def _dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return value if isinstance(value, str) else None


def run_name(session_id: str, session_doc: Mapping[str, Any] | None = None) -> str:
    """B, H or H-mem: from the session's stored config, else from its id (`H-0926-101500`)."""
    config = (session_doc or {}).get("config") or {}
    if config.get("name"):
        return str(config["name"])
    match = _SESSION_PREFIX.match(session_id)
    if not match:
        return "other"
    return {"h-mem": "H-mem"}.get(match["name"].lower(), match["name"].upper())


def _is_clean(attempt: Mapping[str, Any]) -> bool:
    """Counts toward progress: closed and not flagged (I8) or killed."""
    return attempt.get("status") == "closed" and attempt.get("outcome") not in ("flagged", "killed")


def _blocked_events(db: Database, session_id: str) -> list[dict[str, Any]]:
    out = []
    for event in db["events"].find({"session_id": session_id}):
        verdict = str(event.get("verdict") or "")
        if verdict.startswith(("block", "flag")):
            out.append(
                {
                    "attempt_id": event.get("attempt_id"),
                    "agent": event.get("agent"),
                    "tool": event.get("tool"),
                    "input": str(event.get("input") or "")[:200],
                    "verdict": verdict,
                    "ts": _dt(event.get("ts")),
                }
            )
    return sorted(out, key=lambda e: e["ts"] or "")


def _series(attempts: Sequence[Mapping[str, Any]], held_out: Mapping[str, float]) -> list[dict]:
    """One point per attempt, in attempt order, with a running best over clean attempts only."""
    points, best_val, best_held = [], None, None
    for a in sorted(attempts, key=lambda d: d.get("number", 0)):
        clean = _is_clean(a)
        val = a.get("val_pass") if a.get("status") == "closed" else None
        held = held_out.get(a["attempt_id"])
        if clean and val is not None:
            best_val = val if best_val is None else max(best_val, val)
        if clean and held is not None:
            best_held = held if best_held is None else max(best_held, held)
        section = str(a.get("goal_id") or "").split(":", 1)[-1]
        points.append(
            {
                "attempt_id": a["attempt_id"],
                "number": a.get("number"),
                "status": a.get("status"),
                "outcome": a.get("outcome"),
                "agent": a.get("agent"),
                "goal_id": a.get("goal_id"),
                "section": section,
                "intent": a.get("intent"),
                "diff_summary": a.get("diff_summary"),
                "visible_pass": a.get("visible_pass"),
                "val_pass": val,
                "held_out": held,
                "best_val": best_val,
                "best_held_out": best_held,
                # Reported by the agent (runners/usage.py); None when an attempt has no usage.
                "context_tokens": (a.get("usage") or {}).get("peak_context"),
                "tokens_processed": (a.get("usage") or {}).get("tokens_processed"),
                "usage": a.get("usage"),
                "flags": list(a.get("observer_flags") or []),
                "clean": clean,
                "created_at": _dt(a.get("created_at")),
                "closed_at": _dt(a.get("closed_at")),
            }
        )
    return points


def _summarize(
    session_id: str,
    attempts: Sequence[Mapping[str, Any]],
    goals: Sequence[Mapping[str, Any]],
    held_out: Mapping[str, float],
    session_doc: Mapping[str, Any] | None,
) -> dict[str, Any]:
    ordered = sorted(attempts, key=lambda d: d.get("number", 0))
    closed = [a for a in ordered if a.get("status") == "closed"]
    clean = [a for a in closed if _is_clean(a)]
    config = (session_doc or {}).get("config") or {}
    agents = list(dict.fromkeys(a.get("agent") for a in ordered if a.get("agent")))
    models = config.get("models") or {}
    model = config.get("model") or "; ".join(str(models[a]) for a in agents if a in models) or None
    last_held = next(
        (held_out[a["attempt_id"]] for a in reversed(clean) if a["attempt_id"] in held_out), None
    )
    usages = [a["usage"] for a in ordered if a.get("usage")]
    has_running = any(a.get("status") == "running" for a in ordered)
    stamps = [_dt(a.get("closed_at") or a.get("created_at")) for a in ordered]
    open_goals = sum(1 for g in goals if g.get("status") == "open")
    if has_running:
        status = "running"
    elif goals and open_goals == 0:
        status = "complete"
    else:
        status = (session_doc or {}).get("status") or "stopped"
    return {
        "session_id": session_id,
        "name": run_name(session_id, session_doc),
        "status": status,
        "agents": agents,
        "model": model,
        "attempts": len(ordered),
        "budget": config.get("max_attempts"),
        "flagged": sum(1 for a in ordered if a.get("outcome") == "flagged"),
        "killed": sum(1 for a in ordered if a.get("outcome") == "killed"),
        "replans": sum(int(g.get("replan_count") or 0) for g in goals),
        "goals_done": len(goals) - open_goals,
        "goals_total": len(goals),
        # The headline metric: what the model read and wrote across the run, and its largest window.
        "tokens_processed": sum(u.get("tokens_processed", 0) for u in usages) if usages else None,
        "peak_context": max((u.get("peak_context", 0) for u in usages), default=None),
        "usage_attempts": len(usages),
        "best_val": max((a["val_pass"] for a in clean), default=None),
        "final_val": clean[-1]["val_pass"] if clean else None,
        "final_held_out": last_held,
        "best_held_out": max(
            (held_out[a["attempt_id"]] for a in clean if a["attempt_id"] in held_out), default=None
        ),
        "started_at": min((s for s in stamps if s), default=None),
        "updated_at": max((s for s in stamps if s), default=None),
    }


def _load(db: Database, session_id: str) -> tuple[list, list, dict, Mapping | None]:
    attempts = list(db["attempts"].find({"session_id": session_id}, _NO_EMBEDDING))
    goals = list(db["goals"].find({"session_id": session_id}))
    session_doc = next(iter(db["sessions"].find({"session_id": session_id})), None)
    return attempts, goals, held_out_by_attempt(db, session_id), session_doc


def list_runs(db: Database | None = None) -> list[dict[str, Any]]:
    """Every run that has attempts, newest first."""
    db = db if db is not None else get_db()
    runs = []
    for session_id in db["attempts"].distinct("session_id"):
        attempts, goals, held_out, session_doc = _load(db, session_id)
        runs.append(_summarize(session_id, attempts, goals, held_out, session_doc))
    return sorted(runs, key=lambda r: r["updated_at"] or "", reverse=True)


def _handoffs(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Where the agent changed between consecutive attempts: the agent handoff."""
    out, previous = [], None
    for p in points:
        if previous and p["agent"] and previous["agent"] and p["agent"] != previous["agent"]:
            out.append({"number": p["number"], "from": previous["agent"], "to": p["agent"]})
        previous = p
    return out


def _goal_rows(goals: Iterable[Mapping[str, Any]], points: Sequence[Mapping[str, Any]]) -> list:
    rows = []
    for g in sorted(goals, key=lambda d: d.get("priority", 0)):
        mine = [p for p in points if p["goal_id"] == g["goal_id"] and p["clean"]]
        replanned = _dt(g.get("last_replanned_at"))
        # Only the latest replan time is stored, so the marker sits on the first attempt that was
        # opened after it. Earlier replans of the same goal aren't recoverable from `goals`.
        after = next(
            (
                p
                for p in points
                if p["goal_id"] == g["goal_id"]
                and replanned
                and (p["created_at"] or "") >= replanned
            ),
            None,
        )
        rows.append(
            {
                "goal_id": g["goal_id"],
                "section": g.get("section"),
                "status": g.get("status"),
                "target": (g.get("completion_criteria") or {}).get("val_pass"),
                "strategy_notes": g.get("strategy_notes") or "",
                "replan_count": int(g.get("replan_count") or 0),
                "last_replanned_at": replanned,
                "replan_at_attempt": after["number"] if after else None,
                "attempts": len([p for p in points if p["goal_id"] == g["goal_id"]]),
                "best_val": max(
                    (p["val_pass"] for p in mine if p["val_pass"] is not None), default=None
                ),
            }
        )
    return rows


def run_detail(session_id: str, db: Database | None = None) -> dict[str, Any] | None:
    """Everything the run page draws: summary, per-attempt series, goals, blocks, handoffs."""
    db = db if db is not None else get_db()
    attempts, goals, held_out, session_doc = _load(db, session_id)
    if not attempts:
        return None
    points = _series(attempts, held_out)
    return {
        "run": _summarize(session_id, attempts, goals, held_out, session_doc),
        "attempts": points,
        "goals": _goal_rows(goals, points),
        "blocked": _blocked_events(db, session_id),
        "handoffs": _handoffs(points),
    }


def compare_runs(session_ids: Sequence[str], db: Database | None = None) -> dict[str, Any]:
    """Score series for several runs. `common` is the shortest run's attempt count, so lines can
    be compared at equal attempt numbers (the baseline is capped at the harness's count)."""
    db = db if db is not None else get_db()
    runs = []
    for sid in session_ids:
        detail = run_detail(sid, db)
        if detail is None:
            continue
        runs.append(
            {
                "session_id": sid,
                "name": detail["run"]["name"],
                "summary": detail["run"],
                "points": [
                    {
                        k: p[k]
                        for k in (
                            "number",
                            "val_pass",
                            "held_out",
                            "best_val",
                            "best_held_out",
                            "visible_pass",
                            "clean",
                            "context_tokens",
                            "tokens_processed",
                        )
                    }
                    for p in detail["attempts"]
                ],
            }
        )
    lengths = [len(r["points"]) for r in runs]
    return {"runs": runs, "common": min(lengths) if lengths else 0}


def default_compare_ids(db: Database | None = None) -> list[str]:
    """The newest run of each configuration, in B, H, H-mem order."""
    latest: dict[str, str] = {}
    for run in list_runs(db):  # newest first, so setdefault keeps the newest
        latest.setdefault(run["name"], run["session_id"])
    return [latest[n] for n in RUN_NAME_ORDER if n in latest]


def _tail(text: Any, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _entry(ts: Any, kind: str, attempt: str | None, text: str, detail: str = "") -> dict[str, Any]:
    return {"ts": _dt(ts), "kind": kind, "attempt_id": attempt, "text": text, "detail": detail}


def run_feed(
    session_id: str, after: str | None = None, limit: int = 300, db: Database | None = None
) -> dict[str, Any]:
    """The CLI's live feed rebuilt from Atlas: attempts opening and closing, hook events, replans.

    The CLI runs on someone's machine and the dashboard may be on Vercel, so stdout can't be
    streamed; what every process shares is the database. Entries come back oldest first, with
    `ts >= after` (inclusive, so equal timestamps aren't dropped; the client de-duplicates).
    Filtering happens here, not in the query, so a run's feed rereads its events each poll: fine at
    hackathon scale (hundreds to low thousands of events per run).
    """
    db = db if db is not None else get_db()
    attempts = list(db["attempts"].find({"session_id": session_id}, _NO_EMBEDDING))
    if not attempts:
        return {"entries": [], "cursor": after, "running": False, "known": False}
    numbers = {a["attempt_id"]: a.get("number") for a in attempts}
    label = lambda aid: f"#{numbers.get(aid) or 0:03d}"  # noqa: E731
    out: list[dict[str, Any]] = []
    for a in attempts:
        aid, section = a["attempt_id"], str(a.get("goal_id") or "").split(":", 1)[-1]
        out.append(
            _entry(
                a.get("created_at"),
                "attempt",
                aid,
                f"{label(aid)}  {a.get('agent', ''):<7} {section}",
                f"intent  {_tail(a.get('intent'), 240)}",
            )
        )
        if a.get("status") == "closed":
            score = f"score   visible {a['visible_pass']:.2f}  val {a['val_pass']:.2f}"
            outcome = a.get("outcome")
            kind = {"improved": "good", "flagged": "bad"}.get(outcome, "info")
            verdict = {
                "flagged": "FLAGGED  kept out of memory and metrics",
                "improved": "improved",
            }.get(outcome, f"{outcome or 'closed'}")
            out.append(_entry(a.get("closed_at"), kind, aid, f"{label(aid)}  {score}", verdict))
            for reason in a.get("observer_flags") or []:
                out.append(
                    _entry(a.get("closed_at"), "bad", aid, f"{label(aid)}  review", _tail(reason))
                )
        elif a.get("status") == "killed":
            out.append(
                _entry(
                    a.get("closed_at") or a.get("created_at"),
                    "bad",
                    aid,
                    f"{label(aid)}  KILLED",
                    "recorded, never scored",
                )
            )
    for e in db["events"].find({"session_id": session_id}):
        if e.get("phase") != "pre":  # the post event repeats the same call with its output
            continue
        verdict, aid = str(e.get("verdict") or ""), e.get("attempt_id")
        blocked = verdict.startswith("block")
        out.append(
            _entry(
                e.get("ts"),
                "bad" if blocked else "tool",
                aid,
                f"{label(aid)}  {'BLOCKED' if blocked else e.get('tool', 'tool'):<7} "
                f"{_tail(e.get('input'))}",
                verdict if blocked else "",
            )
        )
    for g in db["goals"].find({"session_id": session_id}):
        if g.get("last_replanned_at"):
            out.append(
                _entry(
                    g["last_replanned_at"],
                    "warn",
                    None,
                    f"REPLAN  {g.get('section')}",
                    f"strategy  {_tail(g.get('strategy_notes'), 240)}",
                )
            )
        if g.get("completed_at"):
            out.append(
                _entry(g["completed_at"], "good", None, f"GOAL COMPLETE  {g.get('section')}")
            )
    out = sorted((e for e in out if e["ts"]), key=lambda e: e["ts"])
    if after:
        out = [e for e in out if e["ts"] >= after]
    out = out[-limit:]
    running = any(a.get("status") == "running" for a in attempts)
    return {
        "entries": out,
        "cursor": out[-1]["ts"] if out else after,
        "running": running,
        "known": True,
    }


def running_session(db: Database | None = None) -> str | None:
    """The run currently in progress (newest), else the most recently updated one."""
    runs = list_runs(db)
    live = next((r for r in runs if r["status"] == "running"), None)
    return (live or (runs[0] if runs else {})).get("session_id")
