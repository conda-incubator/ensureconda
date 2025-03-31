import os
import pathlib
import shutil
import subprocess
import sys

import docker
import pytest


@pytest.fixture()
def golang_exe():
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


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param([], id="default"),
        pytest.param(["--no-micromamba"], id="no-micromamba"),
    ],
)
def test_install(golang_exe, flags):
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    args = [golang_exe, "--verbosity=3"]
    args.extend(flags)
    print(args)
    result = subprocess.check_output(args, encoding="utf8")
    subprocess.check_call([result, "--help"])


def test_find():
    pass


@pytest.fixture(scope="session")
def in_github_actions():
    return os.environ.get("GITHUB_WORKFLOW") is not None


@pytest.fixture(scope="session")
def platform():
    return sys.platform.lower()


@pytest.fixture(scope="session")
def can_i_docker(in_github_actions, platform):
    """Some platforms can't docker under certain conditions"""
    if in_github_actions and platform in {"win32", "darwin"}:
        return False
    else:
        return True


@pytest.fixture(scope="session")
def docker_client(can_i_docker) -> docker.client.DockerClient:
    if can_i_docker:
        return docker.from_env()


@pytest.fixture(scope="session")
def ensureconda_go_container(can_i_docker, docker_client: docker.client.DockerClient):
    if can_i_docker:
        test_root = pathlib.Path(__file__).parent
        src_root = test_root.parent
        proj_root = src_root.parent
        image, logs = docker_client.images.build(
            path=str(proj_root),
            tag="ensureconda:test-go",
            dockerfile=str(
                (proj_root / "test" / "docker-go" / "Dockerfile").relative_to(proj_root)
            ),
        )
        return image


@pytest.mark.parametrize(
    "environment, expected_status",
    [
        ({}, 0),
        ({"ENSURECONDA_CONDA_STANDALONE_CHANNEL": "non-existent-channel"}, 2),
        ({"ENSURECONDA_CONDA_STANDALONE_CHANNEL": "anaconda"}, 0),
        ({"ENSURECONDA_CONDA_STANDALONE_CHANNEL": "conda-forge"}, 0),
    ],
    ids=["no-environment-var", "non-existent-channel", "anaconda", "conda-forge"],
)
def test_non_existent_channel(
    can_i_docker,
    docker_client: docker.client.DockerClient,
    ensureconda_go_container,
    environment,
    expected_status,
):
    if not can_i_docker:
        raise pytest.skip("Docker not available")

    container_inst: docker.models.containers.Container = docker_client.containers.run(
        ensureconda_go_container,
        detach=True,
        command=["ensureconda", "--conda-exe", "--no-micromamba"],
        environment=environment,
    )
    try:
        res = container_inst.wait()
        stdout = container_inst.logs(stdout=True, stderr=False).decode().strip()
        stderr = container_inst.logs(stdout=False, stderr=True).decode().strip()
        print(f"container stdout:\n{stdout}", file=sys.stdout)
        print(f"container stderr:\n{stderr}", file=sys.stderr)
        assert res["StatusCode"] == expected_status
    finally:
        container_inst.remove()
