import concurrent.futures
import os
import pathlib
import subprocess
import sys
import time
from typing import List

import docker
import docker.models.containers
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


@pytest.fixture(scope="session")
def ensureconda_container(can_i_docker, docker_client: docker.client.DockerClient):
    if can_i_docker:
        test_root = pathlib.Path(__file__).parent
        root = test_root.parent
        image, logs = docker_client.images.build(
            path=str(root),
            tag="ensureconda:test",
            dockerfile=str(
                (test_root / "docker-simple" / "Dockerfile").relative_to(root)
            ),
        )
        return image


@pytest.fixture(scope="session")
def ensureconda_container_full(can_i_docker, docker_client: docker.client.DockerClient):
    if can_i_docker:
        test_root = pathlib.Path(__file__).parent
        root = test_root.parent
        image, logs = docker_client.images.build(
            path=str(pathlib.Path(__file__).parent.parent),
            tag="ensureconda:test-full",
            dockerfile=str(
                (test_root / "docker-full" / "Dockerfile").relative_to(root)
            ),
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
        pytest.param(
            ["--min-mamba-version", "100.0.0"],
            "/root/.local/share/ensure-conda/conda_standalone",
        ),
        pytest.param(
            ["--min-conda-version", "100.0.0", "--min-mamba-version", "100.0.0"],
            "",
            marks=[pytest.mark.xfail],
        ),
    ],
)
def test_ensure_simple(
    args: List[str],
    expected: str,
    can_i_docker,
    docker_client: docker.client.DockerClient,
    ensureconda_container,
):
    if not can_i_docker:
        raise pytest.skip("Docker not available")

    _run_container_test(args, docker_client, expected, ensureconda_container)


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
    ensureconda_container_full,
    can_i_docker,
):
    if not can_i_docker:
        raise pytest.skip("Docker not available")

    _run_container_test(args, docker_client, expected, ensureconda_container_full)


def test_locally_install(tmp_path, monkeypatch):
    # The order of these tests matter
    import appdirs

    def user_data_dir(*args, **kwargs):
        return str(tmp_path)

    monkeypatch.setattr(appdirs, "user_data_dir", user_data_dir)

    from ensureconda.api import ensureconda
    from ensureconda.resolve import is_windows

    monkeypatch.delenv("CONDA_EXE", raising=False)
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
    assert str(executable) == f"{str(tmp_path)}{os.path.sep}conda_standalone{ext}"
    subprocess.check_call([str(executable), "--help"])

    # Ensure that we can install micromamba in the desired directory
    executable = ensureconda(
        mamba=False, micromamba=True, conda=False, conda_exe=False, no_install=False
    )
    ext = ".exe" if is_windows else ""
    assert str(executable) == f"{str(tmp_path)}{os.path.sep}micromamba{ext}"
    subprocess.check_call([str(executable), "--help"])


def test_concurrent_access(tmp_path, monkeypatch):
    """Test that multiple concurrent threads can safely call ensureconda and use the result.

    View output with:

    ```bash
    pytest -s test/test_library.py::test_concurrent_access
    ```
    """
    import appdirs

    def user_data_dir(*args, **kwargs):
        return str(tmp_path)

    monkeypatch.setattr(appdirs, "user_data_dir", user_data_dir)

    from ensureconda.api import ensureconda
    from ensureconda.resolve import is_windows

    # Clobber environment to ensure no conda executables are found
    monkeypatch.delenv("CONDA_EXE", raising=False)
    conda_executable = ensureconda(no_install=True)
    remaining_attempts = 100
    while conda_executable is not None and remaining_attempts >= 0:
        remaining_attempts -= 1
        paths = os.environ["PATH"].split(os.pathsep)
        dirname = str(os.path.dirname(conda_executable))
        print(f"Removing {dirname} from PATH")

        # Need to handle the case where the directory might not be in PATH
        if dirname in paths:
            paths.remove(dirname)

        monkeypatch.setenv("PATH", os.pathsep.join(paths))
        conda_executable = ensureconda(no_install=True)
    if remaining_attempts < 0:
        raise RuntimeError("Failed to clear all conda executables from PATH")

    print("Cleared all conda executables from PATH")

    # Function to be executed by each thread
    def worker(thread_num):
        """Worker function that gets and uses a conda executable."""
        print(f"Thread {thread_num}: Starting")

        # Get the conda executable, only allowing conda_standalone (not mamba/micromamba)
        executable = ensureconda(mamba=False, micromamba=False)
        print(f"Thread {thread_num}: Got executable: {executable}")

        # Use the executable to run a simple conda command
        print(f"Thread {thread_num}: Running conda search command")
        result = subprocess.run(
            [
                str(executable),
                "search",
                "--override-channels",
                "--channel=maresb",
                "mamba",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        # Verify that the command executed successfully
        assert result.returncode == 0
        print(f"Thread {thread_num}: Successfully completed conda command")
        return executable

    # Use a thread pool to run 3 concurrent threads
    start_time = time.time()
    executables = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        # Start all threads with their thread numbers
        futures = [executor.submit(worker, i) for i in range(3)]

        # Wait for all to complete and collect results
        for future in concurrent.futures.as_completed(futures):
            executables.append(future.result())

    end_time = time.time()

    # All threads should have used the same executable
    assert len(set(str(exe) for exe in executables)) == 1

    # Verify we have the expected executable path
    ext = ".exe" if is_windows else ""
    assert str(executables[0]).endswith(f"conda_standalone{ext}")

    print(f"Concurrent execution completed in {end_time - start_time:.2f} seconds")
