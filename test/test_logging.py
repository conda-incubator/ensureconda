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
        lambda path, flavor: Version("1" if path.name == "old" else "2"),
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
        determine_versions, "_probe_version", lambda path, flavor: Version("2")
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
