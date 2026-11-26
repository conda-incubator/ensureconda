import json
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import click
import requests
from packaging.version import InvalidVersion, Version

from ensureconda.api import CondaExecutable, _ensure, _legacy_targets
from ensureconda.installer import (
    DownloadedExecutableError,
    ExecutableReplacementError,
    NotPublishedError,
)
from ensureconda.resolve import UnsupportedPlatformError


def _as_loose_version(obj: Union[str, Version, None]) -> Version:
    if isinstance(obj, Version):
        return obj
    elif obj is None:
        return Version("0.0.0")
    else:
        return Version(obj)


_DEFAULT_MIN_CONDA = "4.8.2"
_DEFAULT_MIN_MAMBA = "0.7.3"


# ParamType is generic only in click 8.4 and later, which require Python 3.10.
class VersionNumber(click.ParamType):  # type: ignore[type-arg, unused-ignore]
    name = "VersionNumber"

    def convert(
        self, value: Union[str, Version, None], param: Any, ctx: Any
    ) -> Version:
        try:
            return _as_loose_version(value)
        except InvalidVersion:
            self.fail(f"{value!r} is not a valid version", param, ctx)


@contextmanager
def _cli_logging(verbosity: int) -> Iterator[None]:
    logger = logging.getLogger("ensureconda")
    level, propagate = logger.level, logger.propagate
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(
        logging.WARNING
        if verbosity == 0
        else logging.INFO if verbosity == 1 else logging.DEBUG
    )
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
        handler.close()


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
    "--force-install",
    is_flag=True,
    help="skip existing executables and download a fresh copy",
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
@click.option(
    "--verbosity",
    "-v",
    type=click.IntRange(0, 3),
    default=1,
    show_default=True,
    help="0: warnings; 1: decisions; 2-3: debug details",
)
def ensureconda_cli(
    mamba: bool,
    micromamba: bool,
    conda: bool,
    conda_exe: bool,
    no_install: bool,
    force_install: bool,
    min_conda_version: Optional[Version],
    min_mamba_version: Optional[Version],
    as_json: bool,
    verbosity: int,
) -> None:
    with _cli_logging(verbosity):
        if force_install and no_install:
            raise click.UsageError(
                "--force-install cannot be combined with --no-install"
            )
        if force_install and not (micromamba or conda_exe):
            raise click.UsageError(
                "--force-install requires --micromamba or --conda-exe"
            )

        exe: Union[CondaExecutable, Path, None]
        try:
            # Keep old-API minimums when calling the resolver directly.
            targets = _legacy_targets(
                mamba=mamba,
                micromamba=micromamba,
                conda=conda,
                conda_exe=conda_exe,
                min_conda_version=min_conda_version,
                min_mamba_version=min_mamba_version,
            )
            exe = _ensure(
                targets,
                install=(
                    "force" if force_install else "never" if no_install else "fallback"
                ),
                probe_version=as_json,
            )
            if exe is not None and not as_json:
                exe = exe.path
        except requests.RequestException as error:
            # Each attempt has already been reported on stderr, so end with
            # one line instead of a traceback.
            detail = str(error)
            reason = type(error).__name__
            if detail:
                reason += f": {detail}"
            raise click.ClickException(f"download failed: {reason}")
        except (
            DownloadedExecutableError,
            ExecutableReplacementError,
            NotPublishedError,
            UnsupportedPlatformError,
        ) as error:
            raise click.ClickException(str(error))
        if exe:
            logging.getLogger("ensureconda.cli").info("Found compatible executable")
            # silly thing to force correct output order
            time.sleep(0.01)
            if isinstance(exe, CondaExecutable):
                print(json.dumps(exe.to_json()), flush=True)
            else:
                print(str(exe), flush=True)
            sys.exit(0)
        else:
            logging.getLogger("ensureconda.cli").error(
                "Could not find compatible executable"
            )
            sys.exit(1)


if __name__ == "__main__":
    ensureconda_cli()
