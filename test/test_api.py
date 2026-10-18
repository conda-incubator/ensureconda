import subprocess
from pathlib import Path

import pytest
import requests
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


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Error", response=response)


@pytest.fixture
def nothing_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    for finder in [
        "mamba_executables",
        "micromamba_executables",
        "conda_executables",
        "conda_standalone_executables",
    ]:
        monkeypatch.setattr(api, finder, lambda: iter([]))


@pytest.mark.usefixtures("nothing_installed")
def test_missing_micromamba_build_falls_back_to_conda_standalone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def install_micromamba() -> Path:
        raise http_error(404)

    conda_standalone = Path("data", "conda_standalone")
    monkeypatch.setattr(api, "install_micromamba", install_micromamba)
    monkeypatch.setattr(api, "install_conda_exe", lambda: conda_standalone)

    assert api.ensureconda(no_install=False) == conda_standalone
    assert "micromamba is not available" in capsys.readouterr().err


@pytest.mark.usefixtures("nothing_installed")
@pytest.mark.parametrize(
    "status, conda_exe",
    [(404, False), (500, True), (403, True)],
    ids=["404-without-conda-exe", "500", "403"],
)
def test_other_micromamba_failures_propagate(
    status: int, conda_exe: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def install_micromamba() -> Path:
        raise http_error(status)

    monkeypatch.setattr(api, "install_micromamba", install_micromamba)
    monkeypatch.setattr(api, "install_conda_exe", lambda: Path("unused"))

    with pytest.raises(requests.HTTPError):
        api.ensureconda(no_install=False, conda_exe=conda_exe)
