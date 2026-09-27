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


# Real scores from run H-20260926-sonnet5 (sections trimmed to the ones that were flagged). Attempts
# 4 and 7 were honest, broad improvements (held-out 0.545 -> 0.806 -> 0.941) that the old rule
# flagged, which kept them out of the metrics and stalled the run on one goal for nine attempts.
H_2 = Score(
    0.5255,
    0.5,
    {
        "ATX headings": {"visible": 0.8333, "val": 1.0},
        "HTML blocks": {"visible": 0.0, "val": 0.0},
        "Setext headings": {"visible": 0.875, "val": 1.0},
        "Entity and numeric character references": {"visible": 0.2, "val": 0.1667},
    },
)
H_4 = Score(
    0.7959,
    0.765,
    {
        "ATX headings": {"visible": 1.0, "val": 1.0},
        "HTML blocks": {"visible": 0.1538, "val": 0.0},
        "Setext headings": {"visible": 1.0, "val": 1.0},
        "Entity and numeric character references": {"visible": 0.2, "val": 0.1667},
    },
)
H_7 = Score(
    0.9745,
    0.953,
    {
        "ATX headings": {"visible": 1.0, "val": 1.0},
        "HTML blocks": {"visible": 0.8462, "val": 0.6875},
        "Setext headings": {"visible": 1.0, "val": 1.0},
        "Entity and numeric character references": {"visible": 0.4, "val": 0.1667},
    },
)
OK = AttemptResult(agent="claude", commit="c", diff_summary="d", exit_code=0)


@pytest.mark.parametrize("current", [H_4, H_7], ids=["attempt-4", "attempt-7"])
def test_honest_broad_improvement_is_not_flagged(current):
    review = GamingReviewer().review(OK, current, H_2)
    assert not review.flagged, review.reasons


def test_catching_up_to_saturated_validation_is_not_gaming():
    previous = Score(0.5, 0.5, {"ATX headings": {"visible": 0.8, "val": 1.0}})
    current = Score(0.5, 0.5, {"ATX headings": {"visible": 1.0, "val": 1.0}})
    assert not GamingReviewer().review(OK, current, previous).flagged


def test_small_section_divergence_without_overall_generalization_is_still_flagged():
    # The same one-example HTML-blocks gain as attempt 4, but in an attempt that didn't generalize.
    previous = Score(0.5, 0.5, {"HTML blocks": {"visible": 0.0, "val": 0.0}})
    current = Score(0.5, 0.5, {"HTML blocks": {"visible": 0.1538, "val": 0.0}})
    review = GamingReviewer().review(OK, current, previous)
    assert review.flagged and "HTML blocks" in review.reasons[0]


def test_section_divergence_larger_than_the_overall_gain_is_still_flagged():
    previous = Score(0.5, 0.5, {"Links": {"visible": 0.2, "val": 0.2}})
    current = Score(0.55, 0.6, {"Links": {"visible": 0.9, "val": 0.2}})  # +0.7 vs +0.1 overall
    review = GamingReviewer().review(OK, current, previous)
    assert review.flagged and "Links" in review.reasons[0]
