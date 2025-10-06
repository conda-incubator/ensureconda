import concurrent.futures
import os
import pathlib
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import docker
import docker.models.containers
import docker.models.images
import pytest

if TYPE_CHECKING:
    from _typeshed import StrPath


@pytest.fixture(scope="session")
def ensureconda_python_container(
    can_i_docker: bool, docker_client: Optional[docker.client.DockerClient]
) -> Optional[docker.models.images.Image]:
    if can_i_docker and docker_client is not None:
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
    return None


@pytest.fixture(scope="session")
def ensureconda_python_container_full(
    can_i_docker: bool, docker_client: Optional[docker.client.DockerClient]
) -> Optional[docker.models.images.Image]:
    if can_i_docker and docker_client is not None:
        test_root = pathlib.Path(__file__).parent
        src_root = test_root.parent
        image, logs = docker_client.images.build(
            path=str(pathlib.Path(__file__).parent.parent),
            tag="ensureconda:test-full",
            dockerfile=str(
                (test_root / "docker-full" / "Dockerfile").relative_to(src_root)
            ),
        )
        return image
    return None


def _run_container_test(
    args: List[str],
    docker_client: docker.client.DockerClient,
    container: docker.models.images.Image,
    expected_stdout: Optional[str] = None,
) -> None:
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
        if expected_stdout is not None:
            assert stdout == expected_stdout
    finally:
        container_inst.remove()


@pytest.mark.parametrize(
    "args, expected_stdout",
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
    expected_stdout: str,
    can_i_docker: bool,
    docker_client: docker.client.DockerClient,
    ensureconda_python_container: docker.models.images.Image,
) -> None:
    if not can_i_docker:
        raise pytest.skip("Docker not available")

    _run_container_test(
        args=args,
        docker_client=docker_client,
        container=ensureconda_python_container,
        expected_stdout=expected_stdout,
    )


@pytest.mark.parametrize(
    "args, expected_stdout",
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
    expected_stdout: str,
    docker_client: docker.client.DockerClient,
    ensureconda_python_container_full: docker.models.images.Image,
    can_i_docker: bool,
) -> None:
    if not can_i_docker:
        raise pytest.skip("Docker not available")

    _run_container_test(
        args=args,
        docker_client=docker_client,
        container=ensureconda_python_container_full,
        expected_stdout=expected_stdout,
    )


def test_locally_install(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The order of these tests matter
    import appdirs

    def user_data_dir(*args: Any, **kwargs: Any) -> str:
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


def test_concurrent_access(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that multiple concurrent threads can safely call ensureconda and use the result.

    View output with:

    ```bash
    pytest -s test/test_library.py::test_concurrent_access
    ```
    """
    import appdirs

    def user_data_dir(*args: Any, **kwargs: Any) -> str:
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
    def worker(thread_num: int) -> "StrPath":
        """Worker function that gets and uses a conda executable."""
        print(f"Thread {thread_num}: Starting")

        # Get the conda executable, only allowing conda_standalone (not mamba/micromamba)
        executable: Optional["StrPath"] = ensureconda(mamba=False, micromamba=False)
        assert executable is not None
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
