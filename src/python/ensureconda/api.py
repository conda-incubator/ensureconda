import errno
import os
import re
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Literal,
    NamedTuple,
    Optional,
    Tuple,
    cast,
)

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from ensureconda.installer import (
    NotPublishedError,
    install_conda_exe,
    install_micromamba,
)
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


Flavor = Literal["mamba", "micromamba", "conda", "conda_standalone"]
_VersionCheck = Optional[Callable[[Version], bool]]


class CondaExecutable(NamedTuple):
    """An executable that ensureconda found, installed, or identified."""

    path: Path
    flavor: Flavor
    # None when unmeasured, or when `--version` fails or has no known version.
    version: Optional[Version]

    def to_json(self) -> Dict[str, Optional[str]]:
        """Return a JSON-serializable dict, as printed by ``ensureconda --json``."""
        return {
            "path": str(self.path),
            "flavor": self.flavor,
            "version": None if self.version is None else str(self.version),
        }


def _probe_version(executable: "StrPath", flavor: Flavor) -> Optional[Version]:
    probes: Dict[Flavor, Callable[["StrPath"], Version]] = {
        "mamba": determine_mamba_version,
        "micromamba": determine_micromamba_version,
        "conda": determine_conda_version,
        "conda_standalone": determine_conda_version,
    }
    try:
        return probes[flavor](executable)
    except (subprocess.CalledProcessError, OSError, InvalidVersion, UnicodeDecodeError):
        return None


def _known_version(version: Optional[Version]) -> Optional[Version]:
    # The determine_*_version functions return 0.0.0 for unrecognized output.
    return None if version is None or version == Version("0.0.0") else version


def _flavor_from_name(executable: "StrPath") -> Optional[Flavor]:
    name = Path(executable).name.lower()
    for suffix in (".exe", ".bat", ".cmd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    flavors: Dict[str, Flavor] = {
        "mamba": "mamba",
        "micromamba": "micromamba",
        "conda": "conda",
        "conda_standalone": "conda_standalone",
    }
    return flavors.get(name)


def _flavor_from_output(executable: "StrPath") -> Optional[Flavor]:
    try:
        output = subprocess.check_output([executable, "--version"], encoding="utf-8")
    except (subprocess.CalledProcessError, OSError, UnicodeDecodeError):
        return None
    lines = output.strip().splitlines()
    if any(line.startswith("mamba") for line in lines):
        return "mamba"  # mamba 1 also reports the version of conda
    if lines and lines[0].startswith("conda"):
        return "conda"
    if lines:
        # micromamba prints only its version.
        try:
            Version(lines[0].strip())
        except InvalidVersion:
            return None
        return "micromamba"  # or mamba 2, whose output is identical
    return None


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
            try:
                maybe_exe = install_micromamba()
            except NotPublishedError as error:
                # conda-standalone is published for platforms that micromamba
                # isn't, for example linux-s390x in the anaconda channel.
                remaining = [
                    name
                    for enabled, name in [
                        (conda, "conda"),
                        (conda_exe, "conda-standalone"),
                    ]
                    if enabled
                ]
                if not remaining:
                    raise
                print(
                    f"{error}; trying {' or '.join(remaining)} instead",
                    file=sys.stderr,
                )
                maybe_exe = None
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


_FLAVORS: Tuple[Flavor, ...] = ("mamba", "micromamba", "conda", "conda_standalone")


def _parse_target(target: str) -> Tuple[Flavor, _VersionCheck]:
    match = re.fullmatch(r"([a-z_]+)\s*(.*)", target.strip())
    if match is None or match[1] not in _FLAVORS:
        raise ValueError(
            f"Unknown target {target!r}; expected mamba, micromamba, conda, "
            "or conda_standalone, optionally followed by version constraints"
        )
    flavor = cast(Flavor, match[1])
    constraint = match[2].strip()
    if not constraint:
        return flavor, None
    try:
        lower_bound = re.fullmatch(r">=\s*([^,]+)", constraint)
        if lower_bound is not None:
            # Preserve the legacy API's Version comparison, including local
            # version labels, which SpecifierSet disallows in >= bounds.
            minimum = Version(lower_bound[1])
            return flavor, lambda version: version >= minimum
        specifier = SpecifierSet(constraint)
    except (InvalidSpecifier, InvalidVersion) as error:
        raise ValueError(f"Invalid version constraint in target {target!r}") from error
    return flavor, lambda version: specifier.contains(version, prereleases=True)


def identify(executable: "StrPath") -> CondaExecutable:
    """Describe a conda, mamba, micromamba, or conda-standalone executable.

    The flavor comes from the file name, as for the executables that
    :func:`ensureconda` finds. For other names it comes from the shape of the
    ``--version`` output instead, which can't distinguish micromamba from
    mamba 2, nor conda from conda-standalone; those are reported as
    "micromamba" and "conda".

    Raises FileNotFoundError if the executable doesn't exist, PermissionError
    if it isn't executable, and ValueError if it can't be recognized.
    """
    path = Path(executable)
    if not path.is_file():
        raise FileNotFoundError(errno.ENOENT, "No such executable", str(executable))
    if not os.access(path, os.X_OK):
        raise PermissionError(errno.EACCES, "Not an executable file", str(executable))
    flavor = _flavor_from_name(executable) or _flavor_from_output(executable)
    if flavor is None:
        raise ValueError(f"{executable} is not a recognized conda executable")
    version = _known_version(_probe_version(executable, flavor))
    return CondaExecutable(path, flavor, version)
