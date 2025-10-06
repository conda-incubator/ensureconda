import os
import pathlib
import subprocess
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
    return None


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param([], id="default"),
        pytest.param(["--no-micromamba"], id="no-micromamba"),
    ],
)
def test_install(golang_exe: Optional[str], flags: List[str]) -> None:
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    args = [golang_exe, "--verbosity=3"]
    args.extend(flags)
    print(args)
    result = subprocess.check_output(args, encoding="utf8")
    subprocess.check_call([result, "--help"])


def test_find() -> None:
    pass


@pytest.fixture(scope="session")
def ensureconda_go_container(
    can_i_docker: bool, docker_client: Optional[docker.client.DockerClient]
) -> Optional[docker.models.images.Image]:
    if can_i_docker and docker_client is not None:
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
    docker_client: docker.client.DockerClient,
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
