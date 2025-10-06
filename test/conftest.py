import os
import sys

import docker
import pytest


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
def docker_client(can_i_docker):
    if can_i_docker:
        return docker.from_env()
