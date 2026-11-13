import json
import subprocess
from pathlib import Path
from test.test_info import fake_version_outputs
from typing import List, Optional

import pytest
import requests
from click.testing import CliRunner
from packaging.version import InvalidVersion, Version

from ensureconda import CondaExecutable, api, cli, ensure, ensureconda
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


def test_legacy_api_installs_micromamba_before_searching_for_conda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)
    calls = fake_version_outputs(monkeypatch, {})

    assert ensureconda() == MICROMAMBA
    assert ensureconda(no_install=True) == CONDA
    assert calls == []


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


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize(
    "error, skip",
    [
        (subprocess.CalledProcessError(1, [str(MICROMAMBA), "--version"]), True),
        (OSError(8, "Exec format error"), True),
        (InvalidVersion("Invalid version: 'garbage'"), False),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), False),
    ],
    ids=["nonzero-exit", "exec-error", "invalid-version", "undecodable-output"],
)
def test_unconstrained_probe_errors(
    installed: bool,
    error: Exception,
    skip: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken_version(executable: object) -> Version:
        raise error

    monkeypatch.setattr(api, "determine_micromamba_version", broken_version)
    if installed:
        monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    else:
        monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)

    result = ensure(["micromamba"], no_install=installed)

    assert result == (None if skip else CondaExecutable(MICROMAMBA, "micromamba", None))
    captured = capsys.readouterr()
    assert captured.out == ""
    if skip:
        reason = (
            "`--version` exited with status 1"
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        assert captured.err == (
            f"Skipping {MICROMAMBA}: could not determine its version ({reason})\n"
        )
    else:
        assert captured.err == ""


def test_unconstrained_target_can_disable_execution_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {})

    assert ensure(["micromamba"], probe_version=False) == CondaExecutable(
        MICROMAMBA, "micromamba", None
    )
    assert calls == []
    assert capsys.readouterr().err == ""


def test_failed_probe_reports_skip_before_installation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def install() -> Path:
        assert f"Skipping {CONDA}" in capsys.readouterr().err
        return MICROMAMBA

    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "install_micromamba", install)
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(["conda", "micromamba"]) == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0")
    )
    assert calls == ["conda", "micromamba"]


def test_failed_probe_allows_a_later_installer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api, "install_micromamba", lambda: MICROMAMBA)
    monkeypatch.setattr(api, "install_conda_exe", lambda: STANDALONE)
    calls = fake_version_outputs(monkeypatch, {"conda_standalone": "conda 25.1.1"})

    assert ensure(["micromamba", "conda_standalone"]) == CondaExecutable(
        STANDALONE, "conda_standalone", Version("25.1.1")
    )
    assert calls == ["micromamba", "conda_standalone"]
    assert f"Skipping {MICROMAMBA}" in capsys.readouterr().err


def test_no_install_applies_with_target_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    result = ensure(["micromamba", "conda"], prefer_installed=False, no_install=True)

    assert result is not None and result.path == CONDA


@pytest.mark.parametrize("version", ["2.0rc1", ""])
def test_legacy_version_comparison_is_preserved(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": version})

    # Legacy comparisons accept prereleases and the probes' 0.0.0 fallback.
    assert ensureconda(no_install=True, min_mamba_version=Version("0")) == MICROMAMBA
    assert calls == ["micromamba"]


@pytest.mark.parametrize(
    "version, expected",
    [("1.0", None), ("1.0+aaa", None), ("1.0+local", MICROMAMBA), ("1.1", MICROMAMBA)],
)
def test_legacy_minimum_can_have_a_local_version_label(
    version: str, expected: Optional[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    fake_version_outputs(monkeypatch, {"micromamba": version})

    assert (
        ensureconda(no_install=True, min_mamba_version=Version("1.0+local")) == expected
    )


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


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_prefers_installed_conda(
    as_json: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    result = CliRunner().invoke(cli.ensureconda_cli, ["--json"] if as_json else [])

    assert result.exit_code == 0
    output = result.stdout.splitlines()[-1]
    assert (json.loads(output)["path"] if as_json else output) == str(CONDA)


def test_cli_json_translates_legacy_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "mamba_executables", lambda: iter([Path("mamba")]))
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "conda_standalone_executables", lambda: iter([STANDALONE]))
    calls = fake_version_outputs(
        monkeypatch, {"micromamba": "2.9.0", "conda_standalone": "conda 25.1.1"}
    )

    result = CliRunner().invoke(
        cli.ensureconda_cli,
        [
            "--json",
            "--no-mamba",
            "--no-conda",
            "--min-mamba-version",
            "3",
            "--min-conda-version",
            "25",
            "--no-install",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout.splitlines()[-1])["flavor"] == "conda_standalone"
    assert calls == ["micromamba", "conda_standalone"]


@pytest.mark.parametrize(
    "error", [NotPublishedError("No build"), requests.ConnectionError("Disconnected")]
)
def test_cli_json_reports_installation_failure(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> Path:
        raise error

    monkeypatch.setattr(api, "install_micromamba", fail)

    result = CliRunner().invoke(cli.ensureconda_cli, ["--json", "--no-conda-exe"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Error:" in result.output
    assert str(error) in result.output
