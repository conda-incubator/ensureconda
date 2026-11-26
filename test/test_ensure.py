import inspect
import json
import subprocess
from pathlib import Path
from test.test_info import fake_version_outputs
from typing import Callable, List, Optional

import pytest
import requests
from click.testing import CliRunner
from packaging.version import InvalidVersion, Version

from ensureconda import (
    CondaExecutable,
    api,
    cli,
    determine_versions,
    ensure,
    ensureconda,
)
from ensureconda.api import Flavor, InstallPolicy
from ensureconda.installer import NotPublishedError
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

    def unexpected_install(constraint: object, **kwargs: object) -> Path:
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
    monkeypatch.setattr(api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA)
    calls = fake_version_outputs(monkeypatch, {})

    assert ensureconda() == MICROMAMBA
    assert ensureconda(no_install=True) == CONDA
    assert calls == []


def test_per_target_installs_before_searching_for_a_later_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA)
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(["micromamba", "conda"], install="per-target") == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0")
    )
    assert calls == ["micromamba"]


def test_fallback_is_the_default_install_value() -> None:
    assert inspect.signature(ensure).parameters["install"].default == "fallback"


@pytest.mark.parametrize("install", ["always", "Never", "", True, False, None])
def test_unknown_install_values_are_rejected_before_searching(
    install: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9"})

    with pytest.raises(ValueError, match="Unknown install value"):
        ensure(["micromamba"], install=install)  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.parametrize("install", ["never", "fallback", "per-target"])
@pytest.mark.parametrize("target", ["micromamba", "micromamba>=2"])
def test_probe_version_false_still_checks_constraints(
    install: InstallPolicy, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    result = ensure([target], install=install, probe_version=False)

    constrained = ">=" in target
    assert result == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0") if constrained else None
    )
    assert calls == (["micromamba"] if constrained else [])


def test_unprobed_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA)
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
    log_messages: Callable[[], str],
) -> None:
    def broken_version(executable: object) -> Version:
        raise error

    monkeypatch.setattr(
        determine_versions, "determine_micromamba_version", broken_version
    )
    if installed:
        monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    else:
        monkeypatch.setattr(
            api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA
        )

    result = ensure(["micromamba"], install="never" if installed else "fallback")

    assert result == (None if skip else CondaExecutable(MICROMAMBA, "micromamba", None))
    captured = log_messages()
    if skip:
        reason = (
            "`--version` exited with status 1"
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        assert captured == (
            f"Skipping {MICROMAMBA}: could not determine its version ({reason})\n"
        )
    else:
        assert captured == ""


def test_unconstrained_target_can_disable_execution_check(
    monkeypatch: pytest.MonkeyPatch, log_messages: Callable[[], str]
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    calls = fake_version_outputs(monkeypatch, {})

    assert ensure(["micromamba"], probe_version=False) == CondaExecutable(
        MICROMAMBA, "micromamba", None
    )
    assert calls == []
    assert log_messages() == ""


def test_failed_probe_reports_skip_before_installation(
    monkeypatch: pytest.MonkeyPatch, log_messages: Callable[[], str]
) -> None:
    def install(constraint: object, **kwargs: object) -> Path:
        assert f"Skipping {CONDA}" in log_messages()
        return MICROMAMBA

    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    monkeypatch.setattr(api, "install_micromamba", install)
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0"})

    assert ensure(["conda", "micromamba"]) == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.9.0")
    )
    assert calls == ["conda", "micromamba"]


def test_failed_probe_allows_a_later_installer(
    monkeypatch: pytest.MonkeyPatch, log_messages: Callable[[], str]
) -> None:
    monkeypatch.setattr(api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA)
    monkeypatch.setattr(api, "install_conda_exe", lambda *args, **kwargs: STANDALONE)
    calls = fake_version_outputs(monkeypatch, {"conda_standalone": "conda 25.1.1"})

    assert ensure(["micromamba", "conda_standalone"]) == CondaExecutable(
        STANDALONE, "conda_standalone", Version("25.1.1")
    )
    assert calls == ["micromamba", "conda_standalone"]
    assert f"Skipping {MICROMAMBA}" in log_messages()


def test_never_searches_later_targets_without_installing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    result = ensure(["micromamba", "conda"], install="never")

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


@pytest.mark.parametrize(
    "version", ["", "0.0.0", "0", "0.0.0.dev0+placeholder", "0+unknown"]
)
@pytest.mark.parametrize("flavor", ["mamba", "micromamba", "conda", "conda_standalone"])
@pytest.mark.parametrize("minimum", ["0", "1"])
def test_zero_sentinel_is_only_used_for_old_api_minimums(
    version: str, flavor: Flavor, minimum: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path("unknown")
    monkeypatch.setattr(api, f"{flavor}_executables", lambda: iter([path]))
    prefix = "conda " if version and flavor.startswith("conda") else ""
    fake_version_outputs(monkeypatch, {path.name: prefix + version})

    assert ensure([f"{flavor}>=0"], install="never") is None
    assert ensure([f"{flavor}<24"], install="never") is None
    assert ensure([flavor], install="never") == CondaExecutable(path, flavor, None)
    assert ensureconda(
        mamba=flavor == "mamba",
        micromamba=flavor == "micromamba",
        conda=flavor == "conda",
        conda_exe=flavor == "conda_standalone",
        no_install=True,
        min_mamba_version=Version(minimum),
        min_conda_version=Version(minimum),
    ) == (path if minimum == "0" else None)


@pytest.mark.parametrize("version", ["0.0.1", "0.0.0.1"])
def test_versions_with_a_nonzero_release_number_are_known(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    fake_version_outputs(monkeypatch, {"micromamba": version})

    assert ensure(["micromamba<24"], install="never") == CondaExecutable(
        MICROMAMBA, "micromamba", Version(version)
    )


@pytest.mark.parametrize("flavor", ["mamba", "micromamba", "conda", "conda_standalone"])
@pytest.mark.parametrize("mamba_minimum, conda_minimum", [("1", "3"), ("3", "1")])
def test_each_tool_uses_its_own_old_api_minimum(
    flavor: Flavor,
    mamba_minimum: str,
    conda_minimum: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("tool")
    monkeypatch.setattr(api, f"{flavor}_executables", lambda: iter([path]))
    is_mamba = flavor in ("mamba", "micromamba")
    fake_version_outputs(monkeypatch, {path.name: "2.0" if is_mamba else "conda 2.0"})
    minimum = mamba_minimum if is_mamba else conda_minimum

    assert ensureconda(
        mamba=flavor == "mamba",
        micromamba=flavor == "micromamba",
        conda=flavor == "conda",
        conda_exe=flavor == "conda_standalone",
        no_install=True,
        min_mamba_version=Version(mamba_minimum),
        min_conda_version=Version(conda_minimum),
    ) == (path if minimum == "1" else None)


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("target", ["micromamba>=1", "micromamba>=1,<3"])
def test_prereleases_satisfy_target_constraints(
    installed: bool, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if installed:
        monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    else:
        monkeypatch.setattr(
            api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA
        )
    fake_version_outputs(monkeypatch, {"micromamba": "2.0rc1"})

    # Pin today's policy until the explicit prerelease-policy change in #123.
    assert ensure([target]) == CondaExecutable(
        MICROMAMBA, "micromamba", Version("2.0rc1")
    )


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
        monkeypatch.setattr(
            api, "install_conda_exe", lambda *args, **kwargs: STANDALONE
        )
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

    def micromamba(constraint: object, **kwargs: object) -> Path:
        installations.append(MICROMAMBA)
        return MICROMAMBA

    def standalone(constraint: object, **kwargs: object) -> Path:
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
    monkeypatch.setattr(api, "install_micromamba", lambda *args, **kwargs: MICROMAMBA)
    monkeypatch.setattr(api, "install_conda_exe", lambda *args, **kwargs: STANDALONE)
    calls = fake_version_outputs(
        monkeypatch, {"micromamba": "2.9.0", "conda_standalone": "conda 25.1.1"}
    )

    assert ensure(["micromamba<2", "conda_standalone>=24"]) == CondaExecutable(
        STANDALONE, "conda_standalone", Version("25.1.1")
    )
    assert calls == ["micromamba", "conda_standalone"]


@pytest.mark.parametrize("flavor", ["micromamba", "conda_standalone"])
def test_unpublished_target_falls_back_in_either_order(
    flavor: str, monkeypatch: pytest.MonkeyPatch, log_messages: Callable[[], str]
) -> None:
    def unpublished(constraint: object, **kwargs: object) -> Path:
        raise NotPublishedError(f"{flavor} is not published")

    other = "conda_standalone" if flavor == "micromamba" else "micromamba"
    expected = STANDALONE if other == "conda_standalone" else MICROMAMBA
    monkeypatch.setattr(
        api,
        "install_micromamba",
        unpublished if flavor == "micromamba" else lambda *args, **kwargs: MICROMAMBA,
    )
    monkeypatch.setattr(
        api,
        "install_conda_exe",
        (
            unpublished
            if flavor == "conda_standalone"
            else lambda *args, **kwargs: STANDALONE
        ),
    )
    fake_version_outputs(
        monkeypatch, {"micromamba": "2.9", "conda_standalone": "conda 25"}
    )

    result = ensure([flavor, other])

    assert result is not None and result.path == expected
    assert f"trying {other.replace('_', '-')} instead" in log_messages()


@pytest.mark.parametrize(
    "error", [requests.HTTPError("404 Error"), requests.ConnectionError("Disconnected")]
)
def test_download_errors_propagate_without_fallback(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(constraint: object, **kwargs: object) -> Path:
        raise error

    monkeypatch.setattr(api, "install_micromamba", fail)
    with pytest.raises(type(error)):
        ensure(["micromamba", "conda_standalone"])


def test_last_unpublished_target_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def unpublished(constraint: object, **kwargs: object) -> Path:
        raise NotPublishedError("micromamba is not published")

    monkeypatch.setattr(api, "install_micromamba", unpublished)
    with pytest.raises(NotPublishedError, match="micromamba is not published"):
        ensure(["micromamba"])


@pytest.mark.parametrize("install", ["fallback", "per-target"])
@pytest.mark.parametrize("flavor", ["micromamba", "conda_standalone"])
def test_unpublished_tool_is_requested_once_per_call(
    install: InstallPolicy,
    flavor: str,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: Callable[[], str],
) -> None:
    requests = []

    def unpublished(constraint: object, **kwargs: object) -> Path:
        requests.append(flavor)
        raise NotPublishedError(f"{flavor} is not published")

    other = "conda_standalone" if flavor == "micromamba" else "micromamba"
    monkeypatch.setattr(
        api,
        "install_micromamba",
        unpublished if flavor == "micromamba" else lambda *a, **k: MICROMAMBA,
    )
    monkeypatch.setattr(
        api,
        "install_conda_exe",
        unpublished if flavor == "conda_standalone" else lambda *a, **k: STANDALONE,
    )
    fake_version_outputs(
        monkeypatch, {"micromamba": "2.9", "conda_standalone": "conda 25"}
    )
    targets = [flavor, f"{other}>=99", f"{flavor}>=1"]

    # A second invocation must try again: this is not persistent caching.
    for count in [1, 2]:
        assert ensure(targets, install=install) is None
        assert requests == [flavor] * count
        assert (
            f"{flavor} is not published; trying {other.replace('_', '-')} instead\n"
            in log_messages()
        )


@pytest.mark.parametrize("found", [False, True])
def test_unpublished_tool_can_still_be_found_by_a_later_search(
    found: bool, monkeypatch: pytest.MonkeyPatch, log_messages: Callable[[], str]
) -> None:
    requests = []

    def unpublished(constraint: object, **kwargs: object) -> Path:
        requests.append("micromamba")
        raise NotPublishedError("micromamba is not published")

    monkeypatch.setattr(api, "install_micromamba", unpublished)
    monkeypatch.setattr(
        api, "micromamba_executables", lambda: iter([MICROMAMBA] if found else [])
    )
    fake_version_outputs(monkeypatch, {"micromamba": "2.0"})
    targets = ["micromamba>=3", "micromamba>=1"]

    if found:
        assert ensure(targets, install="per-target") == CondaExecutable(
            MICROMAMBA, "micromamba", Version("2.0")
        )
    else:
        with pytest.raises(NotPublishedError, match="micromamba is not published"):
            ensure(targets, install="per-target")
    assert requests == ["micromamba"]
    assert (
        "micromamba is not published; checking installed copies instead\n"
        in log_messages()
    )


@pytest.fixture
def unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_subdir() -> str:
        raise UnsupportedPlatformError("Unsupported platform 'freebsd14' (amd64)")

    monkeypatch.setattr(api, "platform_subdir", no_subdir)


@pytest.mark.usefixtures("unsupported_platform")
@pytest.mark.parametrize("install", ["fallback", "per-target"])
def test_unsupported_platform_still_finds_an_installed_target(
    install: InstallPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    # The no_tools fixture fails the test if an installer is called.
    result = ensure(["micromamba", "conda"], install=install)

    assert result is not None and result.path == CONDA


@pytest.mark.usefixtures("unsupported_platform")
@pytest.mark.parametrize("install", ["fallback", "per-target"])
def test_unsupported_platform_with_nothing_installed(install: InstallPolicy) -> None:
    with pytest.raises(UnsupportedPlatformError, match="freebsd14"):
        ensure(["micromamba", "conda"], install=install)


@pytest.mark.usefixtures("unsupported_platform")
def test_unsupported_platform_is_no_error_when_nothing_would_be_installed() -> None:
    assert ensure(["micromamba", "conda"], install="never") is None
    assert ensure(["mamba", "conda"]) is None


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


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_skips_a_broken_executable(
    as_json: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    monkeypatch.setattr(api, "conda_executables", lambda: iter([CONDA]))
    calls = fake_version_outputs(monkeypatch, {"conda": "conda 25.1.1"})

    arguments = ["--no-install"] + (["--json"] if as_json else [])
    result = CliRunner().invoke(cli.ensureconda_cli, arguments)

    assert result.exit_code == 0
    assert f"Skipping {MICROMAMBA}" in result.output
    output = result.stdout.splitlines()[-1]
    assert (json.loads(output)["path"] if as_json else output) == str(CONDA)
    assert calls == ["micromamba", "conda"]


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("output, minimum", [("", "0"), ("2.0+local", "2.0+local")])
def test_cli_keeps_old_minimum_semantics_in_both_output_formats(
    as_json: bool, output: str, minimum: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))
    fake_version_outputs(monkeypatch, {"micromamba": output})
    arguments = ["--no-install", "--min-mamba-version", minimum]
    if as_json:
        arguments.append("--json")

    result = CliRunner().invoke(cli.ensureconda_cli, arguments)

    assert result.exit_code == 0
    last_line = result.stdout.splitlines()[-1]
    if as_json:
        assert (
            json.loads(last_line)
            == CondaExecutable(
                MICROMAMBA, "micromamba", Version(output) if output else None
            ).to_json()
        )
    else:
        assert last_line == str(MICROMAMBA)


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_no_install_installs_nothing(as_json: bool) -> None:
    arguments = ["--no-install", "--json"] if as_json else ["--no-install"]

    result = CliRunner().invoke(cli.ensureconda_cli, arguments)

    # The no_tools fixture fails the test if an installer is called.
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Could not find compatible executable" in result.output


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
    def fail(constraint: object, **kwargs: object) -> Path:
        raise error

    monkeypatch.setattr(api, "install_micromamba", fail)

    result = CliRunner().invoke(cli.ensureconda_cli, ["--json", "--no-conda-exe"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Error:" in result.output
    assert str(error) in result.output


@pytest.mark.usefixtures("unsupported_platform")
def test_cli_json_ends_with_one_line_on_an_unsupported_platform() -> None:
    result = CliRunner().invoke(cli.ensureconda_cli, ["--json"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.output == "Error: Unsupported platform 'freebsd14' (amd64)\n"
