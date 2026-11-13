import pytest

from ensureconda.resolve import subdir_for


@pytest.mark.parametrize(
    "sys_platform, machine, bits, expected",
    [
        ("linux", "x86_64", 64, "linux-64"),
        ("linux", "i686", 32, "linux-32"),
        ("linux", "aarch64", 64, "linux-aarch64"),
        ("linux", "ppc64le", 64, "linux-ppc64le"),
        ("darwin", "x86_64", 64, "osx-64"),
        ("darwin", "arm64", 64, "osx-arm64"),
    ],
)
def test_subdir_for(sys_platform: str, machine: str, bits: int, expected: str) -> None:
    assert subdir_for(sys_platform, machine, bits) == expected
