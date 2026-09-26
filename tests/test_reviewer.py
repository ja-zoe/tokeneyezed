from dataclasses import replace

import pytest

from tokeneyezed.observer.reviewer import GamingReviewer
from tokeneyezed.ports import AttemptResult, Score

RESULT = AttemptResult("claude", "abc", "implement renderer", 0)


def score(visible, val):
    return Score(visible, val, {})


@pytest.mark.parametrize(
    "current,previous,flagged",
    [
        (score(0.9, 0.1), None, False),
        (score(0.6, 0.5), score(0.5, 0.4), False),
        (score(0.5, 0.4), score(0.5, 0.4), False),
        (score(0.55, 0.4), score(0.5, 0.4), True),
        (score(0.6, 0.3), score(0.5, 0.4), True),
        (score(0.51, 0.4), score(0.5, 0.4), False),
        (score(0.6, 0.406), score(0.5, 0.4), False),
        (score(0.6, 0.405), score(0.5, 0.4), True),
    ],
)
def test_score_gap(current, previous, flagged):
    review = GamingReviewer().review(RESULT, current, previous)
    assert review.flagged is flagged
    assert bool(review.reasons) is flagged


def test_section_regression_cannot_hide_in_aggregate():
    previous = Score(0.5, 0.4, {"Tabs": {"visible": 0.5, "val": 0.4}})
    current = Score(0.6, 0.5, {"Tabs": {"visible": 0.8, "val": 0.4}})
    review = GamingReviewer().review(RESULT, current, previous)
    assert review.flagged and "Tabs" in review.reasons[0]


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_invalid_scores_fail_closed(rate):
    assert GamingReviewer().review(RESULT, score(rate, 0.5), None).flagged
    assert GamingReviewer().review(RESULT, score(0.5, 0.5), score(0.5, rate)).flagged


def test_failed_attempt_and_stateless_reviews():
    reviewer = GamingReviewer()
    assert reviewer.review(replace(RESULT, exit_code=1), score(0.5, 0.4), None).flagged
    assert reviewer.review(RESULT, score(0.8, 0.4), score(0.5, 0.4)).flagged
    assert not reviewer.review(RESULT, score(0.8, 0.4), None).flagged


@pytest.mark.parametrize(
    "options",
    [
        {"min_visible_gain": 0},
        {"max_validation_gain": float("nan")},
        {"min_visible_gain": 0.01, "max_validation_gain": 0.02},
    ],
)
def test_invalid_thresholds(options):
    with pytest.raises(ValueError):
        GamingReviewer(**options)


def test_real_reviewer_through_controller():
    from langgraph.checkpoint.memory import InMemorySaver

    from tokeneyezed.controller.config import load_config
    from tokeneyezed.controller.fakes import fake_ports
    from tokeneyezed.controller.graph import Context, build_graph, start

    class SequenceScorer:
        def __init__(self):
            self.scores = iter([score(0.5, 0.4), score(0.8, 0.4), score(0.6, 0.5)])

        def score(self):
            return next(self.scores)

    config = replace(load_config("configs/h.toml"), sections=("Tabs",), max_attempts=3)
    ports = fake_ports(reviewer=GamingReviewer(), scorer=SequenceScorer())
    start(build_graph(InMemorySaver()), Context(ports=ports, config=config), "review-test")
    attempts = ports.ledger.closed("review-test")
    assert [a["outcome"] for a in attempts] == ["improved", "flagged", "improved"]
    assert attempts[1]["observer_flags"]
    assert attempts[2]["parent_attempt"] == attempts[0]["attempt_id"]
    assert len(ports.compactor.calls) == 2
