"""The one MongoDB connection. Other packages go through data/ helpers, never raw Mongo calls."""

from __future__ import annotations

import os

from pymongo import MongoClient
from pymongo.database import Database

DEFAULT_DB_NAME = "tokeneyezed"

_client: MongoClient | None = None


def get_db() -> Database:
    """The project database, from MONGODB_URI (and optionally TOKENEYEZED_DB)."""
    global _client
    if _client is None:
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI is not set; copy .env.example to .env and fill it in")
        _client = MongoClient(uri, appname="tokeneyezed")
    return _client[os.environ.get("TOKENEYEZED_DB") or DEFAULT_DB_NAME]
