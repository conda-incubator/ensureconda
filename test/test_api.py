import subprocess
from pathlib import Path

import pytest
from packaging.version import InvalidVersion, Version

from ensureconda import api

BROKEN_MAMBA = Path("broken", "mamba")
WORKING_MICROMAMBA = Path("working", "micromamba")


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, [str(BROKEN_MAMBA), "--version"]),
        OSError(8, "Exec format error"),
        InvalidVersion("Invalid version: 'garbage'"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["nonzero-exit", "exec-error", "invalid-version", "undecodable-output"],
)
def test_skip_candidate_whose_version_check_fails(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken_version(executable: object) -> Version:
        raise error

    monkeypatch.setattr(api, "mamba_executables", lambda: iter([BROKEN_MAMBA]))
    monkeypatch.setattr(
        api, "micromamba_executables", lambda: iter([WORKING_MICROMAMBA])
    )
    monkeypatch.setattr(api, "determine_mamba_version", broken_version)
    monkeypatch.setattr(
        api, "determine_micromamba_version", lambda executable: Version("2.0.0")
    )

    executable = api.ensureconda(
        conda=False,
        conda_exe=False,
        no_install=True,
        min_mamba_version=Version("0.7.3"),
    )

    assert executable == WORKING_MICROMAMBA
    assert f"Skipping {BROKEN_MAMBA}" in capsys.readouterr().err
