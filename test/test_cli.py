import errno
from pathlib import Path
from typing import Any, List, Optional

import pytest
import requests
from click.testing import CliRunner

from ensureconda import cli
from ensureconda.cli import ensureconda_cli
from ensureconda.installer import (
    DownloadedExecutableError,
    ExecutableReplacementError,
    NotPublishedError,
)
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
            "Error: download failed: ConnectionError: Connection refused\n",
        ),
        (
            requests.ReadTimeout(),
            "Error: download failed: ReadTimeout\n",
        ),
        (
            NotPublishedError("No conda-standalone package found for linux-s390x"),
            "Error: No conda-standalone package found for linux-s390x\n",
        ),
        (
            UnsupportedPlatformError("Unsupported platform 'freebsd14' (amd64)"),
            "Error: Unsupported platform 'freebsd14' (amd64)\n",
        ),
        (
            ExecutableReplacementError("Could not replace executable: access denied"),
            "Error: Could not replace executable: access denied\n",
        ),
        (
            ExecutableReplacementError(
                errno.EACCES,
                "Could not replace executable /cache/micromamba: Permission denied",
                "/cache/temporary",
                None,
                "/cache/micromamba",
            ),
            "Error: Could not replace executable /cache/micromamba: Permission denied\n",
        ),
        (
            DownloadedExecutableError(
                "micromamba: downloaded executable reports 1.5, expected 2.0"
                " satisfying >=2"
            ),
            "Error: micromamba: downloaded executable reports 1.5, expected 2.0"
            " satisfying >=2\n",
        ),
    ],
    ids=[
        "download-failure",
        "empty-timeout",
        "not-published",
        "unsupported-platform",
        "replacement-failure",
        "replacement-failure-with-metadata",
        "invalid-download",
    ],
)
@pytest.mark.parametrize(
    "options", [[], ["--json"], ["--force-install"], ["--force-install", "--json"]]
)
def test_failed_installation_ends_with_one_line(
    error: Exception,
    expected_output: str,
    options: List[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ensureconda(*args: Any, **kwargs: Any) -> Optional[Path]:
        if kwargs.get("no_install"):
            return None
        raise error

    monkeypatch.setattr(cli, "ensureconda", ensureconda)
    monkeypatch.setattr(cli, "_ensure", ensureconda)

    result = CliRunner().invoke(ensureconda_cli, options)

    assert result.exit_code == 1
    assert result.output == expected_output
    # An exception that the CLI doesn't handle would be reported here instead.
    assert isinstance(result.exception, SystemExit)
