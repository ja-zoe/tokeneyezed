from tokeneyezed.observer.core import PreGate
from tokeneyezed.observer.learner import learn_rules


def event(verdict, payload, event_id, *, tool="bash", phase="pre"):
    return {
        "_id": event_id,
        "session_id": "s1",
        "attempt_id": f"attempt-{event_id}",
        "agent": "codex",
        "phase": phase,
        "tool": tool,
        "input": payload,
        "verdict": verdict,
        "ts": "2026-09-26T12:00:00+00:00",
    }


def find_rule(rules, pattern):
    return next(rule for rule in rules if rule["pattern"] == pattern)


def test_replay_promotes_only_repeated_patterns_without_good_hits():
    events = [
        event("block: destructive command", "git reset --hard", "bad-1"),
        event("block: destructive command", "git reset --hard HEAD", "bad-2"),
        event("allow", "git status --short", "good-1"),
    ]

    rules = learn_rules(events)
    rule = find_rule(rules, "git reset --hard")

    assert rule["status"] == "active"
    assert rule["evidence_event_ids"] == ["bad-1", "bad-2"]
    assert rule["replay"] == {
        "hits_on_flagged": 2,
        "hits_on_good": 0,
        "hits_on_unlabeled": 0,
        "minimum_support": 2,
    }


def test_replay_keeps_false_positive_pattern_as_candidate():
    events = [
        event("block: destructive command", "git reset --hard", "bad-1"),
        event("block: destructive command", "git reset --hard HEAD", "bad-2"),
        event("allow", "git reset --hard --dry-run", "good-1"),
    ]

    rule = find_rule(learn_rules(events), "git reset --hard")

    assert rule["status"] == "candidate"
    assert rule["replay"]["hits_on_good"] == 1


def test_unlabeled_replay_hits_prevent_promotion():
    events = [
        event("block: repeated violation", "dangerous token", "bad-1"),
        event("block: repeated violation", "dangerous token now", "bad-2"),
        event("observed", "dangerous token in baseline", "unknown-1"),
    ]

    rule = find_rule(learn_rules(events), "dangerous token")

    assert rule["status"] == "candidate"
    assert rule["replay"]["hits_on_unlabeled"] == 1


def test_learner_ignores_single_flags_and_non_pre_events():
    rules = learn_rules(
        [
            event("block: destructive command", "git reset --hard", "bad-1"),
            event("block: destructive command", "git reset --hard", "bad-2", phase="post"),
        ]
    )

    assert rules == []


def test_replay_retires_a_stale_active_rule():
    stale_rule = {
        "tool": "bash",
        "check_type": "input_contains",
        "pattern": "dangerous token",
        "status": "active",
        "version": 1,
    }

    [rule] = learn_rules(
        [event("allow", "echo dangerous token", "good-1")],
        [stale_rule],
    )

    assert rule["status"] == "retired"
    assert rule["replay"]["hits_on_good"] == 1


def test_pre_gate_applies_only_replay_approved_rules(tmp_path):
    rules = [
        {
            "tool": "bash",
            "check_type": "input_contains",
            "pattern": "dangerous token",
            "status": "active",
        },
        {
            "tool": "bash",
            "check_type": "input_contains",
            "pattern": "candidate phrase",
            "status": "candidate",
        },
    ]
    gate = PreGate(tmp_path, rules=rules)

    active = {
        "session_id": "s",
        "attempt_id": "a",
        "agent": "codex",
        "phase": "pre",
        "tool": "bash",
        "input": {"command": "echo dangerous token"},
        "ts": "2026-09-26T12:00:00+00:00",
    }
    candidate = {**active, "input": {"command": "echo candidate phrase"}}

    assert gate.check(active).reason == "learned rule: dangerous token"
    assert gate.check(active).action == "block"
    assert gate.check(candidate).action == "allow"
