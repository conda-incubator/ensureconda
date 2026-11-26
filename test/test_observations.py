import errno
import os
import sys
from pathlib import Path
from typing import Callable

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from ensureconda import api, determine_versions
from ensureconda.determine_versions import (
    CondaExecutable,
    _Observations,
    _probe_version,
    attach_observation,
    check_executable,
    detach_observation,
)
from ensureconda.installer import DownloadValidation, new_executable
from ensureconda.resolve import is_windows

pytestmark = pytest.mark.skipif(is_windows, reason="the stand-ins are shell scripts")
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="relies on Linux")


@pytest.fixture
def runs(tmp_path: Path) -> Callable[[], int]:
    log = tmp_path / "runs.log"
    log.touch()
    return lambda: len(log.read_text().splitlines())


def stand_in(path: Path, body: str) -> Path:
    log = path.parent / "runs.log"
    path.write_text(f'#!/bin/sh\necho run >> "{log}"\n{body}\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def removed_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    work.rmdir()


def test_unchanged_file_runs_once_per_call(
    tmp_path: Path, runs: Callable[[], int]
) -> None:
    micromamba = stand_in(tmp_path / "micromamba", "echo 2.9.0")
    observations: _Observations = {}

    for _ in range(2):
        assert _probe_version(micromamba, "micromamba", observations) == (
            Version("2.9.0")
        )
    assert runs() == 1
    # The old helpers keep nothing.
    for _ in range(2):
        determine_versions.determine_micromamba_version(micromamba)
    assert runs() == 3


def test_changed_file_runs_again(tmp_path: Path, runs: Callable[[], int]) -> None:
    micromamba = stand_in(tmp_path / "micromamba", "echo 2.9.0")
    observations: _Observations = {}

    _probe_version(micromamba, "micromamba", observations)
    stand_in(micromamba, "echo 2.10.0")
    assert _probe_version(micromamba, "micromamba", observations) == Version("2.10.0")
    assert runs() == 2


def test_file_changed_while_probing_is_not_reused(
    tmp_path: Path, runs: Callable[[], int]
) -> None:
    micromamba = stand_in(tmp_path / "micromamba", 'echo "#" >> "$0"\necho 2.9.0')
    observations: _Observations = {}

    for _ in range(2):
        _probe_version(micromamba, "micromamba", observations)
    assert runs() == 2


def test_requirements_apply_to_one_observation(
    tmp_path: Path, runs: Callable[[], int]
) -> None:
    micromamba = stand_in(tmp_path / "micromamba", "echo 2.9.0")
    observations: _Observations = {}

    assert (
        check_executable(
            micromamba, "micromamba", SpecifierSet(">=3"), observations=observations
        )
        is None
    )
    assert check_executable(
        micromamba, "micromamba", SpecifierSet(">=1"), observations=observations
    ) == CondaExecutable(micromamba, "micromamba", Version("2.9.0"))
    assert runs() == 1


@pytest.mark.parametrize(
    "body, expected",
    [("exit 1", None), ("echo unrecognized", "unknown version")],
    ids=["failure", "unknown-version"],
)
def test_failure_and_unknown_version_stay_distinct(
    body: str, expected: object, tmp_path: Path, runs: Callable[[], int]
) -> None:
    micromamba = stand_in(tmp_path / "micromamba", body)
    observations: _Observations = {}

    for _ in range(2):
        result = check_executable(
            micromamba, "micromamba", None, observations=observations
        )
        if expected is None:
            assert result is None
        else:
            assert result == CondaExecutable(micromamba, "micromamba", None)
    assert runs() == 1


@linux_only
def test_failure_to_start_is_not_kept(tmp_path: Path, runs: Callable[[], int]) -> None:
    micromamba = stand_in(tmp_path / "micromamba", "echo 2.9.0")
    observations: _Observations = {}

    with micromamba.open("ab"):
        with pytest.raises(OSError) as busy:
            _probe_version(micromamba, "micromamba", observations)
    assert busy.value.errno == errno.ETXTBSY
    assert _probe_version(micromamba, "micromamba", observations) == Version("2.9.0")
    assert runs() == 1


def test_validation_is_transferred_across_a_rename(
    tmp_path: Path, runs: Callable[[], int]
) -> None:
    staged = stand_in(tmp_path / "staged", "echo 2.9.0")
    target = tmp_path / "micromamba"
    observations: _Observations = {}

    _probe_version(staged, "micromamba", observations)
    validated = detach_observation(observations, staged)
    os.replace(staged, target)
    attach_observation(observations, target, validated)
    assert _probe_version(target, "micromamba", observations) == Version("2.9.0")
    assert runs() == 1


def test_transfer_requires_the_renamed_file(
    tmp_path: Path, runs: Callable[[], int]
) -> None:
    staged = stand_in(tmp_path / "staged", "echo 2.9.0")
    target = stand_in(tmp_path / "micromamba", "echo 2.10.0")
    observations: _Observations = {}

    _probe_version(staged, "micromamba", observations)
    attach_observation(observations, target, detach_observation(observations, staged))
    assert _probe_version(target, "micromamba", observations) == Version("2.10.0")
    assert runs() == 2


def test_change_during_validation_is_not_transferred(tmp_path: Path) -> None:
    target = tmp_path / "micromamba"
    observations: _Observations = {}
    validation = DownloadValidation(
        "micromamba", SpecifierSet(">=1"), Version("2.9"), observations
    )

    with new_executable(target, validation=validation) as staged:
        # The change, to the permissions only, moves the change time alone.
        staged.write(b'#!/bin/sh\nchmod -x "$0"\necho 2.9.0\n')
    assert (
        check_executable(
            target, "micromamba", SpecifierSet(">=1"), observations=observations
        )
        is None
    )


def test_identify_runs_the_executable_once(
    tmp_path: Path, runs: Callable[[], int]
) -> None:
    executable = stand_in(tmp_path / "tool", "echo conda 25.1.1")

    assert api.identify(executable) == CondaExecutable(
        executable, "conda", Version("25.1.1")
    )
    assert runs() == 1


def test_observations_end_with_the_call(
    tmp_path: Path, runs: Callable[[], int], monkeypatch: pytest.MonkeyPatch
) -> None:
    micromamba = stand_in(tmp_path / "micromamba", "echo 2.9.0")
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([micromamba]))

    for _ in range(2):
        assert api.ensure(["micromamba>=3", "micromamba>=1"], install="never") == (
            CondaExecutable(micromamba, "micromamba", Version("2.9.0"))
        )
    assert runs() == 2


def test_bare_names_are_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, on_path = tmp_path / "work", tmp_path / "bin"
    work.mkdir()
    on_path.mkdir()
    stand_in(work / "conda", "echo conda 25.1.0")
    stand_in(on_path / "conda", "echo conda 4.14.0")
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", str(on_path))
    # Found in the working directory, but run from PATH.
    monkeypatch.setenv("CONDA_EXE", "conda")
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", str(work))

    assert api.ensure(["conda>=5"], install="never") == (
        CondaExecutable(work / "conda", "conda", Version("25.1.0"))
    )


@linux_only
@pytest.mark.usefixtures("removed_working_directory")
def test_probes_run_without_a_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in(tmp_path / "conda", "echo conda 25.1.0")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("CONDA_EXE", "../conda")
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", str(tmp_path / "data"))

    assert determine_versions.determine_conda_version("../conda") == Version("25.1.0")
    assert api.ensure(["conda>=5"], install="never") == (
        CondaExecutable(Path("../conda"), "conda", Version("25.1.0"))
    )


@linux_only
@pytest.mark.usefixtures("removed_working_directory")
def test_transfer_without_a_working_directory(tmp_path: Path) -> None:
    staged = stand_in(tmp_path / "staged", "echo 2.9.0")
    observations: _Observations = {}
    _probe_version(staged, "micromamba", observations)
    recorded = detach_observation(observations, staged)
    assert recorded is not None

    assert detach_observation(observations, Path("../staged")) is None
    attach_observation(observations, Path("../staged"), recorded)
    assert observations == {}


def test_nothing_is_reused_on_windows(
    tmp_path: Path, runs: Callable[[], int], monkeypatch: pytest.MonkeyPatch
) -> None:
    micromamba = stand_in(tmp_path / "micromamba", "echo 2.9.0")
    monkeypatch.setattr(os, "name", "nt")
    observations: _Observations = {}

    for _ in range(2):
        _probe_version(micromamba, "micromamba", observations)
    assert runs() == 2
