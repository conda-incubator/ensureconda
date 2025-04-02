import contextlib
import io
import math
import os
import stat
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import IO, Iterator, Optional, Union

import filelock
import requests
from packaging.version import Version

from ensureconda.resolve import is_windows, platform_subdir, site_path

# If the conda or micromamba executable is older than this, it will be redownloaded
REDOWNLOAD_WHEN_OLDER_THAN_SEC = 60 * 60 * 24  # 1 day
# If the age is below this, then the age is considered invalid and the executable will be redownloaded
NEGATIVE_AGE_TOLERANCE_SEC = -60
# Interval to notify users about waiting for lock and timeout for lock acquisition
LOCK_NOTIFICATION_INTERVAL_SEC = 5


def request_url_with_retry(url: str) -> requests.Response:
    n = 10
    for i in range(n):
        try:
            resp = requests.get(url, allow_redirects=True)
        except requests.exceptions.RequestException as e:
            timeout = max(math.e ** (i / 4), 15)
            print(
                f"Failed to retrieve due to {e!r}, retrying in {timeout:.2f} seconds",
                file=sys.stderr,
            )
            time.sleep(timeout)
            continue
        try:
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            if e.response.status_code in [500, 503]:
                timeout = max(math.e ** (i / 4), 15)
                print(
                    f"Failed to retrieve due to {e!r}, "
                    f"retrying in {timeout:.2f} seconds",
                    file=sys.stderr,
                )
                time.sleep(timeout)
            else:
                raise

    raise TimeoutError(f"Could not retrieve {url} in {n} tries")


def extract_files_from_conda_package(
    tarball: io.BytesIO, filename: str, dest_filename: str
) -> Path:
    tarball.seek(0)
    with tarfile.open(mode="r:bz2", fileobj=tarball) as tf:
        fo = tf.extractfile(filename)
        if fo is None:
            raise RuntimeError("Could not extract executable!")
        site_path().mkdir(parents=True, exist_ok=True)
        target_path = site_path() / dest_filename
        with new_executable(target_path) as exe_fo:
            exe_fo.write(fo.read())
        return target_path


@contextlib.contextmanager
def lock_with_feedback(lock_path: Union[str, Path], lock_name: str) -> Iterator[None]:
    """Context manager to acquire a lock with user feedback when we need to wait.

    Args:
        lock_path: The path to the lock file
        lock_name: A descriptive name for the lock to show in waiting messages
    """
    lock = filelock.FileLock(str(lock_path))
    lock_start = time.time()

    while True:
        try:
            # Try to acquire with timeout matching notification interval
            lock.acquire(timeout=LOCK_NOTIFICATION_INTERVAL_SEC)
            try:
                # Success! Now report total wait time if we had to wait
                total_wait_time = time.time() - lock_start
                if total_wait_time > 0.1:
                    print(
                        f"Lock for {lock_name} acquired after waiting {total_wait_time:.1f} seconds",
                        file=sys.stderr,
                    )
                # Yield control back to the caller
                yield
            finally:
                # Always release the lock
                lock.release()
            # Exit the loop after successful execution
            break
        except filelock.Timeout:
            total_wait_time = time.time() - lock_start
            print(
                f"Waiting for lock for {lock_name} to be released by another process... "
                f"({total_wait_time:.1f}s)",
                file=sys.stderr,
            )


def install_conda_exe() -> Optional[Path]:
    # Create a lock file specific to conda_exe to prevent concurrent downloads
    lock_path = site_path() / "conda_exe_install.lock"
    dest_filename = f"conda_standalone{exe_suffix()}"
    site_path().mkdir(parents=True, exist_ok=True)

    # Use the context manager for lock acquisition with feedback
    with lock_with_feedback(lock_path, "downloading conda"):
        # Check if the file already exists after acquiring the lock
        target_path = site_path() / dest_filename
        if target_path.exists():
            # Check if the file is fresh
            file_age = time.time() - target_path.stat().st_mtime
            if (
                file_age < REDOWNLOAD_WHEN_OLDER_THAN_SEC
                and file_age > NEGATIVE_AGE_TOLERANCE_SEC
            ):
                return target_path
            # File is too old or has a weird timestamp, continue to download a new version

        url = "https://api.anaconda.org/package/anaconda/conda-standalone/files"
        resp = request_url_with_retry(url)
        subdir = platform_subdir()

        candidates = []
        for file_info in resp.json():
            if file_info["attrs"]["subdir"] == subdir:
                candidates.append(file_info["attrs"])

        candidates.sort(
            key=lambda attrs: (
                Version(attrs["version"]),
                attrs["build_number"],
                attrs["timestamp"],
            )
        )

        chosen = candidates[-1]
        resp = request_url_with_retry(chosen["source_url"])

        tarball = io.BytesIO(resp.content)

        return extract_files_from_conda_package(
            tarball=tarball,
            filename="standalone_conda/conda.exe",
            dest_filename=dest_filename,
        )


def install_micromamba() -> Optional[Path]:
    """Install micromamba into the installation"""
    # Create a lock file specific to micromamba to prevent concurrent downloads
    lock_path = site_path() / "micromamba_install.lock"
    dest_filename = f"micromamba{exe_suffix()}"
    site_path().mkdir(parents=True, exist_ok=True)

    # Use the context manager for lock acquisition with feedback
    with lock_with_feedback(lock_path, "downloading micromamba"):
        # Check if the file already exists after acquiring the lock
        target_path = site_path() / dest_filename
        if target_path.exists():
            # Check if the file is newer than 1 day
            file_age = time.time() - target_path.stat().st_mtime
            # Skip if file is not too old and not too far in the future
            if (
                file_age < REDOWNLOAD_WHEN_OLDER_THAN_SEC
                and file_age > NEGATIVE_AGE_TOLERANCE_SEC
            ):
                return target_path
            # File is too old or has a weird timestamp, continue to download a new version

        subdir = platform_subdir()
        url = f"https://micromamba.snakepit.net/api/micromamba/{subdir}/latest"
        resp = request_url_with_retry(url)

        tarball = io.BytesIO(resp.content)
        if is_windows:
            filename = "Library/bin/micromamba.exe"
        else:
            filename = "bin/micromamba"

        return extract_files_from_conda_package(
            tarball=tarball, filename=filename, dest_filename=dest_filename
        )


def exe_suffix() -> str:
    return ".exe" if is_windows else ""


@contextlib.contextmanager
def new_executable(target_filename: "Path") -> Iterator[IO[bytes]]:
    """Create a new executable that can be written to.

    Care is take to both prevent concurrent writes as well as guarding against
    early reads.
    """
    lock_path = f"{str(target_filename)}.lock"
    with lock_with_feedback(lock_path, f"file write ({target_filename})"):
        temp_filename = target_filename.with_name(uuid.uuid4().hex)
        with open(temp_filename, "wb") as fo:
            yield fo
        st = os.stat(temp_filename)
        os.chmod(temp_filename, st.st_mode | stat.S_IXUSR)
        # On Windows, we need to handle the case where the target file already exists
        if sys.platform in ["win32", "cygwin", "msys"] and target_filename.exists():
            try:
                target_filename.unlink()
            except (PermissionError, OSError) as e:
                raise RuntimeError(
                    f"Could not remove existing executable {target_filename} "
                    f"for replacement"
                ) from e
        os.rename(temp_filename, target_filename)
