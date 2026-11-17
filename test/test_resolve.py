import platform
import sys
import sysconfig
from pathlib import Path
from test.test_info import executable_file
from typing import Optional

import pytest

from ensureconda import api, resolve
from ensureconda.resolve import (
    UnsupportedPlatformError,
    platform_subdir,
    subdir_for,
    windows_subdir,
)


@pytest.mark.parametrize("value", [None, ""])
def test_default_data_directory(
    value: Optional[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is None:
        monkeypatch.delenv("ENSURECONDA_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("ENSURECONDA_DATA_DIR", value)
    monkeypatch.setattr(
        "ensureconda.resolve.appdirs.user_data_dir", lambda app: str(tmp_path)
    )
    assert resolve.site_path() == tmp_path


@pytest.mark.parametrize("relative", [False, True])
def test_data_directory_override(
    relative: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = Path("custom data") if relative else tmp_path / "custom data"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", str(directory))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "ignored"))
    assert resolve.site_path() == directory
    assert not directory.exists()  # Discovery does not create the directory.


@pytest.mark.parametrize("location", ["data", "bin"])
def test_standalone_discovery_keeps_the_old_filename(
    location: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = ".exe" if sys.platform == "win32" else ""
    executable_file(tmp_path / location / f"conda-standalone{suffix}")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(resolve, "site_path", lambda: tmp_path / "data")

    assert list(resolve.conda_standalone_executables()) == []
    assert (
        api.ensureconda(mamba=False, micromamba=False, conda=False, no_install=True)
        is None
    )

    other_location = "bin" if location == "data" else "data"
    canonical = executable_file(tmp_path / other_location / f"conda_standalone{suffix}")
    assert list(resolve.conda_standalone_executables()) == [canonical]
    assert (
        api.ensureconda(mamba=False, micromamba=False, conda=False, no_install=True)
        == canonical
    )
    assert api.ensure(
        ["conda-standalone"], install="never", probe_version=False
    ) == api.CondaExecutable(canonical, "conda_standalone", None)


@pytest.mark.parametrize(
    "sys_platform, machine, bits, expected",
    [
        ("linux", "x86_64", 64, "linux-64"),
        ("linux", "i686", 32, "linux-32"),
        ("linux", "", 64, "linux-64"),
        ("linux", "aarch64", 64, "linux-aarch64"),
        ("linux", "ppc64", 64, "linux-ppc64"),
        ("linux", "ppc64le", 64, "linux-ppc64le"),
        ("linux", "s390x", 64, "linux-s390x"),
        ("linux", "riscv64", 64, "linux-riscv64"),
        ("linux", "armv6l", 32, "linux-armv6l"),
        ("linux", "armv7l", 32, "linux-armv7l"),
        ("linux", "armv8l", 32, "linux-armv7l"),
        ("darwin", "x86_64", 64, "osx-64"),
        ("darwin", "", 64, "osx-64"),
        ("darwin", "arm64", 64, "osx-arm64"),
        # Machine names are matched in any case.
        ("linux", "X86_64", 64, "linux-64"),
        ("linux", "AArch64", 64, "linux-aarch64"),
        ("zos", "", 64, "zos-z"),
    ],
)
def test_subdir_for(sys_platform: str, machine: str, bits: int, expected: str) -> None:
    assert subdir_for(sys_platform, machine, bits) == expected


@pytest.mark.parametrize(
    "sysconfig_platform, bits, expected",
    [
        ("win-amd64", 64, "win-64"),
        ("win-arm64", 64, "win-arm64"),
        ("win32", 32, "win-32"),
        # MSYS2's MinGW builds of Python, as named before and from Python 3.12
        ("mingw_x86_64", 64, "win-64"),
        ("mingw_i686", 32, "win-32"),
        ("mingw_aarch64", 64, "win-arm64"),
        ("mingw_x86_64_ucrt_gnu", 64, "win-64"),
        ("mingw_aarch64_ucrt_llvm", 64, "win-arm64"),
    ],
)
def test_windows_subdir(sysconfig_platform: str, bits: int, expected: str) -> None:
    assert windows_subdir(sysconfig_platform, bits) == expected


# 32-bit ARM builds of CPython, and of MSYS2's Python before and from 3.12
@pytest.mark.parametrize(
    "sysconfig_platform", ["win-arm32", "mingw_armv7", "mingw_armv7_ucrt_llvm"]
)
def test_windows_subdir_unsupported(sysconfig_platform: str) -> None:
    with pytest.raises(
        UnsupportedPlatformError,
        match=f"Unsupported platform 'win32' \\({sysconfig_platform}\\)",
    ):
        windows_subdir(sysconfig_platform, 32)


def test_platform_subdir_goes_by_the_python_build_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An x86-64 Python on Windows on ARM. From Python 3.12 on,
    # platform.machine() reports the hardware there.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sysconfig, "get_platform", lambda: "win-amd64")
    monkeypatch.setattr(platform, "machine", lambda: "ARM64")
    assert platform_subdir() == "win-64"


def test_platform_subdir_goes_by_the_machine_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert platform_subdir() == "linux-aarch64"


@pytest.mark.parametrize("sys_platform", ["freebsd14", "cygwin", "aix"])
def test_subdir_for_unsupported_platform(sys_platform: str) -> None:
    with pytest.raises(
        UnsupportedPlatformError, match=f"Unsupported platform '{sys_platform}'"
    ):
        subdir_for(sys_platform, "x86_64", 64)


@pytest.mark.parametrize(
    "sys_platform, machine",
    [("linux", "arm64"), ("darwin", "aarch64"), ("darwin", "s390x")],
)
def test_subdir_for_unsupported_combination(sys_platform: str, machine: str) -> None:
    with pytest.raises(UnsupportedPlatformError, match="Unsupported platform"):
        subdir_for(sys_platform, machine, 64)


@pytest.mark.parametrize("machine", ["mips64", "loongarch64", "sparc64"])
def test_subdir_for_unsupported_machine(machine: str) -> None:
    with pytest.raises(
        UnsupportedPlatformError,
        match=f"Unsupported platform 'linux' \\({machine}\\)",
    ):
        subdir_for("linux", machine, 64)


def test_unsupported_platform_error_names_the_machine_as_reported() -> None:
    with pytest.raises(UnsupportedPlatformError, match=r"\(Power Macintosh\)"):
        subdir_for("darwin", "Power Macintosh", 32)
