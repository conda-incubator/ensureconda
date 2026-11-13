"""Exercise version-probe failures with real files, discovery, and subprocesses."""

import sys
from pathlib import Path
from test.test_info import executable_file

import pytest
from packaging.version import Version

from ensureconda import CondaExecutable, ensure, installer, resolve

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="The executable fixtures use POSIX shell scripts"
)


@pytest.fixture
def isolated_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    monkeypatch.setenv("PATH", str(bin_path))
    monkeypatch.delenv("CONDA_EXE", raising=False)
    monkeypatch.setattr(resolve, "site_path", lambda: data)
    monkeypatch.setattr(installer, "site_path", lambda: data)

    def unexpected_download(*args: object, **kwargs: object) -> None:
        raise AssertionError("These fixtures must not download anything")

    monkeypatch.setattr(installer, "request_url_with_retry", unexpected_download)
    return tmp_path


def program(path: Path, source: str) -> Path:
    executable_file(path).write_text(source)
    return path


def test_broken_conda_allows_a_later_installed_target(
    isolated_tools: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conda = program(isolated_tools / "bin" / "conda", "#!/bin/sh\nexit 1\n")
    micromamba = program(
        isolated_tools / "bin" / "micromamba", "#!/bin/sh\nprintf '2.9.0\\n'\n"
    )

    assert ensure(["conda", "micromamba"]) == CondaExecutable(
        micromamba, "micromamba", Version("2.9.0")
    )
    assert f"Skipping {conda}" in capsys.readouterr().err


def test_unrunnable_cached_micromamba_allows_path_candidate(
    isolated_tools: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = executable_file(isolated_tools / "data" / "micromamba")
    working = program(
        isolated_tools / "bin" / "micromamba", "#!/bin/sh\nprintf '2.9.0\\n'\n"
    )

    assert ensure(["micromamba"]) == CondaExecutable(
        working, "micromamba", Version("2.9.0")
    )
    assert f"Skipping {broken}" in capsys.readouterr().err


def test_broken_conda_exe_allows_path_candidate(
    isolated_tools: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = program(isolated_tools / "environment" / "conda", "#!/bin/sh\nexit 1\n")
    working = program(
        isolated_tools / "bin" / "conda", "#!/bin/sh\nprintf 'conda 25.1.1\\n'\n"
    )
    monkeypatch.setenv("CONDA_EXE", str(broken))

    assert ensure(["conda"]) == CondaExecutable(working, "conda", Version("25.1.1"))
    assert f"Skipping {broken}" in capsys.readouterr().err


def test_fresh_unrunnable_cache_is_rejected_after_installation(
    isolated_tools: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = executable_file(isolated_tools / "data" / "micromamba")

    # The installer reuses this fresh file. Probing rejects it but does not
    # change the installer's cache policy or trigger a replacement download.
    assert ensure(["micromamba"]) is None
    assert (isolated_tools / "data" / "micromamba_install.lock").exists()
    assert broken.read_bytes() == b""
    assert f"Skipping {broken}" in capsys.readouterr().err
