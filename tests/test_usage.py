"""Token usage: parsed from the agent's stream-json transcript and stored on the attempt."""

import json

from mongo_fakes import FakeDB, embedder

from tokeneyezed.controller.runners.usage import read_claude_usage
from tokeneyezed.data.ledger import MongoLedger
from tokeneyezed.ports import AttemptResult, Score


def assistant(msg_id, *, inp=0, create=0, read=0, out=0):
    usage = {
        "input_tokens": inp,
        "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read,
        "output_tokens": out,
    }
    return json.dumps({"type": "assistant", "message": {"id": msg_id, "usage": usage}})


def write(tmp_path, *lines):
    path = tmp_path / "a.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_sums_every_call_and_finds_the_peak_window(tmp_path):
    path = write(
        tmp_path,
        assistant("m1", inp=10, create=5_000, read=0, out=200),
        assistant("m2", inp=5, create=300, read=5_000, out=150),
        assistant("m3", inp=5, create=100, read=5_300, out=50),
    )
    usage = read_claude_usage(path)
    windows = [5_010, 5_305, 5_405]  # input + cache creation + cache read at each call
    assert usage["calls"] == 3
    assert usage["peak_context"] == max(windows)
    assert usage["tokens_processed"] == sum(windows) + 400
    assert usage["output_tokens"] == 400
    assert usage["cache_read_tokens"] == 10_300
    assert usage["input_tokens"] == 20 + 5_400


def test_a_message_split_across_blocks_counts_once_with_its_last_report(tmp_path):
    path = write(
        tmp_path,
        assistant("m1", inp=10, read=1_000, out=5),
        assistant("m1", inp=10, read=1_000, out=90),  # same message id, final output count
    )
    usage = read_claude_usage(path)
    assert usage["calls"] == 1 and usage["output_tokens"] == 90


def test_no_usage_is_none_not_zero(tmp_path):
    assert read_claude_usage(tmp_path / "missing.jsonl") is None
    assert read_claude_usage(write(tmp_path, "not json", "{}")) is None
    # A login failure reports an assistant message with all-zero usage: still no usage.
    assert read_claude_usage(write(tmp_path, assistant("m1"))) is None


def test_garbage_lines_are_skipped(tmp_path):
    path = write(tmp_path, "{oops", assistant("m1", inp=1, read=99, out=1), '{"type": "system"}')
    assert read_claude_usage(path)["tokens_processed"] == 101


def test_the_ledger_stores_usage_on_the_attempt_when_the_runner_reports_it():
    db = FakeDB()
    ledger = MongoLedger(db=db, embedder=embedder())
    score = Score(0.5, 0.4, {"g": {"visible": 0.5, "val": 0.4}})
    usage = {"calls": 3, "peak_context": 5_405, "tokens_processed": 15_120}
    for attempt_id, reported in (("a-1", usage), ("a-2", None)):
        ledger.open_attempt(
            session_id="s", attempt_id=attempt_id, number=1, goal_id="g", agent="claude",
            intent="intent", parent_attempt=None,
        )  # fmt: skip
        result = AttemptResult("claude", "c", "d", 0, usage=reported)
        ledger.close_attempt(
            attempt_id=attempt_id, result=result, score=score, outcome="improved", observer_flags=[]
        )
    first, second = db["attempts"].docs
    assert first["usage"] == usage
    assert "usage" not in second  # unknown stays absent, so the dashboard shows "not recorded"
