import platform
import sys
import sysconfig

import pytest

from ensureconda.resolve import (
    UnsupportedPlatformError,
    platform_subdir,
    subdir_for,
    windows_subdir,
)


@pytest.mark.parametrize(
    "sys_platform, machine, bits, expected",
    [
        ("linux", "x86_64", 64, "linux-64"),
        ("linux", "i686", 32, "linux-32"),
        ("linux", "", 64, "linux-64"),
        ("linux", "aarch64", 64, "linux-aarch64"),
        ("linux", "ppc64le", 64, "linux-ppc64le"),
        ("linux", "s390x", 64, "linux-s390x"),
        ("linux", "riscv64", 64, "linux-riscv64"),
        ("linux", "armv7l", 32, "linux-armv7l"),
        ("darwin", "x86_64", 64, "osx-64"),
        ("darwin", "", 64, "osx-64"),
        ("darwin", "arm64", 64, "osx-arm64"),
        # Machine names are matched in any case.
        ("linux", "X86_64", 64, "linux-64"),
        ("linux", "AArch64", 64, "linux-aarch64"),
        ("zos", "", 64, "zos-z"),
        ("zos", "s390x", 64, "zos-z"),
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
        # MinGW builds of Python
        ("mingw_x86_64", 64, "win-64"),
        ("mingw_i686", 32, "win-32"),
    ],
)
def test_windows_subdir(sysconfig_platform: str, bits: int, expected: str) -> None:
    assert windows_subdir(sysconfig_platform, bits) == expected


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
    monkeypatch.setattr(platform, "machine", lambda: "s390x")
    assert platform_subdir() == "linux-s390x"


@pytest.mark.parametrize("sys_platform", ["freebsd14", "cygwin", "aix"])
def test_subdir_for_unsupported_platform(sys_platform: str) -> None:
    with pytest.raises(
        UnsupportedPlatformError, match=f"Unsupported platform '{sys_platform}'"
    ):
        subdir_for(sys_platform, "x86_64", 64)


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
