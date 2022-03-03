import sys
import time

from distutils.version import LooseVersion
from typing import Any, Optional, Union

import click

from ensureconda.api import ensureconda


def _as_loose_version(obj: Union[str, LooseVersion, None]) -> LooseVersion:
    if isinstance(obj, LooseVersion):
        return obj
    elif obj is None:
        return LooseVersion("0.0.0")
    else:
        return LooseVersion(obj)


_DEFAULT_MIN_CONDA = "4.8.2"
_DEFAULT_MIN_MAMBA = "0.7.3"


class VersionNumber(click.ParamType):
    name = "VersionNumber"

    def convert(
        self, value: Union[str, LooseVersion, None], param: Any, ctx: Any
    ) -> LooseVersion:
        return _as_loose_version(value)


@click.command(help="Ensures that a conda/mamba is installed.")
@click.option("--mamba/--no-mamba", default=True, help="search for mamba")
@click.option(
    "--micromamba/--no-micromamba",
    default=True,
    help="search for micromamba, install if not present",
)
@click.option("--conda/--no-conda", default=True, help="search for conda")
@click.option(
    "--conda-exe/--no-conda-exe",
    default=True,
    help="search for conda.exe / conda-standalone, install if not present",
)
@click.option(
    "--no-install",
    is_flag=True,
    help="don't install conda/mamba if no version can be discovered",
)
@click.option(
    "--min-conda-version",
    default=LooseVersion(_DEFAULT_MIN_CONDA),
    type=VersionNumber(),
    help=f"minimum version of conda to accept (defaults to {_DEFAULT_MIN_CONDA})",
)
@click.option(
    "--min-mamba-version",
    default=LooseVersion(_DEFAULT_MIN_MAMBA),
    type=VersionNumber(),
    help=f"minimum version of mamba/micromamba to accept (defaults to {_DEFAULT_MIN_MAMBA})",
)
def ensureconda_cli(
    mamba: bool,
    micromamba: bool,
    conda: bool,
    conda_exe: bool,
    no_install: bool,
    min_conda_version: Optional[LooseVersion],
    min_mamba_version: Optional[LooseVersion],
) -> None:
    # We run the loop twice, once to find all the eligible condas without installation
    # and once if you haven't found anything after installation
    exe = ensureconda(
        mamba=mamba,
        micromamba=micromamba,
        conda=conda,
        conda_exe=conda_exe,
        no_install=True,
        min_mamba_version=min_mamba_version,
        min_conda_version=min_conda_version,
    )
    if not exe and not no_install:
        exe = ensureconda(
            mamba=mamba,
            micromamba=micromamba,
            conda=conda,
            conda_exe=conda_exe,
            no_install=False,
            min_mamba_version=min_mamba_version,
            min_conda_version=min_conda_version,
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


if __name__ == "__main__":
    ensureconda_cli()
