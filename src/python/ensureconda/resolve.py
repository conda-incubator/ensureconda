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
    import struct

    return subdir_for(sys.platform, platform.machine(), 8 * struct.calcsize("P"))


def subdir_for(sys_platform: str, machine: str, bits: int) -> str:
    """Map a platform to its conda subdir, following conda.base.context."""
    platform_map = {
        "linux2": "linux",
        "linux": "linux",
        "darwin": "osx",
        "win32": "win",
        "zos": "zos",
    }
    non_x86_machines = {
        "armv6l",
        "armv7l",
        "aarch64",
        "arm64",
        "ppc64",
        "ppc64le",
        "riscv64",
        "s390x",
    }
    plat = platform_map.get(sys_platform)
    if plat is None:
        raise RuntimeError(
            f"Unsupported platform {sys_platform!r} ({machine}): "
            "no conda subdir is known for it"
        )
    if plat == "win":
        # Unlike conda, keep using x86 builds on Windows on ARM, where they run
        # under emulation: conda-forge publishes no win-arm64 conda-standalone.
        return f"win-{bits}"
    machine = machine.lower()
    if machine in non_x86_machines:
        return f"{plat}-{machine}"
    if plat == "zos":
        return "zos-z"
    return f"{plat}-{bits}"


is_windows = platform.system() == "Windows"
