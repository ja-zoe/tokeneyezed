"""The real, Atlas-backed implementations of the data workstream's four ports, in one call.

The controller builds its Ports from these plus the other workstreams' ports:

    from tokeneyezed.data.wiring import data_ports
    ports = Ports(**data_ports(), planner=..., runner=..., scorer=..., reviewer=...)

All four share one database handle and one Embedder, so the query-embedding cache and the Voyage
rate limiter and token budget cover the whole run.
"""

from __future__ import annotations

from typing import Any

from pymongo.database import Database

from tokeneyezed.data.brief import MongoBriefBuilder
from tokeneyezed.data.compactor import MongoCompactor
from tokeneyezed.data.db import get_db
from tokeneyezed.data.embeddings import Embedder, get_embedder
from tokeneyezed.data.goals import MongoGoalStore
from tokeneyezed.data.ledger import MongoLedger
from tokeneyezed.data.skills import SkillDistiller


def data_ports(db: Database | None = None, embedder: Embedder | None = None) -> dict[str, Any]:
    """{goals, brief, ledger, compactor}: keyword arguments for tokeneyezed.ports.Ports."""
    db = db if db is not None else get_db()
    embedder = embedder or get_embedder()
    return {
        "goals": MongoGoalStore(db=db, distiller=SkillDistiller(db=db, embedder=embedder)),
        "brief": MongoBriefBuilder(db=db, embedder=embedder),
        "ledger": MongoLedger(db=db, embedder=embedder),
        "compactor": MongoCompactor(db=db, embedder=embedder),
    }
