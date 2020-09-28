import sys
import pathlib
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

