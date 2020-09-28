from os import PathLike
from typing import Optional

from ensureconda.installer import install_conda_exe, install_micromamba
from ensureconda.resolve import (
    conda_executables,
    conda_standalone_executables,
    mamba_executables,
    micromamba_executables,
    safe_next,
)


def ensureconda(
    *,
    mamba: bool = True,
    micromamba: bool = True,
    conda: bool = True,
    conda_exe: bool = True,
    no_install: bool = False,
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

    Returns the path to a conda executable.
    """
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
