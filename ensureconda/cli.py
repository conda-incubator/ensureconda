import sys
import time

import click

from ensureconda.api import ensureconda


@click.command(help="Ensures that a conda/mamba is installed.")
@click.option("--mamba/--no-mamba", default=True, help="Search for mamba")
@click.option(
    "--micromamba/--no-micromamba",
    default=True,
    help="Search for mamba/micromamba, Install if not present",
)
@click.option("--conda/--no-conda", default=True, help="Search for conda")
@click.option(
    "--conda-exe/--no-conda-exe",
    default=True,
    help="Search for conda.exe / conda-standalone, install if not present",
)
@click.option("--no-install", is_flag=True)
def ensureconda_cli(mamba, micromamba, conda, conda_exe, no_install):
    # We run the loop twice, once to find all the eligible condas without installation
    # and once if you haven't found anything after installation
    exe = ensureconda(
        mamba=mamba,
        micromamba=micromamba,
        conda=conda,
        conda_exe=conda_exe,
        no_install=True,
    )
    if not exe and not no_install:
        exe = ensureconda(
            mamba=mamba,
            micromamba=micromamba,
            conda=conda,
            conda_exe=conda_exe,
            no_install=False,
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
