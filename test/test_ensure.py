from pathlib import Path
from test.test_info import fake_version_outputs
from typing import List

import pytest
import requests
from packaging.version import Version

from ensureconda import CondaExecutable, api, ensure
from ensureconda.installer import NotPublishedError

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


def test_prefer_installed_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(["micromamba", "conda"], prefer_installed=False) == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0")
    )
    assert calls == ["micromamba"]


@pytest.mark.parametrize("prefer_installed", [False, True])
@pytest.mark.parametrize("target", ["micromamba", "micromamba>=2"])
def test_probe_version_false_still_checks_constraints(
    prefer_installed: bool, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    result = ensure([target], prefer_installed=prefer_installed, probe_version=False)

    constrained = ">=" in target
    assert result == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0") if constrained else None
    )
    assert calls == (["micromamba"] if constrained else [])


def test_unprobed_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)
    calls = fake_version_outputs(monkeypatch, {})

    assert ensure(["micromamba"], probe_version=False) == CondaExecutable(
        MICROMAMBA, "micromamba", None
    )
    assert calls == []


def test_no_install_applies_with_target_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    result = ensure(["micromamba", "conda"], prefer_installed=False, no_install=True)

    assert result is not None and result.path == CONDA


@pytest.mark.parametrize("targets", [[], ["conda"], ["mamba"]])
def test_only_selected_targets_are_considered(
    targets: List[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(targets) is None
    assert calls == []


def test_no_install() -> None:
    assert ensure(["micromamba", "conda_standalone"], no_install=True) is None


@pytest.mark.parametrize("output", ["1.9.0", "3.0.0", "2.5.0", "garbage", ""])
def test_target_constraints_allow_a_later_installed_tool(
    output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    calls = fake_version_outputs(
        monkeypatch, {"micromamba": output, "conda": "conda 25.1.1"}
    )

    result = ensure(["micromamba>=2,<3,!=2.5.0", "conda>=24,<27"])

    assert result == CondaExecutable(CONDA, "conda", Version("25.1.1"))
    assert calls == ["micromamba", "conda"]


def test_search_continues_within_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    older, newer = Path("older"), Path("newer")
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([older, newer]))
    calls = fake_version_outputs(monkeypatch, {"older": "1.0", "newer": "2.9"})

    assert ensure(["micromamba>=2"]) == CondaExecutable(
        newer, "micromamba", Version("2.9")
    )
    assert calls == ["older", "newer"]


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


def test_installed_download_must_satisfy_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)
    monkeypatch.setattr(api, "install_conda_exe", lambda: STANDALONE)
    calls = fake_version_outputs(
        monkeypatch, {"micromamba": "2.9.0", "conda_standalone": "conda 25.1.1"}
    )

    assert ensure(["micromamba<2", "conda_standalone>=24"]) == CondaExecutable(
        STANDALONE, "conda_standalone", Version("25.1.1")
    )
    assert calls == ["micromamba", "conda_standalone"]


@pytest.mark.parametrize("flavor", ["micromamba", "conda_standalone"])
def test_unpublished_target_falls_back_in_either_order(
    flavor: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unpublished() -> Path:
        raise NotPublishedError(f"{flavor} is not published")

    other = "conda_standalone" if flavor == "micromamba" else "micromamba"
    expected = STANDALONE if other == "conda_standalone" else MICROMAMBA
    monkeypatch.setattr(
        api,
        "install_micromamba",
        unpublished if flavor == "micromamba" else lambda: MICROMAMBA,
    )
    monkeypatch.setattr(
        api,
        "install_conda_exe",
        unpublished if flavor == "conda_standalone" else lambda: STANDALONE,
    )
    fake_version_outputs(
        monkeypatch, {"micromamba": "2.9", "conda_standalone": "conda 25"}
    )

    result = ensure([flavor, other])

    assert result is not None and result.path == expected
    assert f"trying {other.replace('_', '-')} instead" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [requests.HTTPError("404 Error"), requests.ConnectionError("Disconnected")]
)
def test_download_errors_propagate_without_fallback(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> Path:
        raise error

    monkeypatch.setattr(api, "install_micromamba", fail)
    with pytest.raises(type(error)):
        ensure(["micromamba", "conda_standalone"])


def test_last_unpublished_target_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def unpublished() -> Path:
        raise NotPublishedError("micromamba is not published")

    monkeypatch.setattr(api, "install_micromamba", unpublished)
    with pytest.raises(NotPublishedError, match="micromamba is not published"):
        ensure(["micromamba"])


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
