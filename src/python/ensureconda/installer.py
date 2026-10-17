import contextlib
import io
import os
import shutil
import stat
import sys
import tarfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryFile
from typing import IO, Any, Dict, Iterator, List, NamedTuple, Optional, Union
from urllib.parse import urlparse

import filelock
import requests
from conda_package_streaming.package_streaming import stream_conda_component
from packaging.version import Version

from ensureconda.resolve import is_windows, platform_subdir, site_path

REDOWNLOAD_WHEN_OLDER_THAN_SEC = 60 * 60 * 24
"""Maximum executable age in seconds (one day) before redownloading."""

NEGATIVE_AGE_TOLERANCE_SEC = -60
"""Minimum valid executable age in seconds, allowing for clock skew."""

LOCK_NOTIFICATION_INTERVAL_SEC = 5
"""Seconds between lock acquisition attempts and waiting notifications."""

CONNECT_TIMEOUT = 10
"""Connection establishment timeout in seconds."""

READ_TIMEOUT = 60
"""Socket inactivity timeout in seconds; does not limit total download time."""

MAX_RETRIES = 6
"""Number of retries after the initial download attempt."""


class AnacondaPkgAttr(NamedTuple):
    """Inner for data from Anaconda API

    <https://api.anaconda.org/package/anaconda/conda-standalone/files>
    <https://api.anaconda.org/package/conda-forge/conda-standalone/files>
    """

    subdir: str
    build: str
    build_number: int
    timestamp: int


class AnacondaPkg(NamedTuple):
    """Outer model for data from Anaconda API"""

    size: int
    attrs: AnacondaPkgAttr
    type: str
    version: str
    download_url: str


def request_url_with_retry(
    url: str, *, destination: Optional[IO[bytes]] = None
) -> requests.Response:
    """Retrieve a URL, retrying transient failures during the entire download.

    By default, return a response with its body in memory. If destination is
    provided, stream the body there instead, replacing its contents on each
    attempt and rewinding it on success. The caller owns the destination;
    the returned response's body has already been consumed in this mode.
    """
    attempt = 0
    while True:
        attempt += 1
        if destination is not None:
            destination.seek(0)
            destination.truncate()
        try:
            with requests.get(
                url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            ) as response:
                response.raise_for_status()
                # urllib3 1.x otherwise accepts bodies shorter than Content-Length.
                response.raw.enforce_content_length = True
                if destination is None:
                    # Cache the body before returning, inside the retry loop.
                    _ = response.content
                else:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        destination.write(chunk)
                    destination.seek(0)
                return response
        except requests.exceptions.RequestException as error:
            retryable = isinstance(
                error,
                (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError,
                ),
            )
            if isinstance(error, requests.exceptions.HTTPError):
                retryable = (
                    error.response is not None
                    and error.response.status_code in (429, 500, 502, 503, 504)
                )

            delay = 2 ** (attempt - 1)
            retry = retryable and attempt <= MAX_RETRIES
            action = (
                f"retrying in {delay}s"
                if retry
                else ("retries exhausted" if retryable else "not retrying")
            )
            print(
                f"Request to {url} failed ({type(error).__name__}: {error}); "
                f"attempt {attempt}/{MAX_RETRIES + 1}; {action}",
                file=sys.stderr,
            )
            if not retry:
                raise
            time.sleep(delay)


def extract_files_from_tar_bz2(
    tarball: io.BytesIO, filename: str, dest_filename: str
) -> Path:
    tarball.seek(0)
    with tarfile.open(mode="r:bz2", fileobj=tarball) as tf:
        fo = tf.extractfile(filename)
        if fo is None:
            raise RuntimeError("Could not extract executable!")
        return write_executable_from_file_object(fo, dest_filename)


def write_executable_from_file_object(fo: IO[bytes], dest_filename: str) -> Path:
    site_path().mkdir(parents=True, exist_ok=True)
    target_path = site_path() / dest_filename
    with new_executable(target_path) as exe_fo:
        shutil.copyfileobj(fo, exe_fo, length=1024 * 1024)
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


def stream_conda_executable(url: str) -> Path:
    """Extract `conda.exe` from the conda-standalone package at the given URL.

    This is roughly a more modern version of `extract_files_from_tar_bz2`
    that also works with `.conda` archives.

    Download the archive before extracting it to avoid an HTTP range request
    for every small read made by the decompressor.
    """
    dest_filename = f"conda_standalone{exe_suffix()}"

    filename = urlparse(url).path.rsplit("/", 1)[-1]
    with TemporaryFile() as conda:
        request_url_with_retry(url, destination=conda)
        with closing(
            stream_conda_component(filename, conda, component="pkg")
        ) as stream:
            for tar, member in stream:
                if member.name == "standalone_conda/conda.exe":
                    fo = tar.extractfile(member)
                    if fo is None:
                        raise RuntimeError(
                            f"Could not extract executable {member.name} from {url}!"
                        )
                    conda_exe = write_executable_from_file_object(fo, dest_filename)
                    return conda_exe
        raise RuntimeError("Could not find conda.exe in the conda-standalone package")


def get_channel_name() -> str:
    channel = os.environ.get("ENSURECONDA_CONDA_STANDALONE_CHANNEL", "conda-forge")
    # Raise an error if the channel name contains any non-alphanumeric characters
    # besides - or _
    if not channel.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid channel name {channel}. Channel names must be alphanumeric"
            " and may contain hyphens and underscores."
        )
    return channel


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

        channel = get_channel_name()
        candidates = compute_candidates(channel, platform_subdir())
        chosen = candidates[-1]
        url = "https:" + chosen.download_url
        path_to_written_executable = stream_conda_executable(url)
        return path_to_written_executable


def compute_candidates(channel: str, subdir: str) -> List[AnacondaPkg]:
    """Compute the candidates for the conda-standalone package"""

    url = f"https://api.anaconda.org/package/{channel}/conda-standalone/files"
    resp = request_url_with_retry(url)
    api_response_data: List[Dict[str, Any]] = resp.json()

    candidates = []
    for file_info in api_response_data:
        info_attrs = AnacondaPkgAttr(
            subdir=file_info["attrs"]["subdir"],
            build=file_info["attrs"]["build"],
            build_number=file_info["attrs"]["build_number"],
            timestamp=file_info["attrs"]["timestamp"],
        )
        info = AnacondaPkg(
            size=file_info["size"],
            attrs=info_attrs,
            type=file_info["type"],
            version=file_info["version"],
            download_url=file_info["download_url"],
        )
        if (
            info.attrs.subdir == subdir
            and
            # Ignore onedir packages as workaround for
            # <https://github.com/conda/conda-standalone/issues/182>
            "_onedir_" not in info.attrs.build
        ):
            candidates.append(info)

    candidates.sort(
        key=lambda attrs: (
            Version(attrs.version),
            attrs.attrs.build_number,
            attrs.attrs.timestamp,
        )
    )
    if len(candidates) == 0:
        raise RuntimeError(f"No conda-standalone package found for {subdir}")
    return candidates


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
        url = f"https://micro.mamba.pm/api/micromamba/{subdir}/latest"
        resp = request_url_with_retry(url)

        tarball = io.BytesIO(resp.content)
        if is_windows:
            filename = "Library/bin/micromamba.exe"
        else:
            filename = "bin/micromamba"

    return extract_files_from_tar_bz2(
        tarball=tarball, filename=filename, dest_filename=f"micromamba{exe_suffix()}"
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
