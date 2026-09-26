"""SpecScorer: the Scorer port over eval/scorer.py (docs/specs/eval-runs.md).

Scores visible from the harness-side copy of the split (an edited copy in the workspace can't
inflate it) plus validation. Never the held-out split: that is scored after the run (heldout.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokeneyezed.ports import Score


def _score_attempt(**kwargs: Any) -> dict:
    from tokeneyezed.eval.scorer import score_attempt  # lands with PR #16

    return score_attempt(**kwargs)


@dataclass
class SpecScorer:
    workspace: Path
    visible: Path  # harness-side copy, outside the workspace
    validation: Path
    program: str = "python3 render.py"
    jobs: int = 8
    score_fn: Callable[..., dict] = _score_attempt

    def score(self) -> Score:
        out = self.score_fn(
            visible=str(self.visible),
            validation=str(self.validation),
            workspace=str(self.workspace),
            program=self.program,
            jobs=self.jobs,
        )
        per_section = {
            section: {k: v for k, v in rates.items() if v is not None}  # tiny sections: no visible
            for section, rates in out["per_section"].items()
        }
        return Score(
            visible_pass=out["visible_pass"], val_pass=out["val_pass"], per_section=per_section
        )
