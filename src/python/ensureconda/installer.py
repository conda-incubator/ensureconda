import contextlib
import hashlib
import io
import os
import shutil
import stat
import sys
import tarfile
import time
import uuid
from contextlib import closing
from datetime import timezone
from email.utils import parsedate_to_datetime
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

CONNECT_TIMEOUT_SEC = 10
"""Connection establishment timeout in seconds."""

READ_TIMEOUT_SEC = 60
"""Socket inactivity timeout in seconds; does not limit total download time."""

RETRY_TIME_BUDGET_SEC = 70
"""Elapsed seconds allowed for scheduling retries; active downloads may finish."""


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
    sha256: Optional[str] = None


def _retry_after_delay(value: str) -> Optional[float]:
    """Parse Retry-After seconds or an HTTP date; ignore malformed values."""
    try:
        if value.strip().isdigit():
            return float(int(value))
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, retry_at.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


def request_url_with_retry(
    url: str, *, destination: Optional[IO[bytes]] = None
) -> requests.Response:
    """Retrieve a URL, retrying transient failures during the entire download.

    By default, return a response with its body in memory. If destination is
    provided, stream the body there instead, replacing its contents on each
    attempt and rewinding it on success. The caller owns the destination;
    the returned response's body has already been consumed in this mode.

    Retry-After on retryable HTTP responses can extend an individual wait.
    Schedule retries only within RETRY_TIME_BUDGET_SEC seconds of the initial
    attempt, including time spent in failed requests. A successful download
    may finish after this deadline. A failed download is retried only if its
    next scheduled attempt fits within the deadline.
    """
    deadline_sec = time.monotonic() + RETRY_TIME_BUDGET_SEC
    backoff_sec = 1.0
    attempt = 0
    while True:
        attempt += 1
        if destination is not None:
            destination.seek(0)
            destination.truncate()
        try:
            with requests.get(
                url, stream=True, timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC)
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

            failure = (
                f"Request to {url} failed ({type(error).__name__}: {error}); "
                f"attempt {attempt}"
            )
            if not retryable:
                print(f"{failure}; not retrying", file=sys.stderr)
                raise

            delay_sec = backoff_sec
            delay_source = ""
            if error.response is not None:
                retry_after_sec = _retry_after_delay(
                    error.response.headers.get("Retry-After", "")
                )
                if retry_after_sec is not None and retry_after_sec > delay_sec:
                    delay_sec = retry_after_sec
                    delay_source = (
                        f" (Retry-After from HTTP {error.response.status_code})"
                    )
            remaining_sec = max(0.0, deadline_sec - time.monotonic())
            if delay_sec > remaining_sec:
                print(
                    f"{failure}; retrying again would exceed the remaining time budget "
                    f"({remaining_sec:.1f}s remaining, {delay_sec:g}s required){delay_source}",
                    file=sys.stderr,
                )
                raise

            print(
                f"{failure}; retrying in {delay_sec:g}s{delay_source}",
                file=sys.stderr,
            )
            time.sleep(delay_sec)
            backoff_sec *= 2


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


def verify_sha256(fileobj: IO[bytes], expected: str, url: str) -> None:
    """Raise unless the contents of fileobj have the expected SHA-256 digest."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
        digest.update(chunk)
    fileobj.seek(0)
    if digest.hexdigest() != expected.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {url}: expected {expected}, "
            f"got {digest.hexdigest()}"
        )


def stream_conda_executable(url: str, sha256: Optional[str] = None) -> Path:
    """Extract `conda.exe` from the conda-standalone package at the given URL.

    This is roughly a more modern version of `extract_files_from_tar_bz2`
    that also works with `.conda` archives.

    Download the archive before extracting it to avoid an HTTP range request
    for every small read made by the decompressor. If sha256 is given, verify
    the downloaded archive against it before extracting anything.
    """
    dest_filename = f"conda_standalone{exe_suffix()}"

    filename = urlparse(url).path.rsplit("/", 1)[-1]
    with TemporaryFile() as conda:
        request_url_with_retry(url, destination=conda)
        if sha256:
            verify_sha256(conda, sha256, url)
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
        path_to_written_executable = stream_conda_executable(url, chosen.sha256)
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
            # Older conda-forge files lack a SHA-256 digest.
            sha256=file_info.get("sha256"),
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
        try:
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
        except BaseException:
            # Don't leave a partial executable behind, for example after Ctrl-C.
            with contextlib.suppress(OSError):
                temp_filename.unlink()
            raise
