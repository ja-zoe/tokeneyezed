"""Observer operator commands exposed through the `tokeneyezed` CLI."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from tokeneyezed.data.db import get_db
from tokeneyezed.data.rules import load_learning_events, load_rules, store_learned_rules
from tokeneyezed.observer.learner import learn_rules


def _minimum_support(value: str) -> int:
    try:
        support = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer of at least 2") from exc
    if support < 2:
        raise argparse.ArgumentTypeError("must be an integer of at least 2")
    return support


def _positive_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if limit < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return limit


def cmd_rules_learn(args: argparse.Namespace) -> int:
    try:
        db = get_db()
        events = load_learning_events(db=db, limit=args.limit)
    except Exception as exc:
        print(f"rules learn failed: {exc}", file=sys.stderr)
        return 1
    labeled_pre_events = [
        event
        for event in events
        if event.get("phase") == "pre"
        and isinstance(event.get("verdict"), str)
        and (event["verdict"].startswith("block:") or event["verdict"] == "allow")
    ]
    if not labeled_pre_events:
        print("no labeled pre-tool events found; no rules changed")
        return 0

    try:
        rules = learn_rules(
            events,
            load_rules(db=db),
            min_support=args.min_support,
        )
        if rules:
            store_learned_rules(rules, db=db)
    except Exception as exc:
        print(f"rules learn failed: {exc}", file=sys.stderr)
        return 1
    if not rules:
        print("no repeated flag patterns found")
        return 0

    counts = {
        status: sum(rule["status"] == status for rule in rules)
        for status in ("active", "candidate", "retired")
    }
    print(
        f"replayed {len(rules)} rules against {len(labeled_pre_events)} labeled pre-tool events "
        f"({counts['active']} active, {counts['candidate']} candidates, "
        f"{counts['retired']} retired)"
    )
    for rule in rules:
        replay = rule["replay"]
        print(
            f"{rule['status']:9} {rule['tool']:5} {rule['pattern']} "
            f"(flagged={replay['hits_on_flagged']}, good={replay['hits_on_good']}, "
            f"unlabeled={replay['hits_on_unlabeled']})"
        )
    return 0


def register(subparsers: Any) -> None:
    rules = subparsers.add_parser("rules", help="learn and inspect observer rules")
    rule_commands = rules.add_subparsers(dest="rules_command", required=True)
    learn = rule_commands.add_parser(
        "learn", help="cluster repeated observer flags and replay-test candidate rules"
    )
    learn.add_argument(
        "--min-support",
        type=_minimum_support,
        default=2,
        help="minimum blocked events required to activate",
    )
    learn.add_argument(
        "--limit", type=_positive_limit, default=10_000, help="maximum recent events to replay"
    )
    learn.set_defaults(func=cmd_rules_learn)
