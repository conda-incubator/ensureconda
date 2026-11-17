import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Union

import pytest
import requests
from packaging.version import InvalidVersion, Version

from ensureconda import api, determine_versions, resolve
from ensureconda.installer import NotPublishedError
from ensureconda.resolve import UnsupportedPlatformError

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
    monkeypatch.setattr(determine_versions, "determine_mamba_version", broken_version)
    monkeypatch.setattr(
        determine_versions,
        "determine_micromamba_version",
        lambda executable: Version("2.0.0"),
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


NO_EXECUTABLE_BIT = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no executable bit"
)


@pytest.mark.parametrize(
    "version, mode, minimum",
    [
        (OSError(8, "Exec format error"), 0o755, Version("0.7.3")),
        (Version("0.1.0"), 0o755, Version("0.7.3")),
        pytest.param(
            Version("2.0.0"), 0o644, Version("0.7.3"), marks=NO_EXECUTABLE_BIT
        ),
        pytest.param(Version("2.0.0"), 0o644, None, marks=NO_EXECUTABLE_BIT),
    ],
    ids=["does-not-run", "too-old", "not-executable", "not-executable-unprobed"],
)
def test_install_replaces_a_new_executable_that_the_search_rejected(
    version: Union[Exception, Version],
    mode: int,
    minimum: Optional[Version],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = tmp_path / ("micromamba.exe" if resolve.is_windows else "micromamba")
    executable.write_bytes(b"micromamba")
    executable.chmod(mode)
    current = version
    downloads: List[str] = []

    def determine_version(exe: object) -> Version:
        if isinstance(current, Exception):
            raise current
        return current

    def download(url: str) -> requests.Response:
        downloads.append(url)
        response = requests.Response()
        response._content = b"tarball"
        return response

    def extract(tarball: object, filename: str, dest_filename: str) -> Path:
        nonlocal current
        executable.write_bytes(b"new micromamba")
        executable.chmod(0o755)
        current = Version("2.0.0")
        return executable

    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(resolve, "site_path", lambda: tmp_path)
    monkeypatch.setattr("ensureconda.installer.site_path", lambda: tmp_path)
    monkeypatch.setattr("ensureconda.installer.request_url_with_retry", download)
    monkeypatch.setattr("ensureconda.installer.extract_files_from_tar_bz2", extract)
    monkeypatch.setattr(
        determine_versions, "determine_micromamba_version", determine_version
    )

    result = api.ensureconda(
        mamba=False, conda=False, conda_exe=False, min_mamba_version=minimum
    )

    assert result == executable
    assert len(downloads) == 1
    skipped = 1 if isinstance(version, Exception) else 0
    # On Windows, the search spells the extension as in PATHEXT (".EXE").
    err = os.path.normcase(capsys.readouterr().err)
    assert err.count(os.path.normcase(f"Skipping {executable}")) == skipped


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
    def install_micromamba(constraint: object, **kwargs: object) -> Path:
        raise error

    monkeypatch.setattr(api, "install_micromamba", install_micromamba)


NOT_PUBLISHED = "micromamba is not published for linux-s390x"


@pytest.mark.usefixtures("nothing_installed")
def test_unpublished_micromamba_falls_back_to_conda_standalone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conda_standalone = Path("data", "conda_standalone")
    failing_micromamba_install(monkeypatch, NotPublishedError(NOT_PUBLISHED))
    monkeypatch.setattr(
        api, "install_conda_exe", lambda *args, **kwargs: conda_standalone
    )

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
    monkeypatch.setattr(
        api, "install_conda_exe", lambda *args, **kwargs: Path("unused")
    )

    with pytest.raises(requests.HTTPError):
        api.ensureconda()


@pytest.fixture
def unsupported_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd14")
    monkeypatch.setattr("ensureconda.installer.site_path", lambda: tmp_path)


@pytest.mark.usefixtures("nothing_installed", "unsupported_platform")
def test_unsupported_platform_still_finds_an_installed_conda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conda = Path("opt", "conda", "bin", "conda")
    monkeypatch.setattr(api, "conda_executables", lambda: iter([conda]))

    assert api.ensureconda() == conda


@pytest.mark.usefixtures("nothing_installed", "unsupported_platform")
def test_unsupported_platform_with_nothing_installed() -> None:
    with pytest.raises(
        UnsupportedPlatformError, match="Unsupported platform 'freebsd14'"
    ):
        api.ensureconda()


@pytest.mark.usefixtures("nothing_installed", "unsupported_platform")
def test_unsupported_platform_is_no_error_when_nothing_would_be_installed() -> None:
    assert api.ensureconda(micromamba=False, conda_exe=False) is None
