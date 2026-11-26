import inspect
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
from click.testing import CliRunner
from packaging.version import Version

from ensureconda import api, cli, determine_versions


@pytest.mark.parametrize("verbosity", [0, 1, 2])
def test_cli_logs_to_stderr_and_restores_application_logger(
    verbosity: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api, "micromamba_executables", lambda: iter([Path("old"), Path("new")])
    )
    monkeypatch.setattr(
        determine_versions,
        "_probe_version",
        lambda path, flavor, observations: Version("1" if path.name == "old" else "2"),
    )
    logger = logging.getLogger("ensureconda")
    previous = logger.level, logger.handlers[:], logger.propagate
    root = logging.getLogger()
    previous_root = root.level, root.handlers[:]
    runner_options: Dict[str, Any] = {}
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        runner_options["mix_stderr"] = False
    result = CliRunner(**runner_options).invoke(
        cli.ensureconda_cli,
        [
            "--json",
            "--no-mamba",
            "--no-conda",
            "--no-conda-exe",
            "--no-install",
            "--min-mamba-version",
            "2",
            "--verbosity",
            str(verbosity),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "path": "new",
        "flavor": "micromamba",
        "version": "2",
    }
    assert ("Found compatible executable" in result.stderr) == (verbosity >= 1)
    assert ("Rejected old 1: requires >=2" in result.stderr) == (verbosity >= 1)
    assert ("Selected micromamba 2 at new" in result.stderr) == (verbosity >= 1)
    assert ("Searching for micromamba" in result.stderr) == (verbosity >= 2)
    assert (logger.level, logger.handlers, logger.propagate) == previous
    assert (root.level, root.handlers) == previous_root


def test_cli_keeps_application_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    class Recorder(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: List[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    logger = logging.getLogger("ensureconda")
    before, during = Recorder(), Recorder()

    def find() -> Iterator[Path]:
        logger.addHandler(during)
        return iter([Path("micromamba")])

    monkeypatch.setattr(api, "micromamba_executables", find)
    monkeypatch.setattr(
        determine_versions,
        "_probe_version",
        lambda path, flavor, observations: Version("2"),
    )
    logger.addHandler(before)
    try:
        result = CliRunner().invoke(cli.ensureconda_cli, ["--no-mamba", "--no-conda"])
        assert result.exit_code == 0, result.output
        assert logger.handlers == [before, during]
        for handler in before, during:
            assert "Found compatible executable" in handler.messages
    finally:
        logger.removeHandler(before)
        logger.removeHandler(during)


def test_library_decisions_are_available_to_application_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([Path("old")]))
    monkeypatch.setattr(
        determine_versions,
        "_probe_version",
        lambda path, flavor, observations: Version("1"),
    )
    with caplog.at_level(logging.INFO, logger="ensureconda"):
        assert api.ensure(["micromamba>=2"], install="never") is None
    assert caplog.messages == ["Rejected old 1: requires >=2"]
    assert capsys.readouterr().out == ""


def test_cli_reports_no_failure_before_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for finder in [
        "mamba_executables",
        "micromamba_executables",
        "conda_executables",
        "conda_standalone_executables",
    ]:
        monkeypatch.setattr(api, finder, lambda: iter([]))
    monkeypatch.setattr(
        api, "install_micromamba", lambda *args, **kwargs: Path("micromamba")
    )
    monkeypatch.setattr(
        determine_versions,
        "_probe_version",
        lambda path, flavor, observations: Version("2"),
    )
    runner_options: Dict[str, Any] = {}
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        runner_options["mix_stderr"] = False

    result = CliRunner(**runner_options).invoke(cli.ensureconda_cli, [])

    assert result.exit_code == 0, result.output
    assert result.stdout == "micromamba\n"
    assert result.stderr == (
        "Selected micromamba 2 at micromamba\nFound compatible executable\n"
    )


@pytest.mark.parametrize("probe_version", [True, False])
def test_selection_without_a_known_version(
    probe_version: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        api, "micromamba_executables", lambda: iter([Path("micromamba")])
    )
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda path, flavor, observations: None
    )
    with caplog.at_level(logging.INFO, logger="ensureconda"):
        result = api.ensure(
            ["micromamba"], install="never", probe_version=probe_version
        )
    assert result == api.CondaExecutable(Path("micromamba"), "micromamba", None)
    assert caplog.messages == ["Selected micromamba at micromamba"]
