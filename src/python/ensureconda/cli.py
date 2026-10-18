import json
import sys
import time
from functools import partial
from typing import Any, Optional, Union

import click
from packaging.version import Version

from ensureconda.api import CondaExecutable, ensureconda, ensureconda_info


def _as_loose_version(obj: Union[str, Version, None]) -> Version:
    if isinstance(obj, Version):
        return obj
    elif obj is None:
        return Version("0.0.0")
    else:
        return Version(obj)


_DEFAULT_MIN_CONDA = "4.8.2"
_DEFAULT_MIN_MAMBA = "0.7.3"


class VersionNumber(click.ParamType):
    name = "VersionNumber"

    def convert(
        self, value: Union[str, Version, None], param: Any, ctx: Any
    ) -> Version:
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
    default=Version(_DEFAULT_MIN_CONDA),
    type=VersionNumber(),
    help=f"minimum version of conda to accept (defaults to {_DEFAULT_MIN_CONDA})",
)
@click.option(
    "--min-mamba-version",
    default=Version(_DEFAULT_MIN_MAMBA),
    type=VersionNumber(),
    help=f"minimum version of mamba/micromamba to accept (defaults to {_DEFAULT_MIN_MAMBA})",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="print the executable's path, flavor and version as JSON",
)
def ensureconda_cli(
    mamba: bool,
    micromamba: bool,
    conda: bool,
    conda_exe: bool,
    no_install: bool,
    min_conda_version: Optional[Version],
    min_mamba_version: Optional[Version],
    as_json: bool,
) -> None:
    # Only --json needs the flavor and version; finding them can take an extra
    # `--version` call.
    find = partial(
        ensureconda_info if as_json else ensureconda,
        mamba=mamba,
        micromamba=micromamba,
        conda=conda,
        conda_exe=conda_exe,
        min_mamba_version=min_mamba_version,
        min_conda_version=min_conda_version,
    )
    # We run the loop twice, once to find all the eligible condas without installation
    # and once if you haven't found anything after installation
    exe = find(no_install=True)
    if not exe and not no_install:
        exe = find(no_install=False)
    if exe:
        print("Found compatible executable", file=sys.stderr, flush=True)
        # silly thing to force correct output order
        time.sleep(0.01)
        if isinstance(exe, CondaExecutable):
            print(json.dumps(exe.to_json()), flush=True)
        else:
            print(str(exe), flush=True)
        sys.exit(0)
    else:
        print("Could not find compatible executable", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    ensureconda_cli()
