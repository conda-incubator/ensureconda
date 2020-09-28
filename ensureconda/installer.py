import contextlib
import os
import pathlib
import stat
from os import PathLike
from pathlib import Path
from typing import IO, Iterator, Optional

import filelock
import requests

from ensureconda.resolve import is_windows, platform_subdir, site_path


def install_conda_exe() -> Optional[Path]:
    conda_exe_prefix = "https://repo.anaconda.com/pkgs/misc/conda-execs"
    subdir = platform_subdir()
    conda_exe_file = f"conda-latest-{subdir}.exe"

    resp = requests.get(f"{conda_exe_prefix}/{conda_exe_file}", allow_redirects=True)
    resp.raise_for_status()

    site_path.mkdir(parents=True, exist_ok=True)
    ext = ".exe" if is_windows else ""
    target_filename = site_path / f"conda_standalone{ext}"
    with new_executable(target_filename) as fo:
        fo.write(resp.content)
    return pathlib.Path(target_filename)


def install_micromamba() -> Optional[Path]:
    """Install micromamba into the installation"""
    subdir = platform_subdir()
    url = f"https://micromamba.snakepit.net/api/micromamba/{subdir}/latest"
    resp = requests.get(url, allow_redirects=True)
    resp.raise_for_status()
    import io
    import tarfile

    tarball = io.BytesIO(resp.content)
    tarball.seek(0)
    with tarfile.open(mode="r:bz2", fileobj=tarball) as tf:
        fo = tf.extractfile("bin/micromamba")
        if fo is None:
            raise RuntimeError("Could not extract micromamba executable!")
        ext = ".exe" if is_windows else ""
        site_path.mkdir(parents=True, exist_ok=True)
        target_path = site_path / f"micromamba{ext}"
        with new_executable(target_path) as exe_fo:
            exe_fo.write(fo.read())
        return target_path


@contextlib.contextmanager
def new_executable(target_filename: "PathLike") -> Iterator[IO[bytes]]:
    with filelock.FileLock(f"{str(target_filename)}.lock"):
        with open(target_filename, "wb") as fo:
            yield fo
        st = os.stat(target_filename)
        os.chmod(target_filename, st.st_mode | stat.S_IXUSR)
