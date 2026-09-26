import json
import threading

from tokeneyezed.observer.core import Decision, PreGate
from tokeneyezed.observer.postcheck import PostChecker
from tokeneyezed.observer.service import make_server
from tokeneyezed.observer.shim import from_decision, handle, to_event


def post_event(tool="read", inputs=None, output=None, attempt_id="a1"):
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": {"bash": "Bash", "edit": "Edit", "write": "Write", "read": "Read"}[tool],
        "tool_input": inputs or {},
    }
    if output is not None:
        payload["tool_response"] = output
    return to_event(
        payload,
        {"TOKENEYEZED_SESSION_ID": "s1", "TOKENEYEZED_ATTEMPT_ID": attempt_id},
    )


def test_notes_on_edits_outside_explicit_intent_scope():
    checker = PostChecker()
    event = post_event("edit", {"file_path": "src/other.py", "content": "x = 1"})

    decision = checker.check(event, "Only edit src/parser.py")

    assert decision.action == "note"
    assert "outside the explicit file scope" in decision.reason
    assert "src/other.py" in decision.reason


def test_notes_on_explicitly_excluded_edit_path():
    checker = PostChecker()
    event = post_event("write", {"file_path": "tests/test_parser.py", "content": "x = 1"})

    decision = checker.check(event, "Fix src/parser.py without touching tests/test_parser.py")

    assert decision.action == "note"
    assert "explicitly excluded" in decision.reason


def test_repeated_identical_failed_tool_input_gets_one_note():
    checker = PostChecker()
    event = post_event(
        "bash",
        {"command": "uv run pytest tests/test_parser.py"},
        {"exit_code": 1, "stderr": "1 failed"},
    )

    assert checker.check(event).action == "allow"
    second = checker.check(event)
    assert second.action == "note"
    assert "failed twice" in second.reason
    assert checker.check(event).action == "allow"


def test_no_progress_note_waits_for_threshold_and_success_resets_it():
    checker = PostChecker(no_progress_limit=3)
    read = post_event()

    assert checker.check(read).action == "allow"
    assert checker.check(read).action == "allow"
    assert checker.check(read).action == "note"
    assert checker.check(read).action == "allow"

    successful_test = post_event(
        "bash", {"command": "uv run pytest"}, {"exit_code": 0, "stdout": "3 passed"}
    )
    assert checker.check(successful_test).action == "allow"
    assert checker.check(read).action == "allow"
    assert checker.check(read).action == "allow"
    assert checker.check(read).action == "note"


def test_successful_edit_resets_no_progress_and_stop_clears_attempt():
    checker = PostChecker(no_progress_limit=3)
    read = post_event()
    assert checker.check(read).action == "allow"
    assert checker.check(read).action == "allow"
    assert checker.check(post_event("edit", {"file_path": "src/parser.py"})).action == "allow"
    assert checker.check(read).action == "allow"
    assert checker.check(read).action == "allow"

    checker.finish_attempt({"session_id": "s1", "attempt_id": "a1"})
    assert checker.check(read).action == "allow"


def test_codex_tool_output_is_preserved_for_failure_checks():
    event = to_event(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_output": {"exit_code": 1, "stderr": "1 failed"},
        },
        {"TOKENEYEZED_SESSION_ID": "s1", "TOKENEYEZED_ATTEMPT_ID": "a1"},
        agent="codex",
    )
    checker = PostChecker()

    assert checker.check(event).action == "allow"
    assert "failed twice" in checker.check(event).reason


def test_codex_post_note_replaces_completed_tool_result_with_feedback():
    code, stdout, stderr = from_decision(
        Decision("note", "Change the failing command before retrying."),
        "post",
        "codex",
    )

    response = json.loads(stdout)
    assert code == 0 and not stderr
    assert response["decision"] == "block"
    assert response["reason"] == "Change the failing command before retrying."
    assert response["hookSpecificOutput"]["additionalContext"] == response["reason"]


def test_post_hook_returns_note_and_persists_contract_event(tmp_path):
    records = []
    server = make_server(PreGate(tmp_path), "secret", records.append, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "src/other.py", "content": "x = 1"},
        "tool_response": {"content": "saved"},
    }
    env = {
        "TOKENEYEZED_SESSION_ID": "s1",
        "TOKENEYEZED_ATTEMPT_ID": "a1",
        "TOKENEYEZED_INTENT": "Only edit src/parser.py",
        "TOKENEYEZED_OBSERVER_URL": f"http://127.0.0.1:{server.server_port}/event",
        "TOKENEYEZED_OBSERVER_TOKEN": "secret",
        "TOKENEYEZED_OBSERVER_SPOOL": str(tmp_path / "spool.jsonl"),
    }
    try:
        code, stdout, stderr = handle(payload, env)
        assert code == 0
        assert not stderr
        assert "additionalContext" in json.loads(stdout)["hookSpecificOutput"]
        assert records[0]["verdict"].startswith("note:")
        assert "intent" not in records[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
