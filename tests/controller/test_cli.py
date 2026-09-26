from tokeneyezed.controller.cli import main


def test_fake_run_completes(capsys):
    code = main(
        [
            "run",
            "--config",
            "configs/h.toml",
            "--fake",
            "--checkpointer",
            "memory",
            "--session-id",
            "cli-test",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "STARTED cli-test" in out and "cli-test ALL GOALS COMPLETE" in out


def test_resume_builds_real_ports_and_names_missing_setup(monkeypatch):
    import pytest

    monkeypatch.delenv("TOKENEYEZED_WORKSPACE", raising=False)
    with pytest.raises(SystemExit, match="TOKENEYEZED_WORKSPACE"):
        main(["resume", "x", "--config", "configs/h.toml"])
