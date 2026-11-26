"""Version probes and matching shared by discovery and installation."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Literal,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

if TYPE_CHECKING or sys.version_info >= (3, 9):
    _StrPath = Union[str, os.PathLike[str]]
else:
    # PathLike is not subscriptable at runtime on Python 3.8.
    _StrPath = Union[str, os.PathLike]


# The old version helpers use 0.0.0 as a sentinel for unrecognized output.
# Keep their return values compatible; new API probes report None for any
# version with base version 0, such as conda's 0.0.0.dev0+placeholder.
_LEGACY_UNKNOWN_VERSION = Version("0.0.0")

# Device, inode, size, and modification and change times in nanoseconds.
_Fingerprint = Tuple[int, int, int, int, int]
# The output of `--version`, or the error from running it, for a fingerprint.
_Observation = Tuple[_Fingerprint, Union[str, Exception]]
# The observations of one call, by absolute path, reused for files that don't
# change during it. Reuse assumes that what a wrapper or script runs doesn't
# change either. None runs every probe.
_Observations = Dict[str, _Observation]


def _fingerprint(path: _StrPath) -> Optional[_Fingerprint]:
    # On Windows st_ctime is the creation time, which a change of contents or
    # attributes leaves alone, so nothing is reused there.
    if os.name == "nt":
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _observation_key(path: _StrPath) -> Optional[str]:
    try:
        return os.path.abspath(path)
    except OSError:
        # The working directory was removed.
        return None


def _run_version(exe: _StrPath) -> Union[str, Exception]:
    try:
        return subprocess.check_output(
            [exe, "--version"], stderr=subprocess.DEVNULL, encoding="utf-8"
        )
    except (subprocess.CalledProcessError, OSError, UnicodeDecodeError) as error:
        return error


def _version_output(exe: _StrPath, observations: Optional[_Observations]) -> str:
    key: Optional[str] = None
    current: Optional[_Fingerprint] = None
    # Running a bare name looks it up on PATH, so it may run another file.
    if observations is not None and os.path.dirname(exe):
        key, current = _observation_key(exe), _fingerprint(exe)
    result: Union[str, Exception]
    if observations is None or key is None or current is None:
        result = _run_version(exe)
    elif key in observations and observations[key][0] == current:
        result = observations[key][1]
    else:
        result = _run_version(exe)
        # A failure to start, such as a busy text file, may not happen again.
        if not isinstance(result, OSError):
            # A change during the probe moves the change time past this record.
            observations[key] = (current, result)
    if isinstance(result, Exception):
        raise result
    return result


def detach_observation(
    observations: Optional[_Observations], path: _StrPath
) -> Optional[_Observation]:
    """Remove the result for path, returning it if the file still matches it."""
    if observations is None:
        return None
    key = _observation_key(path)
    recorded = None if key is None else observations.pop(key, None)
    if recorded is None or recorded[0] != _fingerprint(path):
        return None
    return recorded


def attach_observation(
    observations: Optional[_Observations],
    path: _StrPath,
    recorded: Optional[_Observation],
) -> None:
    """Keep a detached result for the file it was renamed to at path."""
    if observations is None or recorded is None:
        return
    key, after = _observation_key(path), _fingerprint(path)
    # A rename updates the change time only.
    if key is not None and after is not None and recorded[0][:4] == after[:4]:
        observations[key] = (after, recorded[1])


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
    return _mamba_version(_version_output(exe, None))


def _mamba_version(output: str) -> Version:
    for line in output.strip().splitlines(keepends=False):
        if line.startswith("mamba"):
            return Version(line.split()[-1])
    # Not v1 style output, so fall back to micromamba version detection
    return _micromamba_version(output)


def determine_micromamba_version(exe: _StrPath) -> Version:
    return _micromamba_version(_version_output(exe, None))


def _micromamba_version(output: str) -> Version:
    for line in output.strip().splitlines(keepends=False):
        return Version(line.split()[-1])
    return _LEGACY_UNKNOWN_VERSION


def determine_conda_version(exe: _StrPath) -> Version:
    return _conda_version(_version_output(exe, None))


def _conda_version(output: str) -> Version:
    for line in output.strip().splitlines(keepends=False):
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


def _probe_version(
    executable: _StrPath, flavor: Flavor, observations: Optional[_Observations]
) -> Optional[Version]:
    parse: Callable[[str], Version]
    if flavor == "mamba":
        parse = _mamba_version
    elif flavor == "micromamba":
        parse = _micromamba_version
    elif flavor in ("conda", "conda_standalone"):
        parse = _conda_version
    else:
        raise ValueError(f"Unknown executable flavor: {flavor!r}")
    version = parse(_version_output(executable, observations))
    if Version(version.base_version) == _LEGACY_UNKNOWN_VERSION:
        return None
    return version


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
    observations: Optional[_Observations],
    probe_version: bool = True,
    rejection_level: Optional[int] = logging.INFO,
) -> Optional[CondaExecutable]:
    version = None
    if constraint is not None or probe_version:
        try:
            version = _probe_version(path, flavor, observations)
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
