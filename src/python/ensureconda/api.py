import subprocess

from distutils.version import LooseVersion
from functools import partial
from typing import TYPE_CHECKING, Callable, Optional

from ensureconda.installer import install_conda_exe, install_micromamba
from ensureconda.resolve import (
    conda_executables,
    conda_standalone_executables,
    mamba_executables,
    micromamba_executables,
)


if TYPE_CHECKING:
    from _typeshed import AnyPath


def determine_mamba_version(exe: "AnyPath") -> LooseVersion:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("mamba"):
            return LooseVersion(line.split()[-1])
    return LooseVersion("0.0.0")


def determine_micromamba_version(exe: "AnyPath") -> LooseVersion:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        return LooseVersion(line.split()[-1])
    return LooseVersion("0.0.0")


def determine_conda_version(exe: "AnyPath") -> LooseVersion:
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
) -> Optional["AnyPath"]:
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
        executable: "AnyPath",
        min_version: Optional[LooseVersion],
        version_fn: Callable[["AnyPath"], LooseVersion],
    ) -> bool:
        return (min_version is None) or (version_fn(executable) >= min_version)

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
