import pytest

from tokeneyezed.controller.cli import main
from tokeneyezed.observer import commands


def test_rules_learn_cli_replays_and_persists_candidates(monkeypatch, capsys):
    events = [
        {
            "phase": "pre",
            "tool": "bash",
            "input": "git reset --hard",
            "verdict": "block: destructive command",
        },
        {
            "phase": "pre",
            "tool": "bash",
            "input": "git reset --hard HEAD",
            "verdict": "block: destructive command",
        },
        {"phase": "pre", "tool": "bash", "input": "git status", "verdict": "allow"},
    ]
    saved = []
    monkeypatch.setattr(commands, "get_db", lambda: object())
    monkeypatch.setattr(commands, "load_learning_events", lambda **_kwargs: events)
    monkeypatch.setattr(commands, "load_rules", lambda **_kwargs: [])
    monkeypatch.setattr(
        commands, "store_learned_rules", lambda rules, **_kwargs: saved.extend(rules)
    )

    assert main(["rules", "learn"]) == 0

    assert any(
        rule["pattern"] == "git reset --hard" and rule["status"] == "active" for rule in saved
    )
    assert "active" in capsys.readouterr().out


def test_rules_learn_cli_does_not_change_rules_without_labels(monkeypatch, capsys):
    writes = []
    monkeypatch.setattr(commands, "get_db", lambda: object())
    monkeypatch.setattr(commands, "load_learning_events", lambda **_kwargs: [])
    monkeypatch.setattr(
        commands,
        "store_learned_rules",
        lambda *_args, **_kwargs: writes.append(True),
    )

    assert main(["rules", "learn"]) == 0

    assert not writes
    assert "no labeled pre-tool events" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["1", "0", "not-a-number"])
def test_rules_learn_cli_rejects_unsafe_support_thresholds(value, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["rules", "learn", "--min-support", value])

    assert exc_info.value.code == 2
    assert "at least 2" in capsys.readouterr().err
