"""Version probes and matching shared by discovery and installation."""

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Literal,
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
        # Check the exit status before decoding stdout. A decode error on its
        # own means the command ran successfully and its version is unknown.
        output = subprocess.check_output([exe, "--version"], stderr=subprocess.DEVNULL)
        return output.decode("utf-8")
    except subprocess.CalledProcessError as error:
        # The public version helpers expose the captured output as text.
        if error.output is not None:
            text = error.output.decode("utf-8", errors="replace")
            error.output = text.replace("\r\n", "\n").replace("\r", "\n")
        return error
    except (OSError, UnicodeDecodeError) as error:
        return error


def _recorded_probe(
    path: Path, observations: Optional[_Observations]
) -> Union[str, Exception, None]:
    if not observations or not os.path.dirname(path):
        return None
    key = _observation_key(path)
    recorded = observations.get(key) if key is not None else None
    if recorded is not None and recorded[0] == _fingerprint(path):
        return recorded[1]
    return None


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


def _named_version(output: str, name: str) -> Optional[str]:
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0] == name:
            return fields[-1]
    return None


def _mamba1_version(output: str) -> Optional[Version]:
    """Read a mamba 1 version line, even if the wrapper prints a banner first."""
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "mamba":
            try:
                return Version(fields[1])
            except InvalidVersion:
                continue
    return None


def _mamba_version(output: str) -> Version:
    standard = _mamba1_version(output)
    if standard is not None:
        return standard
    version = _named_version(output, "mamba")
    if version is not None:
        return Version(version)
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
    version = _named_version(output, "conda")
    return _LEGACY_UNKNOWN_VERSION if version is None else Version(version)


Flavor = Literal["mamba", "micromamba", "conda", "conda_standalone"]
# The old API supplies a minimum Version or None. The new API supplies a
# SpecifierSet, including an empty one for a bare target.
_VersionConstraint = Optional[Union[Version, SpecifierSet]]


@dataclass(frozen=True)
class CondaExecutable:
    """An executable that ensureconda found, installed, or identified.

    Read the fields by name. Later releases may add fields.
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


def _flavor_from_name(executable: _StrPath) -> Optional[Flavor]:
    name = Path(executable).name.lower()
    for suffix in (".exe", ".bat", ".cmd"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    flavors: Dict[str, Flavor] = {
        "mamba": "mamba",
        "micromamba": "micromamba",
        "conda": "conda",
        "conda_standalone": "conda_standalone",
        "conda-standalone": "conda_standalone",
    }
    return flavors.get(name)


def _flavor_from_output(
    output: str, named_flavor: Optional[Flavor]
) -> Optional[Flavor]:
    if named_flavor is not None:
        # The mamba 1 version line identifies the tool even under a conda alias.
        return "mamba" if _mamba1_version(output) is not None else named_flavor
    if _named_version(output, "mamba") is not None:
        return "mamba"  # mamba 1 also reports the version of conda
    if _named_version(output, "conda") is not None:
        return "conda"
    lines = output.strip().splitlines()
    if lines:
        # micromamba prints only its version.
        try:
            Version(lines[0].strip())
        except InvalidVersion:
            return None
        return "micromamba"  # or mamba 2, whose output is identical
    return None


_VERSION_PARSERS: Dict[Flavor, Callable[[str], Version]] = {
    "mamba": _mamba_version,
    "micromamba": _micromamba_version,
    "conda": _conda_version,
    "conda_standalone": _conda_version,
}


def _parse_version(output: str, flavor: Flavor) -> Optional[Version]:
    version = _VERSION_PARSERS[flavor](output)
    if Version(version.base_version) == _LEGACY_UNKNOWN_VERSION:
        return None
    return version


def _probe_version(
    executable: _StrPath, flavor: Flavor, observations: Optional[_Observations]
) -> Optional[Version]:
    if flavor not in _VERSION_PARSERS:
        raise ValueError(f"Unknown executable flavor: {flavor!r}")
    return _parse_version(_version_output(executable, observations), flavor)


def _requests_prerelease(constraint: SpecifierSet) -> bool:
    for clause in constraint:
        if clause.operator != "!=":
            try:
                if Version(clause.version).is_prerelease:
                    return True
            except InvalidVersion:
                # Wildcards and arbitrary equality can contain non-versions.
                pass
    return False


def has_version_constraint(constraint: _VersionConstraint) -> bool:
    """A legacy minimum or a nonempty specifier requires a version probe."""
    return isinstance(constraint, Version) or bool(constraint)


def requirement_text(constraint: _VersionConstraint) -> str:
    if not has_version_constraint(constraint):
        return "any version"
    if isinstance(constraint, Version):
        return f">={constraint}"
    return str(constraint)


class VersionMatch(Enum):
    ACCEPTED = auto()
    UNKNOWN = auto()
    MISMATCH = auto()
    PRERELEASE_NOT_REQUESTED = auto()

    def rejection_text(self, constraint: _VersionConstraint) -> str:
        requirement = requirement_text(constraint)
        if self is VersionMatch.PRERELEASE_NOT_REQUESTED:
            if not has_version_constraint(constraint):
                requirement = "a stable release"
            return f"{requirement} (prereleases must be requested explicitly)"
        return requirement


def match_version(
    version: Optional[Version], constraint: _VersionConstraint
) -> VersionMatch:
    """Match the running executable, whose version can differ from its package."""
    if not has_version_constraint(constraint):
        return VersionMatch.ACCEPTED
    if version is None:
        # The old API compares unrecognized output as version 0.0.0.
        if isinstance(constraint, Version) and _LEGACY_UNKNOWN_VERSION >= constraint:
            return VersionMatch.ACCEPTED
        return VersionMatch.UNKNOWN
    return match_package_version(version, constraint)


def match_package_version(
    version: Version, constraint: _VersionConstraint
) -> VersionMatch:
    """A bare ensure() target selects only stable published versions."""
    if isinstance(constraint, Version):
        return VersionMatch.ACCEPTED if version >= constraint else VersionMatch.MISMATCH
    if constraint is None:
        return VersionMatch.ACCEPTED
    if not constraint.contains(version, prereleases=True):
        return VersionMatch.MISMATCH
    if version.is_prerelease and not _requests_prerelease(constraint):
        return VersionMatch.PRERELEASE_NOT_REQUESTED
    return VersionMatch.ACCEPTED


def version_satisfies(
    version: Optional[Version], constraint: _VersionConstraint
) -> bool:
    return match_version(version, constraint) is VersionMatch.ACCEPTED


def check_executable(
    path: Path,
    flavor: Flavor,
    constraint: _VersionConstraint,
    *,
    observations: Optional[_Observations],
    probe_version: bool = True,
    rejection_level: Optional[int] = logging.INFO,
) -> Optional[CondaExecutable]:
    # CONDA_EXE can name any tool. Other finders search for a specific name.
    candidate_flavor = _flavor_from_name(path) if flavor == "conda" else flavor
    version = None
    constrained = has_version_constraint(constraint)
    if constrained or probe_version:
        try:
            output = None
            if flavor == "conda" and not (
                constrained and candidate_flavor in ("mamba", "micromamba")
            ):
                output = _version_output(path, observations)
                candidate_flavor = _flavor_from_output(output, candidate_flavor)
            if (
                constrained
                and flavor == "conda"
                and candidate_flavor in ("mamba", "micromamba")
            ):
                if rejection_level is not None:
                    logger.log(
                        rejection_level,
                        "Rejected %s (%s): requires conda%s",
                        path,
                        candidate_flavor,
                        requirement_text(constraint),
                    )
                return None
            if candidate_flavor is not None:
                version = (
                    _probe_version(path, candidate_flavor, observations)
                    if output is None
                    else _parse_version(output, candidate_flavor)
                )
        except (
            subprocess.CalledProcessError,
            OSError,
            InvalidVersion,
            UnicodeDecodeError,
        ) as error:
            # Unknown metadata is acceptable without a constraint, but a
            # command that cannot run must never pass a requested probe.
            if constrained or isinstance(
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
    else:
        recorded = _recorded_probe(path, observations)
        if isinstance(recorded, subprocess.CalledProcessError):
            return None
        if flavor == "conda" and isinstance(recorded, str):
            candidate_flavor = _flavor_from_output(recorded, candidate_flavor)
    match = match_version(version, constraint)
    if match is not VersionMatch.ACCEPTED:
        if rejection_level is not None:
            found_version = version if version is not None else "unknown version"
            logger.log(
                rejection_level,
                "Rejected %s %s: requires %s",
                path,
                found_version,
                match.rejection_text(constraint),
            )
        return None
    return CondaExecutable(path, candidate_flavor or flavor, version)
