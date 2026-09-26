"""Deterministic candidate-rule learning and replay against recorded pre-tool events."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any

CHECK_TYPE = "input_contains"
_TOKEN = re.compile(r"-{0,2}[a-z0-9_]+(?:[.-][a-z0-9_]+)*", re.IGNORECASE)
_STOP_WORDS = {"a", "an", "and", "as", "class", "def", "from", "import", "in", "the"}
_MAX_PHRASE_TOKENS = 6
_MAX_INPUT_CHARS = 16_384
_MAX_INPUT_TOKENS = 128
_MAX_PATTERN_CHARS = 160


def _input_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value[:_MAX_INPUT_CHARS]
    elif isinstance(value, Mapping):
        for key in sorted(value):
            yield from _input_values(value[key])
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _input_values(item)


def input_tokens(value: Any) -> tuple[str, ...]:
    """Normalize event input into tokens, preserving literal punctuation within tokens."""
    return tuple(
        islice(
            (token.lower() for text in _input_values(value) for token in _TOKEN.findall(text)),
            _MAX_INPUT_TOKENS,
        )
    )


def _event_id(event: Mapping[str, Any], index: int) -> str:
    return str(event.get("_id", event.get("event_id", f"event-{index}")))


def _verdict_label(verdict: Any) -> str | None:
    if not isinstance(verdict, str):
        return None
    if verdict == "allow" or verdict.startswith("allow:"):
        return "good"
    if verdict.startswith("block:"):
        return "flagged"
    return None


def _matches(rule: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    if rule.get("check_type") != CHECK_TYPE or rule.get("tool") != event.get("tool"):
        return False
    pattern_tokens = tuple(str(rule.get("pattern", "")).lower().split())
    event_tokens = input_tokens(event.get("input"))
    if not pattern_tokens or len(pattern_tokens) > len(event_tokens):
        return False
    return any(
        event_tokens[offset : offset + len(pattern_tokens)] == pattern_tokens
        for offset in range(len(event_tokens) - len(pattern_tokens) + 1)
    )


def matches_active_rule(event: Mapping[str, Any], rules: Iterable[Mapping[str, Any]]) -> str | None:
    """Return a learned-rule reason for the first matching active rule, if any."""
    for rule in rules:
        if rule.get("status") == "active" and _matches(rule, event):
            return str(rule["pattern"])
    return None


def _ngrams(tokens: tuple[str, ...]) -> set[tuple[str, ...]]:
    phrases = set()
    for size in range(1, min(_MAX_PHRASE_TOKENS, len(tokens)) + 1):
        for offset in range(len(tokens) - size + 1):
            phrase = tokens[offset : offset + size]
            word = phrase[0].lstrip("-") if size == 1 else ""
            if size == 1 and (len(word) < 4 or word in _STOP_WORDS):
                continue
            if any(token not in _STOP_WORDS for token in phrase):
                phrases.add(phrase)
    return phrases


def _existing_rule_key(rule: Mapping[str, Any]) -> tuple[str, str, str] | None:
    pattern, check_type, tool = rule.get("pattern"), rule.get("check_type"), rule.get("tool")
    if (
        isinstance(pattern, str)
        and pattern.strip()
        and check_type == CHECK_TYPE
        and isinstance(tool, str)
        and tool
    ):
        return tool, check_type, pattern.lower()
    return None


def learn_rules(
    events: Iterable[Mapping[str, Any]],
    existing_rules: Iterable[Mapping[str, Any]] = (),
    *,
    min_support: int = 2,
) -> list[dict[str, Any]]:
    """Cluster repeated blocked inputs, then label-replay every rule against good events.

    A rule activates only after matching at least ``min_support`` previously blocked events and
    zero previously allowed events. Existing rules are re-evaluated so stale active rules retire.
    """
    if type(min_support) is not int or min_support < 2:
        raise ValueError("min_support must be an integer of at least 2")

    records = [
        (index, event, _verdict_label(event.get("verdict")))
        for index, event in enumerate(events, start=1)
        if event.get("phase") == "pre" and isinstance(event, Mapping)
    ]
    labeled = [(index, event, label) for index, event, label in records if label]
    clusters: dict[tuple[str, str], list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for index, event, label in labeled:
        if label != "flagged" or not isinstance(event.get("tool"), str):
            continue
        verdict = event.get("verdict", "")
        reason = verdict.partition(":")[2].strip()
        tokens = input_tokens(event.get("input"))
        if reason and tokens:
            clusters[(event["tool"], reason)].append((index, event))

    candidates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for (tool, _reason), flagged_events in clusters.items():
        if len(flagged_events) < min_support:
            continue
        supports: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for _index, event in flagged_events:
            event_id = _event_id(event, _index)
            for phrase in _ngrams(input_tokens(event.get("input"))):
                supports[phrase].add(event_id)
        supported = {
            phrase: event_ids
            for phrase, event_ids in supports.items()
            if len(event_ids) >= min_support and len(" ".join(phrase)) <= _MAX_PATTERN_CHARS
        }
        redundant = set()
        for phrase, event_ids in supported.items():
            if len(phrase) > 1 and (
                supported.get(phrase[:-1]) == event_ids or supported.get(phrase[1:]) == event_ids
            ):
                redundant.update((phrase[:-1], phrase[1:]))
        for phrase, event_ids in supported.items():
            if phrase not in redundant:
                candidates[(tool, CHECK_TYPE, " ".join(phrase))].update(event_ids)

    prior: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for rule in existing_rules:
        key = _existing_rule_key(rule)
        if key is not None:
            prior[key] = rule
            candidates.setdefault(key, set())

    learned = []
    for (tool, check_type, pattern), candidate_evidence in sorted(candidates.items()):
        rule = {"tool": tool, "check_type": check_type, "pattern": pattern}
        hits_on_flagged = []
        hits_on_good = []
        hits_on_unlabeled = []
        for index, event, label in records:
            if _matches(rule, event):
                if label == "flagged":
                    hits_on_flagged.append(_event_id(event, index))
                elif label == "good":
                    hits_on_good.append(_event_id(event, index))
                else:
                    hits_on_unlabeled.append(_event_id(event, index))
        supported = len(hits_on_flagged) >= min_support
        status = (
            "active" if supported and not hits_on_good and not hits_on_unlabeled else "candidate"
        )
        if not supported:
            status = "retired" if (tool, check_type, pattern) in prior else "candidate"
        old = prior.get((tool, check_type, pattern), {})
        evidence = hits_on_flagged or sorted(candidate_evidence)
        learned.append(
            {
                **rule,
                "evidence_event_ids": sorted(set(evidence)),
                "replay": {
                    "hits_on_flagged": len(hits_on_flagged),
                    "hits_on_good": len(hits_on_good),
                    "hits_on_unlabeled": len(hits_on_unlabeled),
                    "minimum_support": min_support,
                },
                "status": status,
                "version": old.get("version", 1),
            }
        )
    return learned
