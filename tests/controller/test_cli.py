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
    assert "cli-test: all goals complete" in capsys.readouterr().out


def test_resume_refuses_without_real_ports(capsys):
    import pytest

    with pytest.raises(SystemExit, match="only `run --fake` works"):
        main(["resume", "x", "--config", "configs/h.toml"])
