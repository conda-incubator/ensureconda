"""Version probes and matching shared by discovery and installation."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Literal, NamedTuple, Optional, Union

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

if TYPE_CHECKING or sys.version_info >= (3, 9):
    _StrPath = Union[str, os.PathLike[str]]
else:
    # PathLike is not subscriptable at runtime on Python 3.8.
    _StrPath = Union[str, os.PathLike]


# The old version helpers use 0.0.0 as a sentinel for unrecognized output.
# Keep their return values compatible; new API probes translate it to None.
_LEGACY_UNKNOWN_VERSION = Version("0.0.0")


def determine_mamba_version(exe: _StrPath) -> Version:
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
    out = subprocess.check_output(
        [exe, "--version"], stderr=subprocess.DEVNULL, encoding="utf-8"
    ).strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("mamba"):
            return Version(line.split()[-1])
    # Not v1 style output, so fall back to micromamba version detection
    return determine_micromamba_version(exe)


def determine_micromamba_version(exe: _StrPath) -> Version:
    out = subprocess.check_output(
        [exe, "--version"], stderr=subprocess.DEVNULL, encoding="utf-8"
    ).strip()
    for line in out.splitlines(keepends=False):
        return Version(line.split()[-1])
    return _LEGACY_UNKNOWN_VERSION


def determine_conda_version(exe: _StrPath) -> Version:
    out = subprocess.check_output(
        [exe, "--version"], stderr=subprocess.DEVNULL, encoding="utf-8"
    ).strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("conda"):
            return Version(line.split()[-1])
    return _LEGACY_UNKNOWN_VERSION


Flavor = Literal["mamba", "micromamba", "conda", "conda_standalone"]
# Version is an old-API minimum; SpecifierSet is a new-API constraint.
_VersionConstraint = Optional[Union[Version, SpecifierSet]]


class CondaExecutable(NamedTuple):
    """An executable that ensureconda found, installed, or identified.

    Later releases may add fields.
    """

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


def _probe_version(executable: _StrPath, flavor: Flavor) -> Optional[Version]:
    if flavor == "mamba":
        version = determine_mamba_version(executable)
    elif flavor == "micromamba":
        version = determine_micromamba_version(executable)
    elif flavor in ("conda", "conda_standalone"):
        version = determine_conda_version(executable)
    else:
        raise ValueError(f"Unknown executable flavor: {flavor!r}")
    return None if version == _LEGACY_UNKNOWN_VERSION else version


def version_satisfies(
    version: Optional[Version], constraint: _VersionConstraint
) -> bool:
    if isinstance(constraint, Version):
        # Preserve the old API's unknown-version sentinel and direct comparison.
        return (
            version if version is not None else _LEGACY_UNKNOWN_VERSION
        ) >= constraint
    if constraint is None:
        return True
    return version is not None and constraint.contains(version, prereleases=True)


def requirement_text(constraint: _VersionConstraint) -> str:
    return (
        f">={constraint}"
        if isinstance(constraint, Version)
        else str(constraint or "any version")
    )


def check_executable(
    path: Path,
    flavor: Flavor,
    constraint: _VersionConstraint,
    *,
    probe_version: bool = True,
    rejection_level: Optional[int] = logging.INFO,
) -> Optional[CondaExecutable]:
    version = None
    if constraint is not None or probe_version:
        try:
            version = _probe_version(path, flavor)
        except (
            subprocess.CalledProcessError,
            OSError,
            InvalidVersion,
            UnicodeDecodeError,
        ) as error:
            # Unknown metadata is acceptable without a constraint, but a
            # command that cannot run must never pass a requested probe.
            if constraint is not None or isinstance(
                error, (subprocess.CalledProcessError, OSError)
            ):
                reason = (
                    f"`--version` exited with status {error.returncode}"
                    if isinstance(error, subprocess.CalledProcessError)
                    else str(error)
                )
                if rejection_level is not None:
                    logger.warning(
                        "Skipping %s: could not determine its version (%s)",
                        path,
                        reason,
                    )
                return None
    if not version_satisfies(version, constraint):
        requirement = requirement_text(constraint)
        if rejection_level is not None:
            found_version = version if version is not None else "unknown version"
            logger.log(
                rejection_level,
                "Rejected %s %s: requires %s",
                path,
                found_version,
                requirement,
            )
        return None
    return CondaExecutable(path, flavor, version)
