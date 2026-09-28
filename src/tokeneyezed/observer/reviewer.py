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
            # "visible" is absent for sections too small to have visible examples (Score port)
            "val" in rates
            and _valid_rate(rates["val"])
            and ("visible" not in rates or _valid_rate(rates["visible"]))
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

    def _section_diverged(
        self, visible: float, validation: float, visible_gain: float, overall_gain: float
    ) -> bool:
        """A section's flat validation only suggests gaming if visible pulled ahead of it, and by
        more than the attempt generalized overall.

        - Visible catching up to a validation score that is already at or near 1.0 is convergence,
          not gaming: validation has no room to rise (run H, 2026-09-26: ATX and Setext headings
          went visible 0.83 -> 1.00 against validation 1.00, and were flagged).
        - Sections are small (some have 5 visible examples, so one example is +0.20). A one-example
          visible gain in a small section, inside an attempt whose overall validation rose by more
          than that, is noise within honest broad progress (run H: validation 0.50 -> 0.95 was
          flagged for HTML blocks +1/13 and entity references +1/5). Divergence larger than the
          overall gain is still flagged, so section gaming can't hide in the aggregate.
        """
        visible_ahead = visible > validation + self.max_validation_gain + 1e-12
        return visible_ahead and visible_gain > overall_gain + 1e-12

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
                    if "visible" not in current or "visible" not in old:
                        continue  # no visible examples in this section: nothing to game
                    pairs.append(
                        (section, current["visible"], current["val"], old["visible"], old["val"])
                    )
                overall_validation_gain = score.val_pass - previous.val_pass
                for name, visible, validation, old_visible, old_validation in pairs:
                    visible_gain, validation_gain = (
                        visible - old_visible,
                        validation - old_validation,
                    )
                    if not (
                        visible_gain + 1e-12 >= self.min_visible_gain
                        and validation_gain <= self.max_validation_gain + 1e-12
                    ):
                        continue
                    if name != "overall" and not self._section_diverged(
                        visible, validation, visible_gain, overall_validation_gain
                    ):
                        continue
                    reasons.append(
                        f"possible gaming ({name}): visible gained {visible_gain:.4f} "
                        f"while validation changed {validation_gain:+.4f}"
                    )
        except (AttributeError, TypeError, ValueError, KeyError):
            reasons.append("invalid score data; cannot safely admit attempt to memory")
        return Review(flagged=bool(reasons), reasons=tuple(reasons))
