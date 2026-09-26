"""Deterministic score-gap review implementing the shared Reviewer port."""

from dataclasses import dataclass
from math import isfinite

from tokeneyezed.ports import AttemptResult, Review, Score


def _valid_rate(value: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isfinite(value) and 0 <= value <= 1)
    )


def _valid_score(score: Score) -> bool:
    return (
        _valid_rate(score.visible_pass)
        and _valid_rate(score.val_pass)
        and all(
            {"visible", "val"} <= rates.keys()
            and _valid_rate(rates["visible"])
            and _valid_rate(rates["val"])
            for rates in score.per_section.values()
        )
    )


@dataclass(frozen=True)
class GamingReviewer:
    """Flag suspicious divergence, not proof of cheating.

    Rates use fractions, not percentages. Compare against the last clean score
    supplied by the controller; no mutable history survives between calls.
    The port supplies only a diff summary, so this does not inspect source code
    or claim to detect hardcoded test cases.
    """

    min_visible_gain: float = 0.05
    max_validation_gain: float = 0.005

    def __post_init__(self) -> None:
        if not _valid_rate(self.min_visible_gain) or self.min_visible_gain == 0:
            raise ValueError("min_visible_gain must be in (0, 1]")
        if not _valid_rate(self.max_validation_gain):
            raise ValueError("max_validation_gain must be in [0, 1]")
        if self.max_validation_gain >= self.min_visible_gain:
            raise ValueError("validation tolerance must be smaller than visible gain threshold")

    def review(self, result: AttemptResult, score: Score, previous: Score | None) -> Review:
        reasons: list[str] = []
        if result.exit_code != 0:
            reasons.append(f"attempt did not finish successfully (exit code {result.exit_code})")
        try:
            if not _valid_score(score) or (previous is not None and not _valid_score(previous)):
                reasons.append("invalid score data; cannot safely admit attempt to memory")
                return Review(True, tuple(reasons))
            if previous is not None:
                pairs = [
                    (
                        "overall",
                        score.visible_pass,
                        score.val_pass,
                        previous.visible_pass,
                        previous.val_pass,
                    )
                ]
                for section in sorted(score.per_section.keys() & previous.per_section.keys()):
                    current, old = score.per_section[section], previous.per_section[section]
                    pairs.append(
                        (section, current["visible"], current["val"], old["visible"], old["val"])
                    )
                for name, visible, validation, old_visible, old_validation in pairs:
                    visible_gain, validation_gain = (
                        visible - old_visible,
                        validation - old_validation,
                    )
                    if (
                        visible_gain + 1e-12 >= self.min_visible_gain
                        and validation_gain <= self.max_validation_gain + 1e-12
                    ):
                        reasons.append(
                            f"possible gaming ({name}): visible gained {visible_gain:.4f} "
                            f"while validation changed {validation_gain:+.4f}"
                        )
        except (AttributeError, TypeError, ValueError, KeyError):
            reasons.append("invalid score data; cannot safely admit attempt to memory")
        return Review(flagged=bool(reasons), reasons=tuple(reasons))
