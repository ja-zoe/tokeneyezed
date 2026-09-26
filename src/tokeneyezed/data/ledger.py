"""The Atlas-backed Ledger port (controller/ports.py). Thin adapter over the writes.py helpers.

Structural typing: the controller's `Ledger` Protocol is satisfied without importing it, so `data/`
doesn't depend on `controller/`. Contract tests in tests/contracts/test_ports.py check the fit.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pymongo.database import Database

from tokeneyezed.data import writes
from tokeneyezed.data.embeddings import Embedder


class MongoLedger:
    def __init__(self, db: Database | None = None, embedder: Embedder | None = None) -> None:
        self._db = db
        self._embedder = embedder

    def open_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
        number: int,
        goal_id: str,
        agent: str,
        intent: str,
        parent_attempt: str | None,
    ) -> None:
        writes.open_attempt(
            session_id=session_id,
            attempt_id=attempt_id,
            number=number,
            goal_id=goal_id,
            agent=agent,
            intent=intent,
            parent_attempt=parent_attempt,
            db=self._db,
        )

    def close_attempt(
        self,
        *,
        attempt_id: str,
        result: Any,  # AttemptResult
        score: Any,  # Score
        outcome: str,
        observer_flags: Sequence[str],
    ) -> None:
        writes.close_attempt(
            attempt_id=attempt_id,
            diff_summary=result.diff_summary,
            commit=result.commit,
            visible_pass=score.visible_pass,
            val_pass=score.val_pass,
            per_section={k: dict(v) for k, v in score.per_section.items()},
            outcome=outcome,
            observer_flags=list(observer_flags),
            db=self._db,
            embedder=self._embedder,
        )

    def mark_running_as_killed(self, session_id: str) -> list[str]:
        return writes.mark_running_as_killed(session_id, db=self._db)

    def last_clean_commit(self, session_id: str) -> str | None:
        return writes.last_clean_commit(session_id, db=self._db)
