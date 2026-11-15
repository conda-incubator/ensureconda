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
    import struct

    bits = 8 * struct.calcsize("P")
    if sys.platform == "win32":
        return windows_subdir(sysconfig.get_platform(), bits)
    return subdir_for(sys.platform, platform.machine(), bits)


def windows_subdir(sysconfig_platform: str, bits: int) -> str:
    """Map the build of a Windows Python to its conda subdir, as conda does.

    An x86-64 Python on Windows on ARM therefore gets win-64.
    """
    windows_subdirs = {
        "win-amd64": "win-64",
        "win-arm64": "win-arm64",
        "win32": "win-32",
    }
    # MSYS2's MinGW builds have other names, such as mingw_aarch64_ucrt_llvm.
    if sysconfig_platform.startswith("mingw_aarch64"):
        return "win-arm64"
    return windows_subdirs.get(sysconfig_platform, f"win-{bits}")


def subdir_for(sys_platform: str, machine: str, bits: int) -> str:
    """Map a platform other than Windows to its conda subdir."""
    # Adapted from conda.context
    _platform_map = {
        "linux2": "linux",
        "linux": "linux",
        "darwin": "osx",
    }
    # Keep this in step with platformMap in src/golang/cmd/ensureconda.go.
    non_x86_subdirs = {
        ("linux", "aarch64"): "linux-aarch64",
        ("linux", "ppc64"): "linux-ppc64",
        ("linux", "ppc64le"): "linux-ppc64le",
        ("osx", "arm64"): "osx-arm64",
    }
    _arch_names = {
        32: "x86",
        64: "x86_64",
    }

    if sys_platform == "zos":
        return "zos-z"
    plat = _platform_map.get(sys_platform)
    if plat is not None:
        name = machine.lower()
        subdir = non_x86_subdirs.get((plat, name))
        if subdir is not None:
            return subdir
        if name not in {non_x86 for _, non_x86 in non_x86_subdirs}:
            return f"{plat}-{bits}"
    raise UnsupportedPlatformError(
        f"Unsupported platform {sys_platform!r} ({machine}): "
        "no conda subdir is known for it"
    )


is_windows = platform.system() == "Windows"
