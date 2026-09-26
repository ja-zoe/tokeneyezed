"""Run configuration, loaded from TOML in configs/.

A run file names a base file with `extends` and overrides only what differs, so everything that
must match across B, H, and H-mem (model, budget, timebox) lives in one place (invariant I7).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Keys a run file may set. Everything else must come from the base, so runs stay comparable.
RUN_SPECIFIC_KEYS = frozenset({"name", "agent", "memory", "extends"})


@dataclass(frozen=True)
class RunConfig:
    name: str
    agent: str
    models: Mapping[str, str]  # agent ("claude", "codex", ...) or "planner" -> model id
    memory: bool
    max_attempts: int
    max_turns: int
    failure_threshold: int
    target_val_pass: float
    timebox_minutes: int
    allowed_tools: tuple[str, ...]
    sections: tuple[str, ...]


def load_config(path: str | Path) -> RunConfig:
    path = Path(path)
    run = tomllib.loads(path.read_text())
    base: dict[str, Any] = {}
    if "extends" in run:
        base = tomllib.loads((path.parent / run["extends"]).read_text())
        overridden = set(run) - RUN_SPECIFIC_KEYS
        if overridden:
            raise ValueError(
                f"{path.name} overrides {sorted(overridden)}; only {sorted(RUN_SPECIFIC_KEYS)} "
                "may differ between runs (put shared settings in the base file)"
            )
    merged = {**base, **run}
    merged.pop("extends", None)
    merged["sections"] = tuple(merged["sections"])
    merged["allowed_tools"] = tuple(merged["allowed_tools"])
    merged["models"] = dict(merged["models"])
    return RunConfig(**merged)


def agent_model(config: RunConfig, agent: str) -> str:
    """The pinned model for an agent. Refuses to guess: an unset model is a config error."""
    model = config.models.get(agent, "")
    if not model:
        raise ValueError(
            f"no pinned model for agent {agent!r}: set [models].{agent} in configs/base.toml"
        )
    return model
