import platform
import subprocess
import sys
from pathlib import Path

import pytest
import requests
from packaging.version import InvalidVersion, Version

from ensureconda import api, resolve
from ensureconda.installer import NotPublishedError

BROKEN_MAMBA = Path("broken", "mamba")
WORKING_MICROMAMBA = Path("working", "micromamba")
INSTALLED_CONDA = Path("opt", "conda", "bin", "conda")


def on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *installed_conda: Path
) -> None:
    # No conda subdir exists for mips64.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "mips64")
    monkeypatch.setattr("ensureconda.installer.site_path", lambda: tmp_path)
    for finder in [
        "mamba_executables",
        "micromamba_executables",
        "conda_standalone_executables",
    ]:
        monkeypatch.setattr(api, finder, lambda: iter([]))
    monkeypatch.setattr(api, "conda_executables", lambda: iter(installed_conda))


def test_unsupported_platform_still_finds_an_installed_conda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_unsupported_platform(monkeypatch, tmp_path, INSTALLED_CONDA)

    assert api.ensureconda() == INSTALLED_CONDA


def test_unsupported_platform_with_nothing_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_unsupported_platform(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match=r"Unsupported platform 'linux' \(mips64\)"):
        api.ensureconda()


def test_unsupported_platform_is_no_error_when_nothing_would_be_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_unsupported_platform(monkeypatch, tmp_path)

    assert api.ensureconda(micromamba=False, conda_exe=False) is None


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


def test_search_without_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset PATH means there is nothing to find there, not an error."""
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("CONDA_EXE", raising=False)
    monkeypatch.setattr(resolve, "site_path", lambda: tmp_path)

    assert api.ensureconda(no_install=True) is None


@pytest.fixture
def nothing_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    for finder in [
        "mamba_executables",
        "micromamba_executables",
        "conda_executables",
        "conda_standalone_executables",
    ]:
        monkeypatch.setattr(api, finder, lambda: iter([]))


def failing_micromamba_install(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def install_micromamba() -> Path:
        raise error

    monkeypatch.setattr(api, "install_micromamba", install_micromamba)


NOT_PUBLISHED = "micromamba is not published for linux-s390x"


@pytest.mark.usefixtures("nothing_installed")
def test_unpublished_micromamba_falls_back_to_conda_standalone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conda_standalone = Path("data", "conda_standalone")
    failing_micromamba_install(monkeypatch, NotPublishedError(NOT_PUBLISHED))
    monkeypatch.setattr(api, "install_conda_exe", lambda: conda_standalone)

    assert api.ensureconda() == conda_standalone
    assert capsys.readouterr().err == (
        f"{NOT_PUBLISHED}; trying conda or conda-standalone instead\n"
    )


@pytest.mark.usefixtures("nothing_installed")
def test_unpublished_micromamba_falls_back_to_conda(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conda = Path("opt", "conda", "bin", "conda")
    failing_micromamba_install(monkeypatch, NotPublishedError(NOT_PUBLISHED))
    monkeypatch.setattr(api, "conda_executables", lambda: iter([conda]))

    assert api.ensureconda(conda_exe=False) == conda
    assert capsys.readouterr().err == f"{NOT_PUBLISHED}; trying conda instead\n"


@pytest.mark.usefixtures("nothing_installed")
def test_unpublished_micromamba_with_nothing_left_to_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_micromamba_install(monkeypatch, NotPublishedError(NOT_PUBLISHED))

    with pytest.raises(NotPublishedError, match=NOT_PUBLISHED):
        api.ensureconda(conda=False, conda_exe=False)


@pytest.mark.usefixtures("nothing_installed")
@pytest.mark.parametrize(
    "status",
    [
        # As for a release that hasn't been uploaded for this platform yet.
        404,
        500,
    ],
)
def test_failed_micromamba_download_does_not_fall_back(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = requests.Response()
    response.status_code = status
    failing_micromamba_install(
        monkeypatch, requests.HTTPError(f"{status} Error", response=response)
    )
    monkeypatch.setattr(api, "install_conda_exe", lambda: Path("unused"))

    with pytest.raises(requests.HTTPError):
        api.ensureconda()
