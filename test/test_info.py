import json
import logging
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, Dict, List, Union, cast

import pytest
from click.testing import CliRunner
from packaging.version import Version

from ensureconda import api, cli, determine_versions, ensure
from ensureconda.api import CondaExecutable, Flavor, ensureconda, identify

MICROMAMBA = Path("data", "micromamba")


def executable_file(path: Path) -> Path:
    """Create an empty executable file at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def fake_version_outputs(
    monkeypatch: pytest.MonkeyPatch, outputs: Dict[str, str]
) -> List[str]:
    """Answer `<exe> --version` from outputs, keyed by file name; return the calls."""
    calls: List[str] = []

    def check_output(args: List[Any], **kwargs: Any) -> bytes:
        name = Path(args[0]).name
        calls.append(name)
        if name not in outputs:
            raise subprocess.CalledProcessError(1, args)
        return outputs[name].encode()

    monkeypatch.setattr(subprocess, "check_output", check_output)
    return calls


@pytest.mark.parametrize("flavor", ["python", "conda-standalone"])
def test_probe_version_rejects_unknown_flavor_without_running_it(
    flavor: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_version_outputs(monkeypatch, {})

    with pytest.raises(ValueError, match="Unknown executable flavor"):
        determine_versions._probe_version(Path("tool"), cast(Flavor, flavor), None)
    assert calls == []


@pytest.fixture
def only_micromamba(monkeypatch: pytest.MonkeyPatch) -> None:
    for finder in ["mamba_executables", "conda_executables"]:
        monkeypatch.setattr(api, finder, lambda: iter([]))
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([MICROMAMBA]))


@pytest.mark.usefixtures("only_micromamba")
def test_ensure_reports_flavor_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_version_outputs(monkeypatch, {"micromamba": "2.9.0\n"})

    info = ensure(install="never")

    assert info == CondaExecutable(MICROMAMBA, "micromamba", Version("2.9.0"))
    assert not isinstance(info, Iterable)
    assert {info} == {CondaExecutable(MICROMAMBA, "micromamba", Version("2.9.0"))}
    with pytest.raises(AttributeError):
        setattr(info, "path", Path("elsewhere"))
    assert info.to_json() == {
        "path": str(MICROMAMBA),
        "flavor": "micromamba",
        "version": "2.9.0",
    }


@pytest.mark.usefixtures("only_micromamba")
@pytest.mark.parametrize("output", ["garbage", ""])
def test_ensure_reports_unknown_version(
    output: str, monkeypatch: pytest.MonkeyPatch, log_messages: Callable[[], str]
) -> None:
    fake_version_outputs(monkeypatch, {"micromamba": output})

    assert ensure(install="never") == CondaExecutable(MICROMAMBA, "micromamba", None)
    assert log_messages() == ""


@pytest.mark.usefixtures("only_micromamba")
def test_ensureconda_still_runs_no_version_check_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0\n"})

    assert ensureconda(no_install=True) == MICROMAMBA
    assert calls == []


@pytest.mark.usefixtures("only_micromamba")
def test_ensure_reuses_the_target_version_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0\n"})

    info = ensure(["micromamba>=1.0"], install="never")

    assert info == CondaExecutable(MICROMAMBA, "micromamba", Version("2.9.0"))
    assert calls == ["micromamba"]


@pytest.mark.parametrize(
    "path, output, flavor, version",
    [
        ("bin/mamba", "mamba 1.5.12\nconda 24.11.3\n", "mamba", "1.5.12"),
        ("bin/mamba", "2.9.0\n", "mamba", "2.9.0"),
        ("bin/micromamba", "2.9.0\n", "micromamba", "2.9.0"),
        ("Scripts/conda.exe", "conda 26.5.2\n", "conda", "26.5.2"),
        ("condabin/conda.bat", "conda 26.5.2\n", "conda", "26.5.2"),
        ("data/conda_standalone", "conda 26.5.2\n", "conda_standalone", "26.5.2"),
        ("bin/conda-standalone", "conda 26.5.2\n", "conda_standalone", "26.5.2"),
        ("bin/conda-standalone.exe", "conda 26.5.2\n", "conda_standalone", "26.5.2"),
        # Unrecognized names fall back to the shape of the output.
        ("bin/mm", "2.9.0\n", "micromamba", "2.9.0"),
        ("bin/my-mamba", "mamba 1.5.12\nconda 24.11.3\n", "mamba", "1.5.12"),
        (
            "bin/my-mamba",
            "conda 24.11.3\nmambaforge 9.0\nmamba 1.5.12",
            "mamba",
            "1.5.12",
        ),
        ("bin/my-conda", "conda 26.5.2\n", "conda", "26.5.2"),
    ],
)
def test_identify(
    path: str,
    output: str,
    flavor: Flavor,
    version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = executable_file(tmp_path / path)
    calls = fake_version_outputs(monkeypatch, {executable.name: output})

    assert identify(executable) == CondaExecutable(executable, flavor, Version(version))
    assert calls == [executable.name]


@pytest.fixture
def set_conda_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], Path]:
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", ".exe")

    def create(name: str) -> Path:
        executable = executable_file(tmp_path / name)
        monkeypatch.setenv("CONDA_EXE", str(executable))
        return executable

    return create


@pytest.mark.parametrize(
    "banner",
    [
        "Loading profile",
        "mambaforge build 9",
        "conda-wrapper 9",
        "mamba shell integration enabled",
        "conda shell integration enabled",
    ],
)
def test_conda_wrapper_reports_its_version_after_a_banner(
    banner: str,
    set_conda_exe: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = set_conda_exe("conda-wrapper")
    calls = fake_version_outputs(monkeypatch, {wrapper.name: f"{banner}\nconda 24.1.2"})
    expected = CondaExecutable(wrapper, "conda", Version("24.1.2"))

    assert ensure(["conda>=24"], install="never") == expected
    assert ensure(["conda"], install="never") == expected
    assert identify(wrapper) == expected
    assert ensureconda(min_conda_version=Version("24"), no_install=True) == wrapper
    assert calls == [wrapper.name] * 4


@pytest.mark.parametrize(
    "output, flavor, version",
    [
        ("mamba 1.5.12\nconda 24.11.3", "mamba", "1.5.12"),
        (
            "mamba shell integration enabled\nconda 24.11.3\nmamba 1.5.12",
            "mamba",
            "1.5.12",
        ),
        ("mamba shell integration enabled\nconda 24.11.3", "conda", "24.11.3"),
    ],
)
def test_conda_name_with_mamba_in_its_output(
    output: str,
    flavor: Flavor,
    version: str,
    set_conda_exe: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = set_conda_exe("conda")
    other = executable_file(tmp_path / "bin" / "conda.exe")
    monkeypatch.setenv("PATH", str(other.parent))
    calls = fake_version_outputs(
        monkeypatch, {alias.name: output, other.name: "conda 25.1"}
    )
    expected = CondaExecutable(alias, flavor, Version(version))

    assert identify(alias) == expected
    assert calls == [alias.name]
    for target, probe in [("conda", True), ("conda>=24", False)]:
        calls.clear()
        result = ensure([target], install="never", probe_version=probe)
        if flavor == "mamba":
            assert result == CondaExecutable(other, "conda", Version("25.1"))
            assert calls == [alias.name, other.name]
        else:
            assert result == expected
            assert calls == [alias.name]


@pytest.mark.parametrize(
    "name, output, flavor, version",
    [
        ("micromamba", "2.9.0", "micromamba", "2.9.0"),
        ("mamba.exe", "2.9.0", "mamba", "2.9.0"),
        ("mamba", "mamba 1.5.12\nconda 24.11.3", "mamba", "1.5.12"),
        ("wrapper", "mamba 1.5.12\nconda 24.11.3", "mamba", "1.5.12"),
        ("wrapper", "2.9.0", "micromamba", "2.9.0"),
        ("wrapper", "conda 24.11.3", "conda", "24.11.3"),
        ("conda-standalone", "conda 24.11.3", "conda_standalone", "24.11.3"),
    ],
)
def test_conda_target_requires_a_conda_family_override(
    name: str,
    output: str,
    flavor: Flavor,
    version: str,
    set_conda_exe: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = set_conda_exe(name)
    calls = fake_version_outputs(monkeypatch, {name: output})

    info = CondaExecutable(executable, flavor, Version(version))
    assert identify(executable) == info
    calls.clear()
    in_family = flavor in ("conda", "conda_standalone")
    result = ensure(["conda"], install="never")
    assert result == (info if in_family else None)
    assert calls == ([name] if in_family or name == "wrapper" else [])

    calls.clear()
    assert ensure(["conda"], install="never", probe_version=False) == (
        CondaExecutable(executable, flavor, None)
        if in_family and name != "wrapper"
        else None
    )
    assert ensureconda(no_install=True) == executable
    assert calls == []


def test_micromamba_minimum_and_conda_fallback(
    set_conda_exe: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = set_conda_exe("micromamba")
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(bin_dir))
    outputs = {
        "micromamba": "1.5.8",
        "conda.exe": "conda 24.1.2",
        "micromamba.exe": "2.9.0",
    }
    calls = fake_version_outputs(monkeypatch, outputs)
    targets = ["micromamba>=2", "conda"]

    assert ensure(targets, install="never") is None
    assert ensure([*targets, "micromamba"], install="never") == CondaExecutable(
        override, "micromamba", Version("1.5.8")
    )
    conda = executable_file(bin_dir / "conda.exe")
    assert ensure(targets, install="never") == CondaExecutable(
        conda, "conda", Version("24.1.2")
    )
    outputs[override.name] = "3.0.0"
    assert ensure(targets, install="never") == CondaExecutable(
        override, "micromamba", Version("3.0.0")
    )
    micromamba = executable_file(bin_dir / "micromamba.exe")
    calls.clear()
    assert ensure(targets, install="never") == CondaExecutable(
        override, "micromamba", Version("3.0.0")
    )
    assert calls == [override.name]
    outputs[override.name] = "1.5.8"
    assert ensure(targets, install="never") == CondaExecutable(
        micromamba, "micromamba", Version("2.9.0")
    )


@pytest.mark.parametrize("flavor", ["mamba", "micromamba"])
def test_mamba_shell_launchers_precede_cache_and_path(
    flavor: Flavor,
    set_conda_exe: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferred = executable_file(tmp_path / "shell" / f"{flavor}.cmd")
    monkeypatch.setenv("MAMBA_EXE", str(preferred))
    secondary = set_conda_exe(f"{flavor}.bat")
    cached = executable_file(tmp_path / "cache" / f"{flavor}.exe")
    on_path = executable_file(tmp_path / "bin" / f"{flavor}.exe")
    monkeypatch.setenv("PATH", str(on_path.parent))
    outputs = {preferred.name: "2.3", secondary.name: "2.2", cached.name: "2.1"}
    calls = fake_version_outputs(monkeypatch, outputs)
    targets = [f"{flavor}>=2"]

    assert ensure(targets, install="never") == CondaExecutable(
        preferred, flavor, Version("2.3")
    )
    assert calls == [preferred.name]
    outputs[preferred.name] = "1.9"
    assert ensure(targets, install="never") == CondaExecutable(
        secondary, flavor, Version("2.2")
    )
    secondary.unlink()
    assert ensure(targets, install="never") == CondaExecutable(
        cached, flavor, Version("2.1")
    )
    cached.unlink()
    assert ensure(targets, install="never") == CondaExecutable(
        on_path, flavor, Version("2.1")
    )


@pytest.mark.parametrize(
    "target, other", [("mamba", "micromamba"), ("micromamba", "mamba")]
)
def test_mamba_launcher_must_match_the_requested_tool(
    target: Flavor,
    other: Flavor,
    set_conda_exe: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = executable_file(tmp_path / other)
    monkeypatch.setenv("MAMBA_EXE", str(wrong))
    matching = set_conda_exe(target)
    fake_version_outputs(monkeypatch, {wrong.name: "2.3", matching.name: "2.2"})

    assert ensure([f"{target}>=2"], install="never") == CondaExecutable(
        matching, target, Version("2.2")
    )
    assert ensure([target], install="never", probe_version=False) == CondaExecutable(
        matching, target, None
    )


@pytest.mark.parametrize(
    "name, output, flavor, version",
    [
        ("micromamba", "2.9.0", "micromamba", "2.9.0"),
        ("wrapper", "2.9.0", "micromamba", "2.9.0"),
        ("mamba.exe", "2.9.0", "mamba", "2.9.0"),
        ("conda", "mamba 1.5.12\nconda 24.11.3", "mamba", "1.5.12"),
        ("conda-standalone", "conda 24.11.3", "conda_standalone", "24.11.3"),
    ],
)
def test_other_targets_check_conda_exe_as_the_requested_tool(
    name: str,
    output: str,
    flavor: Flavor,
    version: str,
    set_conda_exe: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = set_conda_exe(name)
    calls = fake_version_outputs(monkeypatch, {name: output})

    assert ensure([f"{flavor}=={version}"], install="never") == CondaExecutable(
        override, flavor, Version(version)
    )
    assert calls == [name]
    calls.clear()
    named = name in ("micromamba", "mamba.exe", "conda-standalone")
    assert ensure([flavor], install="never", probe_version=False) == (
        CondaExecutable(override, flavor, None) if named else None
    )
    assert ensureconda(no_install=True, conda=False) == (override if named else None)
    assert calls == []


def test_shell_launchers_also_on_path_are_probed_once_per_target(
    set_conda_exe: Callable[[str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = set_conda_exe("micromamba.exe")
    monkeypatch.setenv("MAMBA_EXE", str(override))
    monkeypatch.setenv("PATH", str(override.parent))
    monkeypatch.setattr(determine_versions, "_fingerprint", lambda path: None)
    calls = fake_version_outputs(monkeypatch, {override.name: "1.5.8"})

    assert ensure(["micromamba>=2"], install="never") is None
    assert calls == [override.name]


@pytest.mark.parametrize("variable", ["MAMBA_EXE", "CONDA_EXE"])
def test_legacy_api_and_cli_select_mamba_under_its_own_target(
    variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mamba = executable_file(tmp_path / "mamba")
    monkeypatch.setenv(variable, str(mamba))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", str(tmp_path / "empty"))
    fake_version_outputs(monkeypatch, {mamba.name: "mamba 1.5.12\nconda 24.11.3"})

    assert ensureconda(no_install=True, min_mamba_version=Version("1.5")) == mamba
    runner = CliRunner()
    args = ["--no-install", "--verbosity", "0"]
    plain = runner.invoke(cli.ensureconda_cli, args)
    assert plain.exit_code == 0
    assert plain.stdout.strip() == str(mamba)
    metadata = runner.invoke(cli.ensureconda_cli, [*args, "--json"])
    assert metadata.exit_code == 0
    assert (
        json.loads(metadata.stdout)
        == CondaExecutable(mamba, "mamba", Version("1.5.12")).to_json()
    )


@pytest.mark.parametrize(
    "name, output",
    [("mamba", "mamba 1.5.12\nconda 24.11.3"), ("micromamba", "1.5.8")],
)
def test_only_conda_enabled_skips_a_known_mamba_launcher(
    name: str,
    output: str,
    set_conda_exe: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_conda_exe(name)
    calls = fake_version_outputs(monkeypatch, {name: output, "conda.exe": "conda 25.1"})
    for minimum in (None, Version("0"), Version("4.8.2")):
        assert (
            ensureconda(
                mamba=False,
                micromamba=False,
                conda_exe=False,
                min_conda_version=minimum,
                no_install=True,
            )
            is None
        )

    args = ["--no-install", "--no-mamba", "--no-micromamba", "--no-conda-exe"]
    runner = CliRunner()
    for minimum_arg in ([], ["--min-conda-version", "0"]):
        result = runner.invoke(cli.ensureconda_cli, [*args, *minimum_arg])
        assert result.exit_code == 1
    assert calls == []

    conda = executable_file(tmp_path / "bin" / "conda.exe")
    monkeypatch.setenv("PATH", str(conda.parent))
    result = runner.invoke(cli.ensureconda_cli, [*args, "--json", "--verbosity", "0"])
    assert result.exit_code == 0
    assert (
        json.loads(result.stdout)
        == CondaExecutable(conda, "conda", Version("25.1")).to_json()
    )
    assert calls == [conda.name]


@pytest.mark.parametrize(
    "name, output, flavor",
    [
        ("mamba", "mamba 1.5.12\nconda 24.11.3", "mamba"),
        ("micromamba.exe", "garbage", "micromamba"),
        ("wrapper", "mamba 1.5.12\nconda 24.11.3", "mamba"),
        ("wrapper", "mamba garbage\nconda 24.11.3", "mamba"),
        ("wrapper", "2.9.0", "micromamba"),
    ],
)
def test_conda_constraint_skips_mamba_in_conda_exe(
    name: str,
    output: str,
    flavor: Flavor,
    set_conda_exe: Callable[[str], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    executable = set_conda_exe(name)
    calls = fake_version_outputs(monkeypatch, {name: output, "conda.exe": "conda 25.1"})
    probes = [name] if name == "wrapper" else []

    with caplog.at_level(logging.INFO, logger="ensureconda"):
        assert ensure(["conda>=24"], install="never", probe_version=False) is None
    assert caplog.record_tuples == [
        (
            determine_versions.__name__,
            logging.INFO,
            f"Rejected {executable} ({flavor}): requires conda>=24",
        )
    ]
    assert calls == probes

    calls.clear()
    assert ensure(["conda>=1"], install="never") is None
    assert calls == probes

    calls.clear()
    conda = executable_file(tmp_path / "cache" / "conda.exe")
    assert ensure(["conda>=24"], install="never") == CondaExecutable(
        conda, "conda", Version("25.1")
    )
    assert calls == probes + [conda.name]


def test_cli_json_reports_conda_standalone_from_conda_exe(
    set_conda_exe: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = set_conda_exe("conda-standalone.exe")
    fake_version_outputs(monkeypatch, {executable.name: "conda 25.1"})

    result = CliRunner().invoke(cli.ensureconda_cli, ["--json", "--no-install"])

    assert result.exit_code == 0
    assert f"Selected conda_standalone 25.1 at {executable}" in result.output
    assert (
        json.loads(result.stdout.splitlines()[-1])
        == CondaExecutable(executable, "conda_standalone", Version("25.1")).to_json()
    )


def test_unrecognized_conda_exe_is_only_a_legacy_fallback(
    set_conda_exe: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = set_conda_exe("wrapper")
    fake_version_outputs(monkeypatch, {"wrapper": "unknown"})

    assert ensure(["conda"], install="never") is None
    assert ensure(["conda>=0"], install="never") is None
    assert ensureconda(no_install=True) == wrapper
    assert ensureconda(no_install=True, min_conda_version=Version("0")) == wrapper


def test_identify_rejects_unrecognized_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = executable_file(tmp_path / "bin" / "python")
    fake_version_outputs(monkeypatch, {"python": "Python 3.13.2\n"})

    with pytest.raises(ValueError, match="not a recognized conda executable"):
        identify(python)


@pytest.mark.parametrize(
    "probe_result",
    [
        subprocess.CalledProcessError(1, ["micromamba", "--version"]),
        OSError(8, "Exec format error"),
        b"garbage",
        b"\xff",
    ],
    ids=["nonzero-exit", "exec-error", "invalid-version", "undecodable-output"],
)
def test_identify_still_reports_unknown_version_after_probe_failure(
    probe_result: Union[Exception, bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: Callable[[], str],
) -> None:
    def broken_version(*args: object, **kwargs: object) -> bytes:
        if isinstance(probe_result, Exception):
            raise probe_result
        return probe_result

    executable = executable_file(tmp_path / "micromamba")
    monkeypatch.setattr(subprocess, "check_output", broken_version)

    assert identify(executable) == CondaExecutable(executable, "micromamba", None)
    assert log_messages() == ""


def test_identify_rejects_missing_and_unexecutable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_version_outputs(monkeypatch, {"conda": "conda 26.5.2\n"})

    with pytest.raises(FileNotFoundError):
        identify(tmp_path / "missing" / "conda")
    if sys.platform != "win32":  # Windows has no execute permission.
        conda = executable_file(tmp_path / "bin" / "conda")
        conda.chmod(0o644)
        with pytest.raises(PermissionError):
            identify(conda)
    assert calls == []


@pytest.mark.usefixtures("only_micromamba")
def test_cli_json(monkeypatch: pytest.MonkeyPatch) -> None:
    info = CondaExecutable(MICROMAMBA, "micromamba", Version("2.9.0"))
    calls = fake_version_outputs(monkeypatch, {"micromamba": "2.9.0\n"})

    result = CliRunner().invoke(cli.ensureconda_cli, ["--json", "--no-install"])

    assert result.exit_code == 0
    # Before click 8.2, the runner's stdout also includes stderr.
    assert json.loads(result.stdout.splitlines()[-1]) == info.to_json()
    assert calls == ["micromamba"]
