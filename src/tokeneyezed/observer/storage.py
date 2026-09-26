"""Adapt hook JSON to the data workstream's event writer."""

from datetime import UTC, datetime

from pymongo import timeout

from tokeneyezed.data.writes import insert_event

from .core import validate_event


class MongoEventWriter:
    """Callable for make_server; database access stays in data.writes.

    Keep the storage deadline shorter than the shim's three-second HTTP timeout.
    Failures propagate so the shim blocks pre events and spools for backfill.
    """

    def __init__(self, db=None):
        self.db = db

    def __call__(self, event: dict) -> None:
        validate_event(event)
        timestamp = datetime.fromisoformat(event["ts"])
        if timestamp.tzinfo is None:
            raise ValueError("event timestamp must include a timezone")
        with timeout(2):
            insert_event(
                session_id=event["session_id"],
                attempt_id=event["attempt_id"],
                agent=event["agent"],
                phase=event["phase"],
                tool=event["tool"],
                input=event["input"],
                output_summary=event.get("output_summary"),
                verdict=event["verdict"],
                ts=timestamp.astimezone(UTC),
                db=self.db,
            )
