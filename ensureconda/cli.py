import contextlib
import os
import pathlib
import platform
import shutil
import stat
import sys
import time
import typing
from pathlib import Path
from typing import IO, Iterator, Optional, Union

import requests

import appdirs
import click

if typing.TYPE_CHECKING:
    from os import PathLike

is_windows = platform.system() == "Windows"
site_path = pathlib.Path(appdirs.user_data_dir("ensure-conda"))


@click.command(help="Ensures that a conda/mamba is installed.")
@click.option("--mamba/--no-mamba", default=True, help="Search for mamba")
@click.option(
    "--micromamba/--no-micromamba",
    default=True,
    help="Search for mamba/micromamba, Install if not present",
)
@click.option("--conda/--no-conda", default=True, help="Search for conda")
@click.option(
    "--conda-exe/--no-conda-exe",
    default=True,
    help="Search for conda.exe / conda-standalone, install if not present",
)
@click.option("--no-install", is_flag=True)
def ensureconda_cli(mamba, micromamba, conda, conda_exe, no_install):
    exe = ensure_conda(
        mamba=mamba,
        micromamba=micromamba,
        conda=conda,
        conda_exe=conda_exe,
        no_install=no_install,
    )
    if exe:
        print("Found compatible executable", file=sys.stderr, flush=True)
        # silly thing to force correct output order
        time.sleep(0.01)
        print(str(exe), flush=True)
        sys.exit(0)
    else:
        print("Could not find compatible executable", file=sys.stderr)
        sys.exit(1)


def ext_candidates(fpath: str) -> Iterator[str]:
    if is_windows:
        exts = os.environ.get("PATHEXT", "").split(os.pathsep)
    else:
        exts = ["", ".exe"]
    for ext in exts:
        yield fpath + ext


def is_exe(fpath: Optional[Path]):
    if fpath is None:
        return False
    return os.path.exists(fpath) and os.access(fpath, os.X_OK) and os.path.isfile(fpath)


def resolve_executable(
    exe_name: str, path_prefix: Optional[Path] = site_path
) -> Iterator[Path]:
    for candidate in ext_candidates(exe_name):
        if path_prefix is not None:
            prefixed_exe = path_prefix / candidate
            if is_exe(prefixed_exe):
                yield prefixed_exe

        # which based exe
        exe = shutil.which(candidate)
        if exe:
            exe_path = pathlib.Path(exe)
            if is_exe(exe_path):
                yield exe_path


def conda_executables() -> Iterator[PathLike]:
    conda_exe_from_env = os.environ.get("CONDA_EXE")
    if conda_exe_from_env:
        if is_exe(Path(conda_exe_from_env)):
            yield Path(conda_exe_from_env)
    yield from resolve_executable("conda")


def conda_standalone_executables() -> Iterator[PathLike]:
    yield from resolve_executable("conda_standalone")


def mamba_executables() -> Iterator[PathLike]:
    yield from resolve_executable("mamba")


def micromamba_executables() -> Iterator[PathLike]:
    yield from resolve_executable("micromamba")


def safe_next(it):
    try:
        return next(it)
    except StopIteration:
        return None


def ensure_conda(
    *,
    mamba: bool = True,
    micromamba: bool = True,
    conda: bool = True,
    conda_exe: bool = True,
    no_install: bool = False,
) -> Optional[PathLike]:
    if mamba:
        exe = safe_next(mamba_executables())
        if exe:
            return exe
    if micromamba:
        exe = safe_next(micromamba_executables())
        if exe:
            return exe
        if not no_install:
            return install_micromamba()
    if conda:
        exe = safe_next(conda_executables())
        if exe:
            return exe
    if conda_exe:
        exe = safe_next(conda_standalone_executables())
        if exe:
            return exe
        if not no_install:
            return install_conda_exe()
    return None


@contextlib.contextmanager
def new_executable(target_filename: "PathLike") -> Iterator[IO[bytes]]:
    with open(target_filename, "wb") as fo:
        yield fo
    st = os.stat(target_filename)
    os.chmod(target_filename, st.st_mode | stat.S_IXUSR)


def install_conda_exe() -> Optional[Path]:
    conda_exe_prefix = "https://repo.anaconda.com/pkgs/misc/conda-execs"
    if platform.system() == "Linux":
        conda_exe_file = "conda-latest-linux-64.exe"
    elif platform.system() == "Darwin":
        conda_exe_file = "conda-latest-osx-64.exe"
    elif platform.system() == "NT":
        conda_exe_file = "conda-latest-win-64.exe"
    else:
        # TODO: Support other platforms here
        raise ValueError(f"Unsupported platform: {platform.system()}")

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
    if platform.system() == "Linux":
        p = "linux-64"
    elif platform.system() == "Darwin":
        p = "osx-64"
    else:
        return None
        raise ValueError(f"Unsupported platform: {platform.system()}")
    url = f"https://micromamba.snakepit.net/api/micromamba/{p}/latest"
    resp = requests.get(url, allow_redirects=True)
    resp.raise_for_status()
    import tarfile
    import io

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


if __name__ == "__main__":
    ensureconda_cli()
