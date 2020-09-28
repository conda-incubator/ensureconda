import os
import pathlib
import subprocess
import sys
from typing import List

import docker
import docker.models.containers
import pytest
from _pytest import mark


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def test_container(docker_client: docker.client.DockerClient):
    test_root = pathlib.Path(__file__).parent
    root = test_root.parent
    image, logs = docker_client.images.build(
        path=str(root),
        tag="ensureconda:test",
        dockerfile=str((test_root / "docker-simple" / "Dockerfile").relative_to(root)),
    )
    return image


@pytest.fixture(scope="session")
def test_container_full(docker_client: docker.client.DockerClient):
    test_root = pathlib.Path(__file__).parent
    root = test_root.parent
    image, logs = docker_client.images.build(
        path=str(pathlib.Path(__file__).parent.parent),
        tag="ensureconda:test-full",
        dockerfile=str((test_root / "docker-full" / "Dockerfile").relative_to(root)),
    )
    return image


def _run_container_test(args, docker_client, expected, container):
    container_inst: docker.models.containers.Container = docker_client.containers.run(
        container, detach=True, command=["ensureconda", *args]
    )
    try:
        res = container_inst.wait()
        stdout = container_inst.logs(stdout=True, stderr=False).decode().strip()
        stderr = container_inst.logs(stdout=False, stderr=True).decode().strip()
        print(f"container stdout:\n{stdout}", file=sys.stdout)
        print(f"container stderr:\n{stderr}", file=sys.stderr)
        assert res["StatusCode"] == 0
        assert stdout == expected
    finally:
        container_inst.remove()


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], "/root/.local/share/ensure-conda/micromamba"),
        (["--no-micromamba"], "/root/.local/share/ensure-conda/conda_standalone"),
        (["--no-conda-exe"], "/root/.local/share/ensure-conda/micromamba"),
        pytest.param(
            ["--no-micromamba", "--no-conda-exe"], "", marks=[pytest.mark.xfail]
        ),
    ],
)
def test_ensure_simple(
    args: List[str],
    expected: str,
    docker_client: docker.client.DockerClient,
    test_container,
):
    _run_container_test(args, docker_client, expected, test_container)


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], "/opt/conda/bin/mamba"),
        (["--no-mamba", "--no-install"], "/opt/conda/bin/conda"),
        (["--no-mamba"], "/opt/conda/bin/conda"),
        (["--no-mamba", "--no-conda"], "/root/.local/share/ensure-conda/micromamba"),
        (
            ["--no-mamba", "--no-micromamba", "--no-conda"],
            "/root/.local/share/ensure-conda/conda_standalone",
        ),
    ],
)
def test_ensure_full(
    args: List[str],
    expected: str,
    docker_client: docker.client.DockerClient,
    test_container_full,
):
    _run_container_test(args, docker_client, expected, test_container_full)


def test_locally_install(tmp_path, monkeypatch):
    # The order of these tests matter
    import appdirs

    def user_data_dir(*args, **kwargs):
        return str(tmp_path)

    monkeypatch.setattr(appdirs, "user_data_dir", user_data_dir)

    from ensureconda.api import ensureconda
    from ensureconda.resolve import is_windows

    # remove all paths from $PATH until we don't have a conda executable
    conda_executable = ensureconda(no_install=True)
    while conda_executable is not None:
        paths = os.environ["PATH"].split(os.pathsep)
        dirname = str(os.path.dirname(conda_executable))
        print(dirname)
        print(paths)
        paths.remove(dirname)
        monkeypatch.setenv("PATH", os.pathsep.join(paths))
        conda_executable = ensureconda(no_install=True)

    # Ensure that we can install conda_standalone in the desired directory
    executable = ensureconda(
        mamba=False, micromamba=False, conda=False, conda_exe=True, no_install=False
    )
    ext = ".exe" if is_windows else ""
    assert str(executable) == f"{str(tmp_path)}/conda_standalone{ext}"
    subprocess.check_call([executable, "--help"])

    # Ensure that we can install micromamba in the desired directory
    executable = ensureconda(
        mamba=False, micromamba=True, conda=False, conda_exe=False, no_install=False
    )
    ext = ".exe" if is_windows else ""
    assert str(executable) == f"{str(tmp_path)}/micromamba{ext}"
    subprocess.check_call([executable, "--help"])
