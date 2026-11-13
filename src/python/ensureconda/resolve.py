import os
import pathlib
import platform
import shutil
import sys
import sysconfig
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
        for p in os.environ.get("PATH", "").split(os.pathsep)
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


class UnsupportedPlatformError(RuntimeError):
    """No conda subdir is known for the platform."""


def platform_subdir() -> str:
    """Return the conda subdir of the running Python, detected as conda does."""
    import struct

    bits = 8 * struct.calcsize("P")
    if sys.platform == "win32":
        return windows_subdir(sysconfig.get_platform(), bits)
    return subdir_for(sys.platform, platform.machine(), bits)


def windows_subdir(sysconfig_platform: str, bits: int) -> str:
    """Map the build of a Windows Python to its conda subdir.

    Like conda, this goes by what the interpreter was built for, so an x86-64
    Python on Windows on ARM gets win-64. platform.machine() can't decide
    this: for that Python it answers ARM64 from Python 3.12 on, and AMD64
    before.
    """
    windows_subdirs = {
        "win-amd64": "win-64",
        "win-arm64": "win-arm64",
        "win32": "win-32",
    }
    # Other builds, such as MinGW's, keep the pointer-size subdir as before.
    return windows_subdirs.get(sysconfig_platform, f"win-{bits}")


def subdir_for(sys_platform: str, machine: str, bits: int) -> str:
    """Map a platform other than Windows to its conda subdir.

    This follows conda's own detection, but raises for a platform that conda
    has no subdir for.
    """
    os_names = {
        "linux2": "linux",
        "linux": "linux",
        "darwin": "osx",
    }
    non_x86_machines = {
        "aarch64",  # for linux
        "arm64",  # for osx
        "armv6l",
        "armv7l",
        "ppc64",
        "ppc64le",
        "riscv64",
        "s390x",
    }

    if sys_platform == "zos":
        return "zos-z"
    os_name = os_names.get(sys_platform)
    if os_name is not None:
        name = machine.lower()
        if name in non_x86_machines:
            return f"{os_name}-{name}"
        return f"{os_name}-{bits}"
    raise UnsupportedPlatformError(
        f"Unsupported platform {sys_platform!r} ({machine}): "
        "no conda subdir is known for it"
    )


is_windows = platform.system() == "Windows"
