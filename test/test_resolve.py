import pytest

from ensureconda.resolve import subdir_for


@pytest.mark.parametrize(
    "sys_platform, machine, bits, expected",
    [
        ("linux", "x86_64", 64, "linux-64"),
        ("linux", "i686", 32, "linux-32"),
        ("linux", "aarch64", 64, "linux-aarch64"),
        ("linux", "ppc64le", 64, "linux-ppc64le"),
        ("linux", "s390x", 64, "linux-s390x"),
        ("linux", "riscv64", 64, "linux-riscv64"),
        ("linux", "armv7l", 32, "linux-armv7l"),
        ("darwin", "x86_64", 64, "osx-64"),
        ("darwin", "arm64", 64, "osx-arm64"),
        ("win32", "AMD64", 64, "win-64"),
        ("win32", "x86", 32, "win-32"),
        # Windows on ARM keeps using x64 builds; see subdir_for.
        ("win32", "ARM64", 64, "win-64"),
        ("zos", "", 64, "zos-z"),
    ],
)
def test_subdir_for(sys_platform: str, machine: str, bits: int, expected: str) -> None:
    assert subdir_for(sys_platform, machine, bits) == expected


@pytest.mark.parametrize("sys_platform", ["freebsd14", "cygwin", "aix"])
def test_subdir_for_unsupported_platform(sys_platform: str) -> None:
    with pytest.raises(RuntimeError, match=f"Unsupported platform '{sys_platform}'"):
        subdir_for(sys_platform, "x86_64", 64)
