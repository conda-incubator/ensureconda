import subprocess
import sys
from functools import partial
from typing import TYPE_CHECKING, Callable, Optional

from packaging.version import InvalidVersion, Version

from ensureconda.installer import install_conda_exe, install_micromamba
from ensureconda.resolve import (
    conda_executables,
    conda_standalone_executables,
    mamba_executables,
    micromamba_executables,
)

if TYPE_CHECKING:
    from _typeshed import StrPath


def determine_mamba_version(exe: "StrPath") -> Version:
    """Determine the version of mamba on the given executable.

    Typical output of `mamba --version` for v1 is:
    ```
    mamba 1.4.7
    conda 23.5.0
    ```

    Typical output of `mamba --version` for v2 is identical to micromamba:
    ```
    2.0.8
    ```
    """
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("mamba"):
            return Version(line.split()[-1])
    # Not v1 style output, so fall back to micromamba version detection
    return determine_micromamba_version(exe)


def determine_micromamba_version(exe: "StrPath") -> Version:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        return Version(line.split()[-1])
    return Version("0.0.0")


def determine_conda_version(exe: "StrPath") -> Version:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("conda"):
            return Version(line.split()[-1])
    return Version("0.0.0")


def ensureconda(
    *,
    mamba: bool = True,
    micromamba: bool = True,
    conda: bool = True,
    conda_exe: bool = True,
    no_install: bool = False,
    min_conda_version: Optional[Version] = None,
    min_mamba_version: Optional[Version] = None,
) -> "Optional[StrPath]":
    """Ensures that conda is installed

    Parameters
    ----------
    mamba:
        Allow resolving mamba
    micromamba:
        Allow resolving micromamba.  Micromamba implements a subset of features of conda su
        it may not be the correct choice in all cases.
    conda:
        Allow resolving conda.
    conda_exe
        Allow resolving conda-standalong
    min_conda_version:
        Minimum version of conda required
    min_mamba_version:
        Minimum version of mamba required

    Returns the path to a conda executable.
    """

    def version_constraint_met(
        executable: "StrPath",
        min_version: Optional[Version],
        version_fn: Callable[["StrPath"], Version],
    ) -> bool:
        if min_version is None:
            return True
        try:
            version = version_fn(executable)
        except subprocess.CalledProcessError as error:
            reason = f"`--version` exited with status {error.returncode}"
        except (OSError, InvalidVersion, UnicodeDecodeError) as error:
            reason = str(error)
        else:
            return version >= min_version
        # A broken candidate shouldn't prevent finding a working one.
        print(
            f"Skipping {executable}: could not determine its version ({reason})",
            file=sys.stderr,
        )
        return False

    conda_constraints_met = partial(
        version_constraint_met,
        min_version=min_conda_version,
        version_fn=determine_conda_version,
    )
    mamba_constraints_met = partial(
        version_constraint_met,
        min_version=min_mamba_version,
        version_fn=determine_mamba_version,
    )
    micromamba_constraints_met = partial(
        version_constraint_met,
        min_version=min_mamba_version,
        version_fn=determine_micromamba_version,
    )

    if mamba:
        for exe in mamba_executables():
            if exe and mamba_constraints_met(exe):
                return exe
    if micromamba:
        for exe in micromamba_executables():
            if exe and micromamba_constraints_met(exe):
                return exe
        if not no_install:
            maybe_exe = install_micromamba()
            if maybe_exe is not None and micromamba_constraints_met(maybe_exe):
                return maybe_exe
    if conda:
        for exe in conda_executables():
            if exe and conda_constraints_met(exe):
                return exe
    if conda_exe:
        for exe in conda_standalone_executables():
            if exe and conda_constraints_met(exe):
                return exe
        if not no_install:
            maybe_exe = install_conda_exe()
            if maybe_exe is not None and conda_constraints_met(maybe_exe):
                return maybe_exe
    return None
