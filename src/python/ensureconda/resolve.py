import os
import pathlib
import platform
import shutil
import sys

from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional, TypeVar

import appdirs


def ext_candidates(fpath: str) -> Iterator[str]:
    if is_windows:
        exts = os.environ.get("PATHEXT", "").split(os.pathsep)
    else:
        exts = ["", ".exe"]
    for ext in exts:
        yield fpath + ext


def is_exe(fpath: Optional[Path]) -> bool:
    if fpath is None:
        return False
    return os.path.exists(fpath) and os.access(fpath, os.X_OK) and os.path.isfile(fpath)


def which_no_shims(executable: str) -> Optional[str]:
    """Ensure that environment manager tools like pyenv don't break us

    Certain tools like pyenv make use of magic shim directories that contain
    executables that are unsuitable for our use case.

    These executables will exist but may fail on use due to not being in the right
    pyenv execution environment.
    """
    path_list = [
        p
        for p in os.environ["PATH"].split(os.pathsep)
        if os.path.join(".pyenv", "shims") not in p
    ]
    return shutil.which(executable, path=os.pathsep.join(path_list))


def site_path() -> pathlib.Path:
    return pathlib.Path(appdirs.user_data_dir("ensure-conda"))


def resolve_executable(exe_name: str) -> Iterator[Path]:
    path_prefix = site_path()
    for candidate in ext_candidates(exe_name):
        if path_prefix is not None:
            prefixed_exe = path_prefix / candidate
            if is_exe(prefixed_exe):
                yield prefixed_exe

        # which based exe
        exe = which_no_shims(candidate)
        if exe:
            exe_path = pathlib.Path(exe)
            if is_exe(exe_path):
                yield exe_path


def conda_executables() -> Iterator[Path]:
    conda_exe_from_env = os.environ.get("CONDA_EXE")
    if conda_exe_from_env:
        if is_exe(Path(conda_exe_from_env)):
            yield Path(conda_exe_from_env)
    yield from resolve_executable("conda")


def conda_standalone_executables() -> Iterator[Path]:
    yield from resolve_executable("conda_standalone")


def mamba_executables() -> Iterator[Path]:
    yield from resolve_executable("mamba")


def micromamba_executables() -> Iterator[Path]:
    yield from resolve_executable("micromamba")


T = TypeVar("T")


def safe_next(it: Iterator[T]) -> Optional[T]:
    try:
        return next(it)
    except StopIteration:
        return None


def platform_subdir() -> str:
    # Adapted from conda.context
    _platform_map = {
        "linux2": "linux",
        "linux": "linux",
        "darwin": "osx",
        "win32": "win",
    }
    non_x86_machines = {
        "aarch64",  # for linux
        "arm64",  # for osx
        "ppc64",
        "ppc64le",
    }
    _arch_names = {
        32: "x86",
        64: "x86_64",
    }

    import struct

    bits = 8 * struct.calcsize("P")
    plat = _platform_map[sys.platform]
    machine = platform.machine()
    if machine in non_x86_machines:
        return f"{plat}-{machine}"
    elif sys.platform.lower() == "zos":
        return "zos-z"
    else:
        return f"{plat}-{bits}"


is_windows = platform.system() == "Windows"
