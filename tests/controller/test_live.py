"""The live feed is the demo interface: its markers must appear, in order, for each beat."""

import io
import re
from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tokeneyezed.controller.config import load_config
from tokeneyezed.controller.fakes import FakeReviewer, FakeRunner, ScriptedScorer, fake_ports
from tokeneyezed.controller.graph import Context, build_graph, resume, start
from tokeneyezed.controller.live import LiveFeed
from tokeneyezed.ports import AttemptKilled

CONFIG = replace(load_config("configs/h.toml"), sections=("Emphasis", "Links"), max_attempts=12)


def test_feed_shows_every_demo_beat_across_kill_and_resume():
    scorer = ScriptedScorer(val_script=(0.42, 0.58, 0.71, 0.58, 0.58, 0.58, 0.88, 0.60, 0.9))
    claude = fake_ports(
        scorer=scorer,
        reviewer=FakeReviewer(flag_calls=frozenset({3})),
        runner=FakeRunner(agent="claude", kill_on_call=9),
    )
    graph, out = build_graph(InMemorySaver()), io.StringIO()
    feed = LiveFeed(agent="claude", out=out, color=False)
    with pytest.raises(AttemptKilled):
        start(graph, Context(claude, CONFIG), "s", feed)
    assert feed.done == 8  # the KILLED banner reports attempt #009

    codex = fake_ports(
        agent="codex",
        goals=claude.goals,
        ledger=claude.ledger,
        scorer=scorer,
        compactor=claude.compactor,
        runner=FakeRunner(agent="codex"),
    )
    feed2 = LiveFeed(agent="codex", out=out, color=False)
    resume(
        graph,
        Context(codex, replace(CONFIG, agent="codex")),
        "s",
        feed2,
        lambda killed, state, nxt: feed2.resumed("s", "codex", killed, state, nxt),
    )

    text = out.getvalue()
    beats = [
        r"#001  claude  Emphasis",
        r"first score  0\.42",
        r"improved  best 0\.42 -> 0\.58",
        r"#003  claude  Emphasis[\s\S]*FLAGGED",
        r"REPLAN  Emphasis",
        r"GOAL COMPLETE  Emphasis \(0\.88\)",
        r"#009  claude  Links",
        r"RESUMED s",
        r"agent now      codex",
        r"attempts done  8",
        r"best val 0\.60  Links",
        r"#009  codex   Links",
        r"improved  best 0\.60 -> 0\.90",
        r"GOAL COMPLETE  Links \(0\.90\)",
    ]
    position = 0
    for beat in beats:
        match = re.compile(beat).search(text, position)
        assert match, f"missing or out of order: {beat!r}\n\n{text}"
        position = match.start()


def test_color_only_when_asked():
    out = io.StringIO()
    LiveFeed(out=out, color=False).banner("x")
    LiveFeed(out=out).banner("y")  # StringIO is not a TTY
    assert "\033[" not in out.getvalue()
    colored = io.StringIO()
    LiveFeed(out=colored, color=True).banner("z")
    assert "\033[" in colored.getvalue()
