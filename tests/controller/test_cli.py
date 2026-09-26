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


def test_resume_refuses_without_real_ports(capsys):
    import pytest

    with pytest.raises(SystemExit, match="only `run --fake` works"):
        main(["resume", "x", "--config", "configs/h.toml"])
