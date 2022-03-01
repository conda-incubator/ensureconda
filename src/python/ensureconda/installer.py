import contextlib
import io
import math
import os
import stat
import tarfile
import time

from distutils.version import LooseVersion
from pathlib import Path
from typing import IO, TYPE_CHECKING, Iterator, Optional

import filelock
import requests

from ensureconda.resolve import is_windows, platform_subdir, site_path


if TYPE_CHECKING:
    from _typeshed import AnyPath


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


def install_conda_exe() -> Optional[Path]:
    url = "https://api.anaconda.org/package/anaconda/conda-standalone/files"
    resp = request_url_with_retry(url)
    subdir = platform_subdir()

    candidates = []
    for file_info in resp.json():
        if file_info["attrs"]["subdir"] == subdir:
            candidates.append(file_info["attrs"])

    candidates.sort(
        key=lambda attrs: (
            LooseVersion(attrs["version"]),
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
        dest_filename=f"conda_standalone{exe_suffix()}",
    )


def install_micromamba() -> Optional[Path]:
    """Install micromamba into the installation"""
    subdir = platform_subdir()
    url = f"https://micromamba.snakepit.net/api/micromamba/{subdir}/latest"
    resp = request_url_with_retry(url)

    tarball = io.BytesIO(resp.content)
    if is_windows:
        filename = "Library/bin/micromamba.exe"
    else:
        filename = "bin/micromamba"

    return extract_files_from_conda_package(
        tarball=tarball, filename=filename, dest_filename=f"micromamba{exe_suffix()}"
    )


def exe_suffix() -> str:
    return ".exe" if is_windows else ""


@contextlib.contextmanager
def new_executable(target_filename: "AnyPath") -> Iterator[IO[bytes]]:
    with filelock.FileLock(f"{str(target_filename)}.lock"):
        with open(target_filename, "wb") as fo:
            yield fo
        st = os.stat(target_filename)
        os.chmod(target_filename, st.st_mode | stat.S_IXUSR)
