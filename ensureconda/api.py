import subprocess
from distutils.version import LooseVersion
from os import PathLike
from typing import Optional

from ensureconda.installer import install_conda_exe, install_micromamba
from ensureconda.resolve import (
    conda_executables,
    conda_standalone_executables,
    mamba_executables,
    micromamba_executables,
)


def determine_mamba_version(exe: PathLike) -> LooseVersion:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("mamba"):
            return LooseVersion(line.split()[-1])
    return LooseVersion("0.0.0")


def determine_micromamba_version(exe: PathLike) -> LooseVersion:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        return LooseVersion(line.split()[-1])
    return LooseVersion("0.0.0")


def determine_conda_version(exe: PathLike) -> LooseVersion:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("conda"):
            return LooseVersion(line.split()[-1])
    return LooseVersion("0.0.0")


def ensureconda(
    *,
    mamba: bool = True,
    micromamba: bool = True,
    conda: bool = True,
    conda_exe: bool = True,
    no_install: bool = False,
    min_conda_version: Optional[LooseVersion] = None,
    min_mamba_version: Optional[LooseVersion] = None,
) -> Optional[PathLike]:
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
    if mamba:
        for exe in mamba_executables():
            if exe and (
                (min_mamba_version is None)
                or (determine_mamba_version(exe) >= min_mamba_version)
            ):
                return exe
    if micromamba:
        for exe in micromamba_executables():
            if exe and (
                (min_mamba_version is None)
                or (determine_micromamba_version(exe) >= min_mamba_version)
            ):
                return exe
        if not no_install:
            return install_micromamba()
    if conda:
        for exe in conda_executables():
            if exe and (
                (min_conda_version is None)
                or (determine_conda_version(exe) >= min_conda_version)
            ):
                return exe
    if conda_exe:
        for exe in conda_standalone_executables():
            if exe and (
                (min_conda_version is None)
                or (determine_conda_version(exe) >= min_conda_version)
            ):
                return exe
        if not no_install:
            return install_conda_exe()
    return None
