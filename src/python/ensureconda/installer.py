import contextlib
import io
import math
import os
import stat
import tarfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import IO, TYPE_CHECKING, Iterator, NamedTuple, Optional

import filelock
import requests
from conda_package_streaming.package_streaming import stream_conda_component
from conda_package_streaming.url import conda_reader_for_url
from packaging.version import Version

from ensureconda.resolve import is_windows, platform_subdir, site_path

if TYPE_CHECKING:
    from _typeshed import StrPath


class AnacondaPkgAttr(NamedTuple):
    """Inner for data from Anaconda API

    <https://api.anaconda.org/package/anaconda/conda-standalone/files>
    <https://api.anaconda.org/package/conda-forge/conda-standalone/files>
    """

    subdir: str
    build_number: int
    timestamp: int


class AnacondaPkg(NamedTuple):
    """Outer model for data from Anaconda API"""

    size: int
    attrs: AnacondaPkgAttr
    type: str
    version: str
    download_url: str


def request_url_with_retry(url: str) -> requests.Response:
    n = 10
    for i in range(n):
        resp = requests.get(url, allow_redirects=True)
        try:
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 500:
                timeout = max(math.e ** (i / 4), 15)
                print(f"Failed to retrieve, retrying in {timeout:.2f} seconds")
                time.sleep(timeout)
            else:
                raise

    raise TimeoutError(f"Could not retrieve {url} in {n} tries")


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
        exe_fo.write(fo.read())
    return target_path


def stream_conda_executable(url: str) -> Path:
    """Extract `conda.exe` from the conda-standalone package at the given URL.

    This is roughly a more modern version of `extract_files_from_tar_bz2`
    that also works with `.conda` archives.

    The code roughly follows the README from conda-package-streaming.
    """
    dest_filename = f"conda_standalone{exe_suffix()}"

    filename, conda = conda_reader_for_url(url)
    with closing(conda):
        for tar, member in stream_conda_component(filename, conda, component="pkg"):
            if member.name == "standalone_conda/conda.exe":
                fo = tar.extractfile(member)
                conda_exe = write_executable_from_file_object(fo, dest_filename)
                return conda_exe
        raise RuntimeError("Could not find conda.exe in the conda-standalone package")


def get_channel_name() -> str:
    channel = os.environ.get("ENSURECONDA_CONDA_STANDALONE_CHANNEL", "anaconda")
    # Raise an error if the channel name contains any non-alphnumeric characters
    # besides - or _
    if not channel.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid channel name {channel}. Channel names must be alphanumeric"
            " and may contain hyphens and underscores."
        )
    return channel


def install_conda_exe() -> Optional[Path]:
    channel = get_channel_name()
    url = f"https://api.anaconda.org/package/{channel}/conda-standalone/files"
    resp = request_url_with_retry(url)
    subdir = platform_subdir()

    candidates = []
    for file_info in resp.json():
        info_attrs = AnacondaPkgAttr(
            subdir=file_info["attrs"]["subdir"],
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
        if info.attrs.subdir == subdir:
            candidates.append(info)

    candidates.sort(
        key=lambda info: (
            Version(info.version),
            info.attrs.build_number,
            info.attrs.timestamp,
        )
    )
    if len(candidates) == 0:
        raise RuntimeError(f"No conda-standalone package found for {subdir}")

    chosen = candidates[-1]
    url = "https:" + chosen.download_url
    path_to_written_executable = stream_conda_executable(url)
    return path_to_written_executable


def install_micromamba() -> Optional[Path]:
    """Install micromamba into the installation"""
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
    """Create a new executabler that can be written to.

    Care is take to both prevent concurrent writes as well as guarding against
    early reads.
    """
    with filelock.FileLock(f"{str(target_filename)}.lock"):
        temp_filename = target_filename.with_name(uuid.uuid4().hex)
        with open(temp_filename, "wb") as fo:
            yield fo
        st = os.stat(temp_filename)
        os.chmod(temp_filename, st.st_mode | stat.S_IXUSR)
        os.rename(temp_filename, target_filename)
