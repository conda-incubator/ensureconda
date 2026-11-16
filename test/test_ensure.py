import inspect
from pathlib import Path
from test.test_info import fake_version_outputs
from typing import List

import pytest
from packaging.version import Version

from ensureconda import CondaExecutable, api, ensure
from ensureconda.resolve import UnsupportedPlatformError

MICROMAMBA = Path("data", "micromamba")
CONDA = Path("opt", "bin", "conda")
STANDALONE = Path("data", "conda_standalone")


@pytest.fixture(autouse=True)
def no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    for finder in [
        "mamba_executables",
        "micromamba_executables",
        "conda_executables",
        "conda_standalone_executables",
    ]:
        monkeypatch.setattr(api, finder, lambda: iter([]))

    def unexpected_install() -> Path:
        raise AssertionError("An installation was not expected")

    monkeypatch.setattr(api, "install_micromamba", unexpected_install)
    monkeypatch.setattr(api, "install_conda_exe", unexpected_install)


@pytest.mark.parametrize(
    "targets, expected",
    [
        (["micromamba", "conda"], MICROMAMBA),
        (["conda", "micromamba"], CONDA),
    ],
)
def test_installed_target_order(
    targets: List[str], expected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    calls = fake_version_outputs(
        monkeypatch, {"micromamba": "2.9.0", "conda": "conda 25.1.1"}
    )

    result = ensure(targets)

    assert result is not None and result.path == expected
    assert calls == [expected.name]


def test_installed_conda_precedes_installing_micromamba(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    calls = fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    assert ensure(["micromamba", "conda"]) == CondaExecutable(
        CONDA, "conda", Version("25.1.1")
    )
    assert calls == ["conda"]


def test_per_target_installs_before_searching_for_a_later_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(["micromamba", "conda"], install="per-target") == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0")
    )
    assert calls == ["micromamba"]


def test_fallback_is_the_default_install_value() -> None:
    assert inspect.signature(ensure).parameters["install"].default == "fallback"


@pytest.mark.parametrize("install", ["force", "Never", "", True, False, None])
def test_unknown_install_values_are_rejected_before_searching(
    install: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9"})

    with pytest.raises(ValueError, match="Unknown install value"):
        ensure(["micromamba"], install=install)  # type: ignore[arg-type]
    assert calls == []


def test_never_searches_later_targets_without_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    result = ensure(["micromamba", "conda"], install="never")

    assert result is not None and result.path == CONDA


@pytest.mark.parametrize("targets", [[], ["conda"], ["mamba"]])
def test_only_selected_targets_are_considered(
    targets: List[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(targets) is None
    assert calls == []


def test_never_installs_nothing() -> None:
    assert ensure(["micromamba", "conda_standalone"], install="never") is None


@pytest.mark.parametrize("installed", [False, True])
def test_canonicalized_target_finds_or_installs_the_same_flavor(
    installed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if installed:
        monkeypatch.setattr(
            api, "conda_standalone_executables", lambda: iter([STANDALONE])
        )
    else:
        monkeypatch.setattr(api, "install_conda_exe", lambda: STANDALONE)
    fake_version_outputs(monkeypatch, {"conda_standalone": "conda 25.1.1"})

    result = ensure(["Conda.Standalone>=25,<26"])

    assert result == CondaExecutable(STANDALONE, "conda_standalone", Version("25.1.1"))


@pytest.mark.parametrize(
    "targets, expected",
    [
        (["micromamba>=2", "conda_standalone>=24"], MICROMAMBA),
        (["conda_standalone>=24", "micromamba>=2"], STANDALONE),
    ],
)
def test_installation_follows_target_order(
    targets: List[str], expected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installations = []

    def micromamba() -> Path:
        installations.append(MICROMAMBA)
        return MICROMAMBA

    def standalone() -> Path:
        installations.append(STANDALONE)
        return STANDALONE

    monkeypatch.setattr(api, "install_micromamba", micromamba)
    monkeypatch.setattr(api, "install_conda_exe", standalone)
    calls = fake_version_outputs(
        monkeypatch, {"micromamba": "2.9.0", "conda_standalone": "conda 25.1.1"}
    )

    result = ensure(targets)

    assert result is not None and result.path == expected
    assert installations == [expected]
    assert calls == [expected.name]


@pytest.fixture
def unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_subdir() -> str:
        raise UnsupportedPlatformError("Unsupported platform 'freebsd14' (amd64)")

    monkeypatch.setattr(api, "platform_subdir", no_subdir)


@pytest.mark.parametrize(
    "target",
    ["python", "", "micromamba>=invalid", "conda[extra]", "conda; os_name == 'nt'"],
)
def test_all_targets_are_validated_before_searching(
    target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9"})

    with pytest.raises(ValueError):
        ensure(["micromamba", target])
    assert calls == []


def test_bare_string_is_not_a_target_sequence() -> None:
    with pytest.raises(TypeError, match="sequence of strings"):
        ensure("micromamba")
