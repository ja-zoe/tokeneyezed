"""The real ports for a run: every workstream's Atlas- and agent-backed implementation.

The CLI is the only place that assembles them (graph nodes only see the Ports protocol). Machine-
specific paths come from .env; shared behavior from configs/.
"""

from __future__ import annotations

import os
from pathlib import Path

from tokeneyezed.controller.config import RunConfig, agent_model
from tokeneyezed.controller.planner import OpenRouterPlanner
from tokeneyezed.controller.runners import runner_for
from tokeneyezed.data.wiring import data_ports
from tokeneyezed.eval.scoring import SpecScorer
from tokeneyezed.observer.reviewer import GamingReviewer
from tokeneyezed.ports import Ports


def hidden_split(name: str) -> Path:
    hidden = os.environ.get("TOKENEYEZED_HIDDEN_DIR")
    if not hidden:
        raise SystemExit("TOKENEYEZED_HIDDEN_DIR is not set: the split's harness-side directory")
    path = Path(hidden).expanduser() / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run the split, and copy visible.json there too")
    return path


def real_ports(config: RunConfig) -> Ports:
    runner = runner_for(config.agent, config)
    return Ports(
        **data_ports(),
        planner=OpenRouterPlanner(model=agent_model(config, "planner")),
        runner=runner,
        reviewer=GamingReviewer(),
        scorer=SpecScorer(
            workspace=runner.paths.workspace,
            visible=hidden_split("visible"),
            validation=hidden_split("validation"),
        ),
    )
