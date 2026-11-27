import contextlib
import hashlib
import io
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import time
import uuid
from contextlib import closing
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryFile
from typing import (
    IO,
    Any,
    Dict,
    Generator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)
from urllib.parse import urljoin, urlparse

import filelock
import requests
from conda_package_streaming.package_streaming import stream_conda_component
from packaging.version import InvalidVersion, Version

from ensureconda import determine_versions
from ensureconda.determine_versions import (
    Flavor,
    VersionMatch,
    _Observations,
    _VersionConstraint,
    check_executable,
    has_version_constraint,
    match_package_version,
    match_version,
)
from ensureconda.resolve import is_exe, is_windows, platform_subdir, site_path

logger = logging.getLogger(__name__)

LOCK_NOTIFICATION_INTERVAL_SEC = 5
"""Seconds between lock acquisition attempts and waiting notifications."""

CONNECT_TIMEOUT_SEC = 10
"""Connection establishment timeout in seconds."""

READ_TIMEOUT_SEC = 60
"""Socket inactivity timeout in seconds; does not limit total download time."""

RETRY_TIME_BUDGET_SEC = 70
"""Elapsed seconds allowed for scheduling retries; active downloads may finish."""


class NotPublishedError(RuntimeError):
    """Nothing that could be installed is published for this platform."""


class NoCompatibleVersionError(RuntimeError):
    """Packages exist for the platform, but none satisfies this target."""


class DownloadedExecutableError(RuntimeError):
    """The downloaded executable cannot run or fails the requested requirement."""


class DownloadValidation(NamedTuple):
    flavor: Flavor
    constraint: _VersionConstraint
    expected_version: Version
    observations: Optional[_Observations] = None


class ExecutableReplacementError(OSError, RuntimeError):
    """A downloaded executable could not be moved into place."""

    def __str__(self) -> str:
        return self.strerror if self.strerror is not None else super().__str__()


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


class _HeadersOnlySession(requests.Session):
    def get_redirect_target(self, resp: requests.Response) -> Optional[str]:
        # Even with allow_redirects=False, Requests normally consumes a
        # redirect body to prepare Response.next. We only need its headers.
        return None


def request_url_with_retry(
    url: str,
    *,
    destination: Optional[IO[bytes]] = None,
    follow_redirects: bool = True,
    retry: bool = True,
) -> requests.Response:
    """Retrieve a URL, retrying transient failures during the entire download.

    By default, return a response with its body in memory. If destination is
    provided, stream the body there instead, replacing its contents on each
    attempt and rewinding it on success. The caller owns the destination;
    the returned response's body has already been consumed in this mode.

    With follow_redirects=False, return headers without reading the body, and
    return a 404 instead of raising; the latest-version lookup expects both.
    With retry=False, let the caller handle the first failure.

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
            session_type = requests.Session if follow_redirects else _HeadersOnlySession
            with session_type() as session, session.get(
                url,
                stream=True,
                timeout=(CONNECT_TIMEOUT_SEC, READ_TIMEOUT_SEC),
                allow_redirects=follow_redirects,
            ) as response:
                if not follow_redirects and response.status_code == 404:
                    return response
                response.raise_for_status()
                if not follow_redirects:
                    # Inspect headers only, even if the endpoint unexpectedly returns 200.
                    return response
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
            if not retry:
                raise
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
                logger.warning("%s; not retrying", failure)
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
                logger.warning(
                    "%s; retrying again would exceed the remaining time budget "
                    "(%.1fs remaining, %gs required)%s",
                    failure,
                    remaining_sec,
                    delay_sec,
                    delay_source,
                )
                raise

            logger.warning("%s; retrying in %gs%s", failure, delay_sec, delay_source)
            time.sleep(delay_sec)
            backoff_sec *= 2


def extract_files_from_tar_bz2(
    tarball: io.BytesIO,
    filename: str,
    dest_filename: str,
    *,
    data_dir: Optional[Path] = None,
    validation: Optional[DownloadValidation] = None,
) -> Path:
    tarball.seek(0)
    with tarfile.open(mode="r:bz2", fileobj=tarball) as tf:
        fo = tf.extractfile(filename)
        if fo is None:
            raise RuntimeError("Could not extract executable!")
        return write_executable_from_file_object(
            fo, dest_filename, data_dir=data_dir, validation=validation
        )


def write_executable_from_file_object(
    fo: IO[bytes],
    dest_filename: str,
    *,
    data_dir: Optional[Path] = None,
    validation: Optional[DownloadValidation] = None,
) -> Path:
    if data_dir is None:
        data_dir = site_path()
    data_dir.mkdir(parents=True, exist_ok=True)
    target_path = data_dir / dest_filename
    with new_executable(target_path, validation=validation) as exe_fo:
        shutil.copyfileobj(fo, exe_fo, length=1024 * 1024)
    return target_path


@contextlib.contextmanager
def lock_with_feedback(
    lock_path: Union[str, Path], lock_name: str
) -> Generator[None, None, None]:
    """Context manager to acquire a lock with user feedback when we need to wait.

    Args:
        lock_path: The path to the lock file
        lock_name: A descriptive name for the lock to show in waiting messages
    """
    lock = filelock.FileLock(str(lock_path))
    lock_start = time.monotonic()

    while True:
        try:
            # Try to acquire with timeout matching notification interval
            lock.acquire(timeout=LOCK_NOTIFICATION_INTERVAL_SEC)
        except filelock.Timeout:
            total_wait_time = time.monotonic() - lock_start
            logger.warning(
                "Waiting for lock for %s to be released by another process... (%.1fs)",
                lock_name,
                total_wait_time,
            )
        else:
            break

    try:
        # Success! Now report total wait time if we had to wait
        total_wait_time = time.monotonic() - lock_start
        if total_wait_time > 0.1:
            logger.info(
                "Lock for %s acquired after waiting %.1f seconds",
                lock_name,
                total_wait_time,
            )
        # Yield control back to the caller
        yield
    finally:
        # Always release the lock
        lock.release()


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


def stream_conda_executable(
    url: str,
    sha256: Optional[str] = None,
    *,
    data_dir: Optional[Path] = None,
    validation: Optional[DownloadValidation] = None,
) -> Path:
    """Extract `conda.exe` from the conda-standalone package at the given URL.

    This is roughly a more modern version of `extract_files_from_tar_bz2`
    that also works with `.conda` archives.

    Download the archive before extracting it to avoid an HTTP range request
    for every small read made by the decompressor. If sha256 is given, verify
    the downloaded archive against it before extracting anything.
    """
    dest_filename = f"conda_standalone{exe_suffix()}"

    filename = urlparse(url).path.rsplit("/", 1)[-1]
    logger.info("conda-standalone: downloading %s", url)
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
                    conda_exe = write_executable_from_file_object(
                        fo, dest_filename, data_dir=data_dir, validation=validation
                    )
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


def install_conda_exe(
    constraint: _VersionConstraint = None,
    *,
    force: bool = False,
    probe_version: bool = True,
    observations: Optional[_Observations] = None,
) -> Path:
    """Install conda-standalone into the installation

    Recheck the cached executable under the download lock against the caller's
    requirement. Force bypasses reuse, including after waiting for another writer.
    """
    subdir = platform_subdir()
    # Create a lock file specific to conda_exe to prevent concurrent downloads
    data_dir = site_path()
    lock_path = data_dir / "conda_exe_install.lock"
    dest_filename = f"conda_standalone{exe_suffix()}"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Use the context manager for lock acquisition with feedback
    with lock_with_feedback(lock_path, "downloading conda"):
        # Check if the file already exists after acquiring the lock
        target_path = data_dir / dest_filename
        if (
            not force
            and is_exe(target_path)
            and check_executable(
                target_path,
                "conda_standalone",
                constraint,
                observations=observations,
                probe_version=probe_version,
                rejection_level=None,
            )
            is not None
        ):
            logger.debug(
                "Reusing cached executable %s after acquiring the download lock",
                target_path,
            )
            return target_path

        channel = get_channel_name()
        candidates = compute_candidates(channel, subdir)
        chosen = select_candidate(candidates, constraint, "conda-standalone", subdir)
        logger.info(
            "conda-standalone: selected version %s for %s", chosen.version, subdir
        )
        url = "https:" + chosen.download_url
        path_to_written_executable = stream_conda_executable(
            url,
            chosen.sha256,
            data_dir=data_dir,
            validation=DownloadValidation(
                "conda_standalone", constraint, Version(chosen.version), observations
            ),
        )
        return path_to_written_executable


def compute_candidates(
    channel: str, subdir: str, package: str = "conda-standalone"
) -> List[AnacondaPkg]:
    """List supported builds for the package, sorted by version/build/time."""

    url = f"https://api.anaconda.org/package/{channel}/{package}/files"
    logger.debug("Fetching %s metadata from %s", package, url)
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
        # Builds marked as broken get the "broken" label and may keep "main".
        # Keep entries whose labels are missing or null, as from a listing
        # that omits them.
        labels = file_info.get("labels")
        if not (
            info.attrs.subdir == subdir
            # Ignore onedir packages as workaround for
            # <https://github.com/conda/conda-standalone/issues/182>
            and (package != "conda-standalone" or "_onedir_" not in info.attrs.build)
            and (labels is None or ("main" in labels and "broken" not in labels))
        ):
            continue
        try:
            artifact = urlparse(info.download_url)
        except ValueError:
            logger.warning(
                "Skipping %s %s for %s: invalid download URL",
                package,
                info.version,
                subdir,
            )
            continue
        if not artifact.path.endswith((".tar.bz2", ".conda")):
            continue
        try:
            Version(info.version)
        except InvalidVersion:
            logger.warning(
                "Skipping %s %s for %s: not a valid version",
                package,
                info.version,
                subdir,
            )
            continue
        candidates.append(info)

    candidates.sort(
        key=lambda attrs: (
            Version(attrs.version),
            attrs.attrs.build_number,
            attrs.attrs.timestamp,
        )
    )
    if len(candidates) == 0:
        if package == "micromamba":
            raise NotPublishedError(f"micromamba is not published for {subdir}")
        raise NotPublishedError(
            f"No {package} package found for {subdir} in channel {channel}"
        )
    return candidates


def install_micromamba(
    constraint: _VersionConstraint = None,
    *,
    force: bool = False,
    probe_version: bool = True,
    observations: Optional[_Observations] = None,
) -> Path:
    """Install micromamba into the installation

    Recheck the cached executable under the download lock against the caller's
    requirement. Force bypasses reuse, including after waiting for another writer.
    """
    subdir = platform_subdir()
    # Create a lock file specific to micromamba to prevent concurrent downloads
    data_dir = site_path()
    lock_path = data_dir / "micromamba_install.lock"
    dest_filename = f"micromamba{exe_suffix()}"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Use the context manager for lock acquisition with feedback
    with lock_with_feedback(lock_path, "downloading micromamba"):
        # Check if the file already exists after acquiring the lock
        target_path = data_dir / dest_filename
        if (
            not force
            and is_exe(target_path)
            and check_executable(
                target_path,
                "micromamba",
                constraint,
                observations=observations,
                probe_version=probe_version,
                rejection_level=None,
            )
            is not None
        ):
            logger.debug(
                "Reusing cached executable %s after acquiring the download lock",
                target_path,
            )
            return target_path

        url, version, sha256 = select_micromamba(subdir, constraint)
        logger.info("micromamba: downloading %s", url)
        validation = DownloadValidation("micromamba", constraint, version, observations)
        resp = request_url_with_retry(url)
        tarball = io.BytesIO(resp.content)
        if sha256:
            verify_sha256(tarball, sha256, url)
        filename = "Library/bin/micromamba.exe" if is_windows else "bin/micromamba"
        if urlparse(url).path.endswith(".conda"):
            with closing(
                stream_conda_component(
                    urlparse(url).path.rsplit("/", 1)[-1], tarball, component="pkg"
                )
            ) as stream:
                for tar, member in stream:
                    if member.name == filename:
                        fo = tar.extractfile(member)
                        if fo is not None:
                            return write_executable_from_file_object(
                                fo,
                                dest_filename,
                                data_dir=data_dir,
                                validation=validation,
                            )
            raise RuntimeError("Could not find micromamba in the package")
        return extract_files_from_tar_bz2(
            tarball=tarball,
            filename=filename,
            dest_filename=dest_filename,
            data_dir=data_dir,
            validation=validation,
        )


def select_candidate(
    candidates: List[AnacondaPkg],
    constraint: _VersionConstraint,
    package: str,
    subdir: str,
) -> AnacondaPkg:
    reason = VersionMatch.MISMATCH
    for candidate in reversed(candidates):
        match = match_package_version(Version(candidate.version), constraint)
        if match is VersionMatch.ACCEPTED:
            return candidate
        if match is VersionMatch.PRERELEASE_NOT_REQUESTED:
            reason = match
    raise NoCompatibleVersionError(
        f"{package}: no published version for {subdir} satisfies {reason.rejection_text(constraint)}; "
        f"newest available is {candidates[-1].version}"
    )


def _latest_micromamba(
    subdir: str, constraint: _VersionConstraint
) -> Optional[Tuple[str, Version]]:
    latest = f"https://micro.mamba.pm/api/micromamba/{subdir}/latest"
    logger.debug("Checking latest micromamba at %s", latest)
    try:
        response = request_url_with_retry(latest, follow_redirects=False, retry=False)
    except requests.RequestException as error:
        logger.warning(
            "micromamba: lookup at %s failed (%s); checking release metadata",
            latest,
            error,
        )
        return None
    if response.status_code == 404:
        logger.info(
            "micromamba: no build of the latest release for %s; checking release metadata",
            subdir,
        )
    else:
        version = None
        try:
            artifact = urljoin(latest, response.headers.get("Location", ""))
            parsed = urlparse(artifact)
            prefix = f"/conda-forge/{subdir}/micromamba-"
            if (
                response.status_code in (301, 302, 303, 307, 308)
                and parsed.scheme == "https"
                and parsed.netloc == "conda.anaconda.org"
                and parsed.path.startswith(prefix)
                and parsed.path.endswith(".tar.bz2")
                and not parsed.query
                and not parsed.fragment
            ):
                package_version, build = parsed.path[
                    len(prefix) : -len(".tar.bz2")
                ].rsplit("-", 1)
                if build:
                    version = Version(package_version)
        except ValueError:
            # A malformed redirect URL or package filename cannot identify
            # the release; the full metadata can still provide a usable build.
            pass
        if version is not None:
            match = match_package_version(version, constraint)
            if match is VersionMatch.ACCEPTED:
                logger.info(
                    "micromamba: selected latest version %s for %s", version, subdir
                )
                return artifact, version
            logger.info(
                "micromamba: latest version %s does not satisfy %s; checking release metadata",
                version,
                match.rejection_text(constraint),
            )
        else:
            logger.info(
                "micromamba: latest response did not identify a release; checking release metadata"
            )
    return None


def select_micromamba(
    subdir: str, constraint: _VersionConstraint
) -> Tuple[str, Version, Optional[str]]:
    latest = _latest_micromamba(subdir, constraint)
    if latest is not None:
        url, version = latest
        return url, version, None
    candidates = compute_candidates("conda-forge", subdir, "micromamba")
    chosen = select_candidate(candidates, constraint, "micromamba", subdir)
    logger.info("micromamba: selected version %s for %s", chosen.version, subdir)
    return "https:" + chosen.download_url, Version(chosen.version), chosen.sha256


def exe_suffix() -> str:
    return ".exe" if is_windows else ""


@contextlib.contextmanager
def new_executable(
    target_filename: "Path", *, validation: Optional[DownloadValidation] = None
) -> Generator[IO[bytes], None, None]:
    """Create a new executable that can be written to.

    Care is take to both prevent concurrent writes as well as guarding against
    early reads.
    """
    lock_path = f"{str(target_filename)}.lock"
    observations = None if validation is None else validation.observations
    with lock_with_feedback(lock_path, f"file write ({target_filename})"):
        temp_filename = target_filename.with_name(
            uuid.uuid4().hex + (".exe" if is_windows else "")
        )
        try:
            with open(temp_filename, "wb") as fo:
                yield fo
            st = os.stat(temp_filename)
            os.chmod(temp_filename, st.st_mode | stat.S_IXUSR)
            if validation is not None:
                validate_download(temp_filename, validation)
            validated = determine_versions.detach_observation(
                observations, temp_filename
            )
            try:
                os.replace(temp_filename, target_filename)
            except OSError as error:
                reason = error.strerror or str(error)
                detail = f"Could not replace executable {target_filename}: {reason}"
                if is_windows:
                    detail += "; it may be in use; close processes using it and retry"
                if error.errno is None:
                    replacement_error = ExecutableReplacementError(detail)
                else:
                    replacement_error = ExecutableReplacementError(
                        error.errno,
                        detail,
                        error.filename,
                        getattr(error, "winerror", None),
                        error.filename2,
                    )
                raise replacement_error from error
            determine_versions.attach_observation(
                observations, target_filename, validated
            )
        except BaseException:
            # Don't leave a partial executable behind, for example after Ctrl-C.
            with contextlib.suppress(OSError):
                temp_filename.unlink()
            raise


def validate_download(path: Path, validation: DownloadValidation) -> None:
    tool = validation.flavor.replace("_", "-")
    try:
        version = determine_versions._probe_version(
            path, validation.flavor, validation.observations
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise DownloadedExecutableError(
            f"{tool}: downloaded executable failed its version check: {error}"
        ) from error
    except (InvalidVersion, UnicodeDecodeError):
        version = None
    if version is None and has_version_constraint(validation.constraint):
        # A staged executable must report a version for every explicit minimum.
        match = VersionMatch.UNKNOWN
    else:
        match = match_version(version, validation.constraint)
    if match is not VersionMatch.ACCEPTED:
        raise DownloadedExecutableError(
            f"{tool}: downloaded executable reports {version or 'unknown version'}, "
            f"expected {validation.expected_version} satisfying {match.rejection_text(validation.constraint)}"
        )
    if version is None:
        logger.info(
            "%s: downloaded executable ran but reported no recognizable version (package version %s)",
            tool,
            validation.expected_version,
        )
    elif version == validation.expected_version:
        logger.info("%s: validated executable version %s", tool, version)
    else:
        logger.info(
            "%s: validated executable version %s (package version %s)",
            tool,
            version,
            validation.expected_version,
        )
