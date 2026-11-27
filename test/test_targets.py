import doctest
from typing import Optional

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from ensureconda import api
from ensureconda.api import Flavor, _parse_target
from ensureconda.determine_versions import version_satisfies


def test_target_doctests() -> None:
    doctest.testmod(api, raise_on_error=True)


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
        ("micromamba>=1", "2.0rc1", False),
        ("micromamba>=1,<3", "2.0.dev1", False),
        ("micromamba>=2.0rc1,<3", "2.0rc1", True),
        ("micromamba<2.0rc2", "2.0rc1", True),
        ("micromamba!=1.0rc1", "2.0rc1", False),
        ("micromamba==2.*", "2.0rc1", False),
        ("micromamba===custom", "2.0rc1", False),
        ("micromamba>=1", "2.0.post1", True),
        ("micromamba>=1.0", "1.0+local", True),
        ("micromamba==1.0+local", "1.0+local", True),
        ("conda (>=24, <27)", "25.1", True),
    ],
)
def test_target_version_constraints(target: str, version: str, expected: bool) -> None:
    _, constraint = _parse_target(target)
    assert version_satisfies(Version(version), constraint) is expected


@pytest.mark.parametrize("flavor", ["mamba", "micromamba", "conda", "conda_standalone"])
def test_unconstrained_target(flavor: str) -> None:
    assert _parse_target(flavor) == (flavor, SpecifierSet())


@pytest.mark.parametrize(
    "version", [None, Version("2"), Version("2rc1"), Version("2.dev1")]
)
def test_unconstrained_executable_accepts_any_version(
    version: Optional[Version],
) -> None:
    assert version_satisfies(version, SpecifierSet())
    assert version_satisfies(version, None)


@pytest.mark.parametrize(
    "target",
    [
        "",
        "micro mamba",
        "micromamba>=invalid",
        "micromamba<invalid",
        "conda<=",
        "conda>=24.*",
        "conda[extra]",
        "conda>=1; python_version >= '3'",
        "conda @ https://example.com/conda",
        "micromamba>=1.0+local,<3",
        "micromamba>=1.0+local",
        "micromamba<3>=1.0+local",
    ],
)
def test_invalid_constraint_names_the_target(target: str) -> None:
    with pytest.raises(ValueError, match="Invalid target") as error:
        _parse_target(target)
    assert target in str(error.value)


@pytest.mark.parametrize(
    "target, flavor",
    [
        ("MAMBA", "mamba"),
        ("MicroMamba", "micromamba"),
        ("CONDA", "conda"),
        ("conda[]", "conda"),
        ("conda-standalone", "conda_standalone"),
        ("conda_standalone", "conda_standalone"),
        ("CONDA_STANDALONE", "conda_standalone"),
        ("Conda.Standalone", "conda_standalone"),
        ("conda._---__standalone", "conda_standalone"),
    ],
)
def test_target_name_canonicalization(target: str, flavor: Flavor) -> None:
    parsed_flavor, constraint = _parse_target(f"{target}>=2.9.1,<3")

    assert parsed_flavor == flavor
    assert constraint is not None
    assert constraint.contains(Version("2.9.1"))
    assert not constraint.contains(Version("2.9.0"))
    assert not constraint.contains(Version("3"))


@pytest.mark.parametrize(
    "target", ["conda-unknown", "conda.standalone.extra", "python3"]
)
def test_unknown_target_names_are_not_version_errors(target: str) -> None:
    with pytest.raises(ValueError, match="Unknown target") as error:
        _parse_target(target)
    assert target in str(error.value)
