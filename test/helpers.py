import sys
from typing import Dict, List, Optional

import docker.client
import docker.models.containers
import docker.models.images


def run_container_test(
    args: List[str],
    docker_client: docker.client.DockerClient,
    container: docker.models.images.Image,
    expected_stdout: Optional[str] = None,
    expected_status: Optional[int] = 0,
    envvars: Optional[Dict[str, str]] = None,
) -> None:
    if envvars is None:
        envvars = {}
    container_inst: docker.models.containers.Container = docker_client.containers.run(
        container, detach=True, command=["ensureconda", *args], environment=envvars
    )
    try:
        res = container_inst.wait()
        stdout = container_inst.logs(stdout=True, stderr=False).decode().strip()
        stderr = container_inst.logs(stdout=False, stderr=True).decode().strip()
        print(f"container stdout:\n{stdout}", file=sys.stdout)
        print(f"container stderr:\n{stderr}", file=sys.stderr)
        if expected_status is not None:
            assert res["StatusCode"] == expected_status
        if expected_stdout is not None:
            assert stdout == expected_stdout
    finally:
        container_inst.remove()
