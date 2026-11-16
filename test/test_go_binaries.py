import json
import os
import pathlib
import subprocess
import sys
import time
from test.helpers import run_container_test
from typing import Dict, List, Optional

import docker.client
import docker.models.images
import pytest


@pytest.fixture()
def golang_exe() -> Optional[str]:
    exe = os.environ.get("ENSURECONDA_EXE")
    if exe is not None:
        if os.path.sep in exe:
            p = pathlib.Path(exe).resolve()
            if p.exists():
                return str(p)
        else:
            p = pathlib.Path(__file__).parent / exe
            if p.exists():
                return str(p)
            p = pathlib.Path(os.getcwd()) / exe
            if p.exists():
                return str(p)
        # Fail rather than skip, so a missing build can't pass CI unnoticed.
        pytest.fail(f"ENSURECONDA_EXE is set, but {exe} was not found")
    return None


@pytest.fixture()
def isolated_env(tmp_path: pathlib.Path) -> Dict[str, str]:
    """An environment that gives the binary a data directory of its own.

    Otherwise the tests install into the user's real data directory, and reuse
    whatever a previous run or test left there.

    Only on Linux does the binary take the directory from the environment. On
    macOS and Windows it asks the system, so there the tests still use the real
    one.
    """
    return {**os.environ, "XDG_DATA_HOME": str(tmp_path)}


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(["--no-mamba", "--no-conda", "--no-conda-exe"], id="micromamba"),
        pytest.param(
            ["--no-mamba", "--no-micromamba", "--no-conda"], id="conda-standalone"
        ),
    ],
)
@pytest.mark.parametrize(
    "replace_rejected", [False, True], ids=["missing-target", "rejected-target"]
)
def test_install_and_force_refresh(
    golang_exe: Optional[str],
    isolated_env: Dict[str, str],
    tmp_path: pathlib.Path,
    platform: str,
    flags: List[str],
    replace_rejected: bool,
) -> None:
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    if replace_rejected and platform != "linux":
        pytest.skip("only Linux supports isolating the Go data directory via XDG")
    if platform == "linux":
        data = tmp_path / "ensure-conda"
        data.mkdir()
        standalone = "--no-micromamba" in flags
        # Model a developer's populated cache: another usable flavor must not
        # satisfy this test instead of installing the requested one.
        other = data / ("mamba" if standalone else "conda_standalone")
        output = "mamba 1.5.12" if standalone else "conda 25.1.0"
        other.write_text(f"#!/bin/sh\necho {output}\n")
        other.chmod(0o755)
        if replace_rejected:
            executable = data / ("conda_standalone" if standalone else "micromamba")
            prefix = "conda " if standalone else ""
            # A new file must still be replaced when its version is too old.
            executable.write_text(f"#!/bin/sh\necho {prefix}0.1.0\n")
            executable.chmod(0o755)
    args = [golang_exe, "--verbosity=3", "--json"]
    args.extend(flags)
    print(args)
    # Both implementations must use the install directory, even on hosts with
    # conda already installed or CONDA_EXE set by an activated environment.
    env = {**isolated_env, "PATH": "", "CONDA_EXE": ""}
    installed = json.loads(subprocess.check_output(args, encoding="utf8", env=env))
    assert installed["flavor"] == (
        "conda_standalone" if "--no-micromamba" in flags else "micromamba"
    )
    assert installed["version"] != "0.1.0"
    if platform == "linux":
        assert pathlib.Path(installed["path"]).parent == tmp_path / "ensure-conda"
    subprocess.check_call([installed["path"], "--help"])
    found = json.loads(
        subprocess.check_output([*args, "--no-install"], encoding="utf8", env=env)
    )
    # Windows discovery spells the extension as in PATHEXT, whereas a fresh
    # installation uses .exe. Both must describe the same executable.
    assert os.path.normcase(installed["path"]) == os.path.normcase(found["path"])
    assert installed["flavor"] == found["flavor"]
    assert installed["version"] == found["version"]
    python_result = json.loads(
        subprocess.check_output(
            [sys.executable, "-m", "ensureconda", "--json", "--no-install", *flags],
            encoding="utf8",
            env=env,
        )
    )
    assert found == python_result

    # macOS and Windows use the real user data directory; do not force a
    # replacement or change timestamps there.
    if platform != "linux":
        return

    # Distinguish the replacement even on filesystems with coarse timestamps.
    cached = pathlib.Path(installed["path"])
    fresh_timestamp = time.time() - 60
    os.utime(cached, (fresh_timestamp, fresh_timestamp))
    cached_mtime = cached.stat().st_mtime_ns
    refreshed = json.loads(
        subprocess.check_output([*args, "--force-install"], encoding="utf8", env=env)
    )
    assert os.path.normcase(refreshed["path"]) == os.path.normcase(installed["path"])
    assert refreshed["flavor"] == installed["flavor"]
    assert cached.stat().st_mtime_ns > cached_mtime
    subprocess.check_call([refreshed["path"], "--help"])
    assert refreshed == json.loads(
        subprocess.check_output(
            [sys.executable, "-m", "ensureconda", "--json", "--no-install", *flags],
            encoding="utf8",
            env=env,
        )
    )


@pytest.mark.parametrize("flavor", ["micromamba", "conda_standalone"])
@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_default_flags(
    golang_exe: Optional[str],
    isolated_env: Dict[str, str],
    tmp_path: pathlib.Path,
    platform: str,
    flavor: str,
    as_json: bool,
) -> None:
    if golang_exe is None:
        pytest.skip("environment variable not set")
    if platform != "linux":
        pytest.skip("only Linux supports isolating the Go data directory via XDG")
    data = tmp_path / "ensure-conda"
    data.mkdir()
    executable = data / flavor
    version = "2.9.0" if flavor == "micromamba" else "25.1.0"
    prefix = "" if flavor == "micromamba" else "conda "
    executable.write_text(f"#!/bin/sh\necho {prefix}{version}\n")
    executable.chmod(0o755)
    # Exercise the default search and install flags without a live download.
    result = subprocess.check_output(
        [golang_exe, *(["--json"] if as_json else [])],
        env={**isolated_env, "PATH": "", "CONDA_EXE": ""},
        encoding="utf8",
    )
    if as_json:
        assert json.loads(result) == {
            "path": str(executable),
            "flavor": flavor,
            "version": version,
        }
    else:
        assert result == str(executable)


@pytest.mark.parametrize(
    "flavor, output, version",
    [
        ("mamba", "mamba 1.5.12\nconda 24.11.3\n", "1.5.12"),
        ("mamba", "2.9.0\n", "2.9.0"),
        ("micromamba", "2.9.0\n", "2.9.0"),
        ("conda", "conda 25.1.0\n", "25.1.0"),
        ("conda_standalone", "conda 25.1.0\n", "25.1.0"),
        ("micromamba", "v02.009.00\n", "2.9.0"),
        ("micromamba", "01!02.0RC01.post02.dev03\n", "1!2.0rc1.post2.dev3"),
        ("micromamba", "2.9alpha\n", "2.9a0"),
        ("micromamba", "2.9PREVIEW02\n", "2.9rc2"),
        ("micromamba", "2.9-rev\n", "2.9.post0"),
        ("micromamba", "2.9-03\n", "2.9.post3"),
        ("micromamba", "2.9DEV\n", "2.9.dev0"),
        ("micromamba", "2.9+Ubuntu_007-000.01abc\n", "2.9+ubuntu.7.0.01abc"),
        ("mamba", "mamba 0.5.0\nconda 4.9.0\n", None),
        ("mamba", "mamba unknown\nconda 25.1.0\n", None),
        ("mamba", "conda 25.1.0\nmamba 1.5.12\n", "1.5.12"),
        ("micromamba", "0.1.0\n2.9.0\n", None),
        ("micromamba", "warning: something\n2.9.0\n", None),
        ("conda", "conda 4.0.0\nconda 25.1.0\n", None),
        ("conda", "conda unknown\nconda 25.1.0\n", None),
        ("micromamba", "2.9.0 \n", "2.9.0"),
        ("micromamba", "micromamba\t2.9.0\n", "2.9.0"),
        ("conda", "\n \tconda 25.1.0\t \r\n", "25.1.0"),
        ("conda", "warning\rconda 25.1.0\r", "25.1.0"),
        ("conda", "\u2003conda\u00a025.1.0\u2003", "25.1.0"),
        ("conda", "warning\u2028conda 25.1.0", "25.1.0"),
        ("conda", "\x1fconda\x1f25.1.0\x1f", "25.1.0"),
        ("micromamba", "\n \t", None),
    ],
)
@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_output_matches_python(
    golang_exe: Optional[str],
    isolated_env: Dict[str, str],
    tmp_path: pathlib.Path,
    platform: str,
    flavor: str,
    output: str,
    version: Optional[str],
    as_json: bool,
) -> None:
    if golang_exe is None:
        pytest.skip("environment variable not set")
    if platform != "linux":
        pytest.skip("only Linux supports isolating the Go data directory via XDG")
    data = tmp_path / "ensure-conda"
    data.mkdir()
    executable = data / flavor
    executable.write_text('#!/bin/sh\nprintf "%s" "$TEST_VERSION_OUTPUT"\n')
    executable.chmod(0o755)
    flags = ["--no-install"]
    if as_json:
        flags.append("--json")
    for name in ["mamba", "micromamba", "conda", "conda_standalone"]:
        if name != flavor:
            flags.append(
                "--no-" + ("conda-exe" if name == "conda_standalone" else name)
            )
    env = {**isolated_env, "PATH": "", "CONDA_EXE": "", "TEST_VERSION_OUTPUT": output}
    go_result = subprocess.run(
        [golang_exe, *flags], capture_output=True, encoding="utf8", env=env
    )
    python_result = subprocess.run(
        [sys.executable, "-m", "ensureconda", *flags],
        capture_output=True,
        encoding="utf8",
        env=env,
    )
    assert (
        go_result.returncode
        == python_result.returncode
        == (1 if version is None else 0)
    )
    if version is None:
        assert go_result.stdout == python_result.stdout == ""
        return
    if not as_json:
        assert go_result.stdout == str(executable)
        assert python_result.stdout == str(executable) + "\n"
        return
    assert (
        json.loads(go_result.stdout)
        == json.loads(python_result.stdout)
        == {
            "path": str(executable),
            "flavor": flavor,
            "version": version,
        }
    )


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize(
    "dirname",
    [b"caf\xc3\xa9", b"\xf0\x9d\x84\x9e", b"lat\xe9"],
    ids=["utf8", "non-bmp", "invalid-utf8"],
)
def test_non_ascii_path(
    golang_exe: Optional[str],
    isolated_env: Dict[str, str],
    tmp_path: pathlib.Path,
    platform: str,
    as_json: bool,
    dirname: bytes,
) -> None:
    if golang_exe is None:
        pytest.skip("environment variable not set")
    if platform != "linux":
        pytest.skip("only Linux supports isolating the Go data directory via XDG")
    data_home = tmp_path / os.fsdecode(dirname)
    data = data_home / "ensure-conda"
    data.mkdir(parents=True)
    executable = data / "micromamba"
    executable.write_text("#!/bin/sh\necho 2.9.0\n")
    executable.chmod(0o755)
    flags = ["--no-install", "--no-mamba", "--no-conda", "--no-conda-exe"]
    if as_json:
        flags.append("--json")
    result = subprocess.run(
        [golang_exe, *flags],
        capture_output=True,
        env={**isolated_env, "PATH": "", "XDG_DATA_HOME": str(data_home)},
    )
    if as_json and dirname == b"lat\xe9":
        assert result.returncode == 1
        assert result.stdout == b""
        assert b"executable path is not valid UTF-8" in result.stderr
    else:
        assert result.returncode == 0
        if as_json:
            assert result.stdout.isascii()
            assert json.loads(result.stdout)["path"] == str(executable)
        else:
            assert result.stdout == os.fsencode(executable)


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(["--no-conda-exe"], id="micromamba"),
        pytest.param(["--no-micromamba"], id="conda-standalone"),
    ],
)
def test_find_installed_executable(
    golang_exe: Optional[str],
    isolated_env: Dict[str, str],
    tmp_path: pathlib.Path,
    platform: str,
    flags: List[str],
) -> None:
    """Both a fresh install and its discovery must print just a usable path."""
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    flags = ["--no-mamba", "--no-conda", *flags]
    # Hide PATH so that both runs must use ensureconda's data directory.
    env = {**isolated_env, "PATH": ""}
    installed = subprocess.check_output([golang_exe, *flags], encoding="utf8", env=env)
    assert pathlib.Path(installed).is_file()
    found = subprocess.check_output(
        [golang_exe, "--no-install", *flags], encoding="utf8", env=env
    )
    # On Windows, a found executable's extension is spelled as in PATHEXT
    # (".EXE"), while a fresh install reports ".exe".
    assert os.path.normcase(found) == os.path.normcase(installed)
    if platform == "linux":
        assert pathlib.Path(installed).parent == tmp_path / "ensure-conda"


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_reports_nothing_found(
    golang_exe: Optional[str], isolated_env: Dict[str, str], as_json: bool
) -> None:
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    # Outside Linux the data directory isn't isolated and may hold micromamba
    # or conda-standalone from an earlier test, so leave both out.
    flags = ["--no-micromamba", "--no-conda-exe", "--no-install"]
    if as_json:
        flags.append("--json")
    result = subprocess.run(
        [golang_exe, *flags],
        env={**isolated_env, "PATH": ""},
        capture_output=True,
        encoding="utf8",
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Could not find compatible executable" in result.stderr


@pytest.fixture(scope="session")
def ensureconda_go_container(
    can_i_docker: bool, docker_client: Optional[docker.client.DockerClient]
) -> Optional[docker.models.images.Image]:
    if can_i_docker and docker_client is not None:
        test_root = pathlib.Path(__file__).parent
        proj_root = test_root.parent
        image, logs = docker_client.images.build(
            path=str(proj_root),
            tag="ensureconda:test-go",
            dockerfile=str(
                (proj_root / "test" / "docker-go" / "Dockerfile").relative_to(proj_root)
            ),
            # Unlike the docker CLI, docker-py keeps a stopped container for
            # each build step unless told otherwise.
            rm=True,
            forcerm=True,
        )
        return image
    return None


@pytest.mark.parametrize(
    "environment, expected_status",
    [
        ({}, 0),
        ({"ENSURECONDA_CONDA_STANDALONE_CHANNEL": "non-existent-channel"}, 1),
        ({"ENSURECONDA_CONDA_STANDALONE_CHANNEL": "anaconda"}, 0),
        ({"ENSURECONDA_CONDA_STANDALONE_CHANNEL": "conda-forge"}, 0),
    ],
    ids=["no-environment-var", "non-existent-channel", "anaconda", "conda-forge"],
)
def test_non_existent_channel(
    can_i_docker: bool,
    docker_client: Optional[docker.client.DockerClient],
    ensureconda_go_container: docker.models.images.Image,
    environment: Dict[str, str],
    expected_status: int,
) -> None:
    if not can_i_docker or docker_client is None:
        raise pytest.skip("Docker not available")

    run_container_test(
        docker_client=docker_client,
        container=ensureconda_go_container,
        args=["--conda-exe", "--no-micromamba"],
        envvars=environment,
        expected_status=expected_status,
    )
