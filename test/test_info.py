import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, cast

import pytest
from packaging.version import InvalidVersion, Version

from ensureconda import api
from ensureconda.api import CondaExecutable, Flavor, ensureconda, identify

MICROMAMBA = Path("data", "micromamba")


def executable_file(path: Path) -> Path:
    """Create an empty executable file at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def fake_version_outputs(
    monkeypatch: pytest.MonkeyPatch, outputs: Dict[str, str]
) -> List[str]:
    """Answer `<exe> --version` from outputs, keyed by file name; return the calls."""
    calls: List[str] = []

    def check_output(args: List[Any], **kwargs: Any) -> str:
        name = Path(args[0]).name
        calls.append(name)
        if name not in outputs:
            raise subprocess.CalledProcessError(1, args)
        return outputs[name]

    monkeypatch.setattr(subprocess, "check_output", check_output)
    return calls


@pytest.mark.parametrize("flavor", ["python", "conda-standalone"])
def test_probe_version_rejects_unknown_flavor_without_running_it(
    flavor: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_version_outputs(monkeypatch, {})

    with pytest.raises(ValueError, match="Unknown executable flavor"):
        api._probe_version(Path("tool"), cast(Flavor, flavor))
    assert calls == []


@pytest.fixture
def only_micromamba(monkeypatch: pytest.MonkeyPatch) -> None:
    for finder in ["mamba_executables", "conda_executables"]:
        monkeypatch.setattr(api, finder, lambda: iter([]))
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))


@pytest.mark.usefixtures("only_micromamba")
def test_ensureconda_still_runs_no_version_check_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0\n"})

    assert ensureconda(no_install=True) == MICROMAMBA
    assert calls == []


@pytest.mark.parametrize(
    "path, output, flavor, version",
    [
        ("bin/mamba", "mamba 1.5.12\nconda 24.11.3\n", "mamba", "1.5.12"),
        ("bin/mamba", "2.9.0\n", "mamba", "2.9.0"),
        ("bin/micromamba", "2.9.0\n", "micromamba", "2.9.0"),
        ("Scripts/conda.exe", "conda 26.5.2\n", "conda", "26.5.2"),
        ("condabin/conda.bat", "conda 26.5.2\n", "conda", "26.5.2"),
        ("data/conda_standalone", "conda 26.5.2\n", "conda_standalone", "26.5.2"),
        ("bin/conda-standalone", "conda 26.5.2\n", "conda_standalone", "26.5.2"),
        ("bin/conda-standalone.exe", "conda 26.5.2\n", "conda_standalone", "26.5.2"),
        # Unrecognized names fall back to the shape of the output.
        ("bin/mm", "2.9.0\n", "micromamba", "2.9.0"),
        ("bin/my-mamba", "mamba 1.5.12\nconda 24.11.3\n", "mamba", "1.5.12"),
        ("bin/my-conda", "conda 26.5.2\n", "conda", "26.5.2"),
    ],
)
def test_identify(
    path: str,
    output: str,
    flavor: Flavor,
    version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = executable_file(tmp_path / path)
    fake_version_outputs(monkeypatch, {executable.name: output})

    assert identify(executable) == CondaExecutable(executable, flavor, Version(version))


def test_identify_rejects_unrecognized_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = executable_file(tmp_path / "bin" / "python")
    fake_version_outputs(monkeypatch, {"python": "Python 3.13.2\n"})

    with pytest.raises(ValueError, match="not a recognized conda executable"):
        identify(python)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["micromamba", "--version"]),
        OSError(8, "Exec format error"),
        InvalidVersion("Invalid version: 'garbage'"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["nonzero-exit", "exec-error", "invalid-version", "undecodable-output"],
)
def test_identify_still_reports_unknown_version_after_probe_failure(
    error: Exception,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken_version(executable: object) -> Version:
        raise error

    executable = executable_file(tmp_path / "micromamba")
    monkeypatch.setattr(api, "determine_micromamba_version", broken_version)

    assert identify(executable) == CondaExecutable(executable, "micromamba", None)
    assert capsys.readouterr().err == ""


def test_identify_rejects_missing_and_unexecutable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_version_outputs(monkeypatch, {"conda": "conda 26.5.2\n"})

    with pytest.raises(FileNotFoundError):
        identify(tmp_path / "missing" / "conda")
    if sys.platform != "win32":  # Windows has no execute permission.
        conda = executable_file(tmp_path / "bin" / "conda")
        conda.chmod(0o644)
        with pytest.raises(PermissionError):
            identify(conda)
    assert calls == []
