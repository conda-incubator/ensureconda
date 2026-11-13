from pathlib import Path
from typing import Any, Optional

import pytest
import requests
from click.testing import CliRunner

from ensureconda import cli
from ensureconda.cli import ensureconda_cli
from ensureconda.installer import NotPublishedError
from ensureconda.resolve import UnsupportedPlatformError


@pytest.mark.parametrize("option", ["--min-conda-version", "--min-mamba-version"])
def test_invalid_version_is_a_usage_error(option: str) -> None:
    result = CliRunner().invoke(ensureconda_cli, [option, "4.8,2", "--no-install"])

    assert result.exit_code == 2
    assert "'4.8,2' is not a valid version" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    "error, expected_output",
    [
        (
            requests.ConnectionError("Connection refused"),
            "Error: download failed: Connection refused\n",
        ),
        (
            NotPublishedError("No conda-standalone package found for linux-s390x"),
            "Error: No conda-standalone package found for linux-s390x\n",
        ),
        (
            UnsupportedPlatformError("Unsupported platform 'linux' (mips64)"),
            "Error: Unsupported platform 'linux' (mips64)\n",
        ),
    ],
    ids=["download-failure", "not-published", "unsupported-platform"],
)
def test_failed_installation_ends_with_one_line(
    error: Exception, expected_output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ensureconda(**kwargs: Any) -> Optional[Path]:
        if kwargs["no_install"]:
            return None
        raise error

    monkeypatch.setattr(cli, "ensureconda", ensureconda)

    result = CliRunner().invoke(ensureconda_cli)

    assert result.exit_code == 1
    assert result.output == expected_output
    # An exception that the CLI doesn't handle would be reported here instead.
    assert isinstance(result.exception, SystemExit)
