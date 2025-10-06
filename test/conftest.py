import os
import sys
from typing import Optional

import docker
import pytest


@pytest.fixture(scope="session")
def in_github_actions() -> bool:
    return os.environ.get("GITHUB_WORKFLOW") is not None


@pytest.fixture(scope="session")
def platform() -> str:
    return sys.platform.lower()


@pytest.fixture(scope="session")
def can_i_docker(in_github_actions: bool, platform: str) -> bool:
    """Some platforms can't docker under certain conditions"""
    if in_github_actions and platform in {"win32", "darwin"}:
        return False
    else:
        return True


@pytest.fixture(scope="session")
def docker_client(can_i_docker: bool) -> Optional[docker.client.DockerClient]:
    if can_i_docker:
        return docker.from_env()
    return None
