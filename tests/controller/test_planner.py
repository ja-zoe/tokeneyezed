"""The real planner (docs/specs/planner.md), on a fake OpenRouter transport."""

import json

import pytest

from tokeneyezed.controller.planner import OpenRouterPlanner
from tokeneyezed.openrouter import OpenRouterClient
from tokeneyezed.ports import Goal

GOAL = Goal(goal_id="s:Emphasis", section="Emphasis and strong emphasis", target_val_pass=0.85)
BRIEF = "## Failed attempts\n- a-003 intent: regex for *emphasis* (val 0.31)"


def planner_replying(*replies, model="anthropic/claude-sonnet-5"):
    calls = []

    def transport(payload, key):
        calls.append(payload)
        reply = replies[min(len(calls), len(replies)) - 1]
        return {"choices": [{"message": {"content": reply}}]}

    client = OpenRouterClient(transport=transport, sleep=lambda s: None)
    return OpenRouterPlanner(model=model, client=client), calls


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def test_plan_returns_the_models_intent_and_sends_goal_brief_and_strategy():
    planner, calls = planner_replying(json.dumps({"intent": "Use a delimiter stack for * and _."}))
    goal = Goal(**{**GOAL.__dict__, "strategy_notes": "Follow spec 6.2's algorithm."})

    assert planner.plan(goal, BRIEF) == "Use a delimiter stack for * and _."
    payload = calls[0]
    assert payload["model"] == "anthropic/claude-sonnet-5"
    assert payload["response_format"] == {"type": "json_object"}
    user = payload["messages"][1]["content"]
    assert "Emphasis and strong emphasis" in user and BRIEF in user
    assert "Current strategy: Follow spec 6.2's algorithm." in user
    assert "different approach from every failed attempt" in payload["messages"][0]["content"]
    assert planner.failures == []


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"intent": "Handle left-flanking runs."}\n```',
        '{"intent": "Handle left-flanking runs."}\nThis keeps link parsing untouched.',
    ],
)
def test_plan_accepts_fenced_json_and_trailing_text(reply):
    planner, _ = planner_replying(reply)
    assert planner.plan(GOAL, BRIEF) == "Handle left-flanking runs."


def test_invalid_reply_gets_one_corrective_retry():
    good = json.dumps({"strategy": "Rewrite inline parsing as a delimiter-run pass."})
    planner, calls = planner_replying(json.dumps({"strategy": "z" * 600}), good)
    assert planner.replan(GOAL, BRIEF) == "Rewrite inline parsing as a delimiter-run pass."
    assert planner.failures == [] and len(calls) == 2
    retry = calls[1]["messages"]
    assert retry[-2]["role"] == "assistant" and "limit 500" in retry[-1]["content"]


@pytest.mark.parametrize(
    "reply, why",
    [
        ("not json", "not a JSON object"),
        (json.dumps({"plan": "x"}), "not a JSON object"),
        (json.dumps({"intent": "  "}), "empty"),
        (json.dumps({"intent": "x" * 301}), "limit 300"),
        (json.dumps({"intent": "pip install markdown-it-py and wrap it"}), "forbidden"),
        (json.dumps({"intent": "Port Mistune's inline parser"}), "forbidden"),
    ],
)
def test_plan_falls_back_on_bad_output(reply, why):
    planner, calls = planner_replying(reply)  # the same bad reply on the retry too
    intent = planner.plan(GOAL, BRIEF)
    assert len(calls) == 2  # one corrective retry before falling back
    assert intent.startswith("Make more of the 'Emphasis and strong emphasis' spec examples pass")
    assert len(planner.failures) == 1 and why in planner.failures[0]


def test_commonmark_itself_is_not_forbidden():
    planner, _ = planner_replying(json.dumps({"intent": "Follow the CommonMark spec for tabs."}))
    assert planner.plan(GOAL, BRIEF) == "Follow the CommonMark spec for tabs."


def test_plan_falls_back_when_the_model_is_down(monkeypatch):
    def down(payload, key):
        raise __import__("urllib.error").error.HTTPError("u", 401, "no", {}, None)

    planner = OpenRouterPlanner(model="m", client=OpenRouterClient(transport=down))
    goal = Goal(**{**GOAL.__dict__, "strategy_notes": "Use a delimiter stack."})
    assert planner.plan(goal, BRIEF).endswith(
        "following the current strategy: Use a delimiter stack."
    )
    assert "HTTP 401" in planner.failures[0]


def test_replan_returns_the_models_strategy_or_falls_back():
    strategy = "Rewrite inline parsing as a delimiter-run pass per spec 6.2."
    planner, calls = planner_replying(json.dumps({"strategy": strategy}))
    assert planner.replan(GOAL, BRIEF) == strategy
    assert "stopped improving" in calls[0]["messages"][0]["content"]

    planner, _ = planner_replying(json.dumps({"strategy": "y" * 501}))
    assert planner.replan(GOAL, BRIEF).startswith("Recent attempts on 'Emphasis")
    assert "limit 500" in planner.failures[0]
