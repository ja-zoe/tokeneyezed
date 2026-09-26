# Contracts (DRAFT until the 10:45 lock)

Three shapes that everything else depends on. Each has one owner. Until the lock, the examples below are illustrations, not agreed shapes. After the lock, the owner replaces each section with the final shape, and any later change needs a heads-up to the whole team.

| Contract | Owner | Consumers |
|---|---|---|
| Neutral event format (`events` documents) | Dharshan | Aaron (storage), Julian (hook wiring) |
| `attempts` and `goals` documents | Aaron | Julian (writes them), Dharshan (post-checks), Gunjan (dashboard) |
| Scorer output JSON | Gunjan | Julian (writes it into `attempts`) |

The field lists in `master-plan.md` ("MongoDB data model") are the starting point.

## Event (example)

```json
{"session_id": "H-0926", "attempt_id": "a-017", "agent": "claude",
 "phase": "pre", "tool": "Bash", "input": "pip install markdown-it-py",
 "output_summary": null, "verdict": "block: honeypot", "ts": "..."}
```

## Goal (example)

```json
{"session_id": "H-0926", "section": "Emphasis and strong emphasis",
 "status": "open", "priority": 2,
 "completion_criteria": {"val_pass": 0.85},
 "strategy_notes": "Replanned after 3 flat attempts: implement the delimiter-run algorithm (spec 6.2) instead of regex",
 "last_replanned_at": "..."}
```

## Attempt (example)

```json
{"attempt_id": "a-017", "goal_id": "...", "agent": "claude",
 "intent": "Replace regex emphasis with a delimiter stack; don't touch link parsing",
 "commit": "3f9c2e1", "visible_pass": 0.81, "val_pass": 0.58,
 "per_section": {"Emphasis and strong emphasis": {"visible": 0.90, "val": 0.71}},
 "outcome": "improved", "observer_flags": [], "parent_attempt": "a-014"}
```

## Scorer output (to be defined by Gunjan)

Must supply at least `visible_pass`, `val_pass`, and `per_section` in the shape the `attempts` document uses. The held-out score goes only to `test_evals`, never into `attempts`.

## CommonMark spec example (for reference)

From the official `spec.json`. Each example carries its section tag, which is what the split stratifies on and what goals are keyed by.

```json
{"example": 360, "section": "Emphasis and strong emphasis",
 "markdown": "_foo_bar\n", "html": "<p>_foo_bar</p>\n"}
```
