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
        exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        if any(ext and fpath.lower().endswith(ext.lower()) for ext in exts):
            yield fpath
            return
    else:
        exts = ["", ".exe"]
    for ext in exts:
        yield fpath + ext


def is_exe(fpath: Optional[Path]) -> bool:
    if fpath is None:
        return False
    return os.path.exists(fpath) and os.access(fpath, os.X_OK) and os.path.isfile(fpath)


def absolute_path(path: Path) -> Path:
    try:
        return path.absolute()
    except FileNotFoundError:
        # A removed working directory has no absolute name, but ../tool can
        # still name a file in its parent.
        return path


def which_no_shims(executable: str) -> Optional[str]:
    """Ensure that environment manager tools like pyenv don't break us

    Certain tools like pyenv make use of magic shim directories that contain
    executables that are unsuitable for our use case.

    These executables will exist but may fail on use due to not being in the right
    pyenv execution environment.
    """
    if os.path.dirname(executable):
        return shutil.which(executable)
    search_path = os.environ.get("PATH")
    if not search_path:
        return None
    names = tuple(ext_candidates(executable)) if is_windows else (executable,)
    for directory in search_path.split(os.pathsep):
        if os.path.join(".pyenv", "shims") in directory:
            continue
        for name in names:
            path = absolute_path(Path(directory) / name)
            if is_exe(path):
                return str(path)
    return None


def site_path() -> pathlib.Path:
    override = os.environ.get("ENSURECONDA_DATA_DIR")
    if override:
        return absolute_path(Path(override))
    return absolute_path(Path(appdirs.user_data_dir("ensure-conda")))


def resolve_executable(exe_name: str) -> Iterator[Path]:
    path_prefix = site_path()
    for candidate in ext_candidates(exe_name):
        prefixed_exe = path_prefix / candidate
        if is_exe(prefixed_exe):
            yield prefixed_exe

        # which based exe
        exe = which_no_shims(candidate)
        if exe:
            exe_path = pathlib.Path(exe)
            if is_exe(exe_path):
                yield exe_path


def _conda_exe_from_env() -> Optional[Path]:
    value = os.environ.get("CONDA_EXE")
    # Path("./conda") loses the distinction between a path and a command name.
    if value and not os.path.dirname(value):
        value = which_no_shims(value)
    if value:
        path = absolute_path(Path(value))
        if is_exe(path):
            return path
    return None


def conda_executables() -> Iterator[Path]:
    override = _conda_exe_from_env()
    if override is not None:
        yield override
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
    # conda has no subdir for 32-bit ARM, and win-32 holds x86 builds.
    if sysconfig_platform == "win-arm32" or sysconfig_platform.startswith(
        "mingw_armv7"
    ):
        raise UnsupportedPlatformError(
            f"Unsupported platform 'win32' ({sysconfig_platform}): "
            "no conda subdir is known for it"
        )
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
        ("linux", "armv6l"): "linux-armv6l",
        ("linux", "armv7l"): "linux-armv7l",
        # What a 64-bit kernel reports to a process with the 32-bit
        # personality, as in an arm/v7 container on an arm64 host.
        ("linux", "armv8l"): "linux-armv7l",
        ("linux", "ppc64"): "linux-ppc64",
        ("linux", "ppc64le"): "linux-ppc64le",
        ("linux", "riscv64"): "linux-riscv64",
        ("linux", "s390x"): "linux-s390x",
        ("osx", "arm64"): "osx-arm64",
    }
    x86_machines = {"x86_64", "amd64", "i386", "i486", "i586", "i686", "x86"}

    if sys_platform == "zos":
        return "zos-z"
    plat = _platform_map.get(sys_platform)
    if plat is not None:
        name = machine.lower()
        # platform.machine() returns an empty string when it can't tell.
        if not name or name in x86_machines:
            return f"{plat}-{bits}"
        subdir = non_x86_subdirs.get((plat, name))
        if subdir is not None:
            return subdir
    raise UnsupportedPlatformError(
        f"Unsupported platform {sys_platform!r} ({machine}): "
        "no conda subdir is known for it"
    )


is_windows = platform.system() == "Windows"
