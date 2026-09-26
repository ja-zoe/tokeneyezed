"""Compactor against the real ledger helpers: only closed, unflagged attempts are compacted."""

from mongo_fakes import FakeDB, embedder

from tokeneyezed.data.compactor import MongoCompactor
from tokeneyezed.data.ledger import MongoLedger
from tokeneyezed.ports import AttemptResult, Score

SCORE = Score(visible_pass=0.6, val_pass=0.5, per_section={"Tabs": {"visible": 0.6, "val": 0.5}})


class RecordingSummarizer:
    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def summarize(self, attempts) -> str:
        self.seen.append([a["attempt_id"] for a in attempts])
        return "Summary of what was tried."


def test_compacts_only_what_the_ledger_marks_closed_and_unflagged() -> None:
    db = FakeDB()
    ledger = MongoLedger(db=db, embedder=embedder())
    outcomes = {1: "improved", 2: "flagged", 3: "no_improvement", 4: "regressed"}
    for n, outcome in outcomes.items():
        ledger.open_attempt(
            session_id="s", attempt_id=f"a-{n}", number=n, goal_id="g", agent="claude",
            intent="intent", parent_attempt=None,
        )  # fmt: skip
        result = AttemptResult(agent="claude", commit=f"c{n}", diff_summary="d", exit_code=0)
        ledger.close_attempt(
            attempt_id=f"a-{n}", result=result, score=SCORE, outcome=outcome, observer_flags=[]
        )
    for n in (5, 6):  # 5 is killed by a resume, 6 is still running
        ledger.open_attempt(
            session_id="s", attempt_id=f"a-{n}", number=n, goal_id="g", agent="claude",
            intent="intent", parent_attempt=None,
        )  # fmt: skip
    db["attempts"].docs[4]["status"] = "killed"
    db["attempts"].docs[4]["outcome"] = "killed"

    summarizer = RecordingSummarizer()
    compactor = MongoCompactor(db=db, embedder=embedder(), summarizer=summarizer)
    compactor.compact("s", "g")

    assert summarizer.seen == [["a-1", "a-3", "a-4"]]  # not flagged a-2, killed a-5, running a-6
    (memory,) = db["memory"].docs
    assert memory["source_event_range"] == ["a-1", "a-3", "a-4"] and "embedding" in memory
    compactor.compact("s", "g")
    assert len(summarizer.seen) == 1  # nothing new, nothing compacted twice
