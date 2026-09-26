import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from mongo_fakes import FakeDB

from tokeneyezed.observer.core import PreGate
from tokeneyezed.observer.service import make_server
from tokeneyezed.observer.shim import handle, to_event
from tokeneyezed.observer.storage import MongoEventWriter


def event():
    result = to_event(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "python renderer.py"},
        },
        {"TOKENEYEZED_SESSION_ID": "s", "TOKENEYEZED_ATTEMPT_ID": "a"},
    )
    result["verdict"] = "allow"
    return result


def test_writer_uses_data_helper_and_normalizes_timestamp():
    db = FakeDB()
    payload = event()
    payload["ts"] = "2026-09-26T10:00:00-07:00"
    MongoEventWriter(db)(payload)
    stored = db["events"].docs[0]
    assert stored["ts"] == datetime(2026, 9, 26, 17, tzinfo=UTC)
    assert stored["input"] == payload["input"]
    assert stored["verdict"] == "allow"
    assert isinstance(payload["ts"], str)  # no mutation of caller's JSON


@pytest.mark.parametrize("timestamp", ["invalid", "2026-09-26T10:00:00"])
def test_bad_timestamp_never_written(timestamp):
    db = FakeDB()
    payload = event()
    payload["ts"] = timestamp
    with pytest.raises(ValueError):
        MongoEventWriter(db)(payload)
    assert not db


def test_hook_subprocess_through_http_and_data_writer(tmp_path):
    db = FakeDB()
    server = make_server(PreGate(tmp_path), "test-secret", MongoEventWriter(db), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "TOKENEYEZED_SESSION_ID": "s",
        "TOKENEYEZED_ATTEMPT_ID": "a",
        "TOKENEYEZED_OBSERVER_URL": f"http://127.0.0.1:{server.server_port}/event",
        "TOKENEYEZED_OBSERVER_TOKEN": "test-secret",
        "TOKENEYEZED_OBSERVER_SPOOL": str(tmp_path / "spool.jsonl"),
    }
    try:
        for tool, inputs, expected in (
            ("Bash", {"command": "python renderer.py"}, 0),
            ("Bash", {"command": "echo TOKENEYEZED_HONEYPOT"}, 2),
            ("Write", {"file_path": "renderer.py", "content": "x"}, 0),
            ("Write", {"file_path": "tests/cases.json", "content": "x"}, 2),
        ):
            payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inputs}
            completed = subprocess.run(
                [sys.executable, "-m", "tokeneyezed.observer.shim"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                timeout=10,
            )
            assert completed.returncode == expected, completed.stderr
            assert completed.stdout == ""
        assert len(db["events"].docs) == 4
        assert [doc["verdict"].split(":")[0] for doc in db["events"].docs] == [
            "allow",
            "block",
            "allow",
            "block",
        ]
        with patch("tokeneyezed.observer.storage.insert_event", side_effect=OSError("offline")):
            for phase, code in (("PreToolUse", 2), ("PostToolUse", 0)):
                result = handle(
                    {
                        "hook_event_name": phase,
                        "tool_name": "Bash",
                        "tool_input": {"command": "python renderer.py"},
                    },
                    env,
                )
                assert result[0] == code
        assert len((tmp_path / "spool.jsonl").read_text().splitlines()) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
