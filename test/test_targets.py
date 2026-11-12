import pytest
from packaging.version import Version

from ensureconda.api import _parse_target


@pytest.mark.parametrize(
    "target, version, expected",
    [
        ("micromamba>=2", "1.9", False),
        ("micromamba>=2", "2.0", True),
        ("conda>=24,<27", "27", False),
        ("conda>=24,<27", "25.1", True),
        ("micromamba==2.9.*", "2.9.1", True),
        ("micromamba==2.9.*", "2.10", False),
        ("micromamba>=2,!=2.5", "2.5", False),
        ("micromamba>=1", "2.0rc1", True),
        ("micromamba>=1.0+local", "1.0", False),
        ("micromamba>=1.0+local", "1.0+local", True),
    ],
)
def test_target_version_constraints(target: str, version: str, expected: bool) -> None:
    _, accepts_version = _parse_target(target)
    assert accepts_version is not None
    assert accepts_version(Version(version)) is expected


@pytest.mark.parametrize("flavor", ["mamba", "micromamba", "conda", "conda_standalone"])
def test_unconstrained_target(flavor: str) -> None:
    assert _parse_target(flavor) == (flavor, None)


@pytest.mark.parametrize("target", ["micromamba>=invalid", "conda[extra]"])
def test_invalid_constraint_names_the_target(target: str) -> None:
    with pytest.raises(ValueError, match="Invalid version constraint") as error:
        _parse_target(target)
    assert target in str(error.value)
