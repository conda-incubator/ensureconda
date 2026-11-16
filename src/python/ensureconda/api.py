import errno
import os
import subprocess
import sys
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ensureconda.installer import (
    NotPublishedError,
    install_conda_exe,
    install_micromamba,
)
from ensureconda.resolve import (
    UnsupportedPlatformError,
    conda_executables,
    conda_standalone_executables,
    is_exe,
    mamba_executables,
    micromamba_executables,
    platform_subdir,
)

if TYPE_CHECKING:
    from _typeshed import StrPath


# The old version helpers use 0.0.0 as a sentinel for unrecognized output.
# Keep their return values compatible; new API probes translate it to None.
_LEGACY_UNKNOWN_VERSION = Version("0.0.0")


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
    return _LEGACY_UNKNOWN_VERSION


def determine_conda_version(exe: "StrPath") -> Version:
    out = subprocess.check_output([exe, "--version"], encoding="utf-8").strip()
    for line in out.splitlines(keepends=False):
        if line.startswith("conda"):
            return Version(line.split()[-1])
    return _LEGACY_UNKNOWN_VERSION


Flavor = Literal["mamba", "micromamba", "conda", "conda_standalone"]
InstallPolicy = Literal["never", "fallback", "per-target", "force"]
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


_FLAVORS: Tuple[Flavor, ...] = ("mamba", "micromamba", "conda", "conda_standalone")
_TARGET_FLAVORS = {canonicalize_name(flavor): flavor for flavor in _FLAVORS}
_INSTALL_POLICIES: Tuple[InstallPolicy, ...] = (
    "never",
    "fallback",
    "per-target",
    "force",
)


def _probe_version(executable: "StrPath", flavor: Flavor) -> Optional[Version]:
    if flavor == "mamba":
        version = determine_mamba_version(executable)
    elif flavor == "micromamba":
        version = determine_micromamba_version(executable)
    elif flavor in ("conda", "conda_standalone"):
        version = determine_conda_version(executable)
    else:
        raise ValueError(f"Unknown executable flavor: {flavor!r}")
    return None if version == _LEGACY_UNKNOWN_VERSION else version


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
        "conda-standalone": "conda_standalone",
    }
    return flavors.get(name)


def _probe_flavor(executable: "StrPath") -> Optional[Flavor]:
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


def _legacy_targets(
    *,
    mamba: bool,
    micromamba: bool,
    conda: bool,
    conda_exe: bool,
    min_conda_version: Optional[Version],
    min_mamba_version: Optional[Version],
) -> List[Tuple[Flavor, Optional[Version]]]:
    """Translate the old API and CLI flags without stringifying minimums."""
    targets: List[Tuple[Flavor, Optional[Version]]] = []
    if mamba:
        targets.append(("mamba", min_mamba_version))
    if micromamba:
        targets.append(("micromamba", min_mamba_version))
    if conda:
        targets.append(("conda", min_conda_version))
    if conda_exe:
        targets.append(("conda_standalone", min_conda_version))
    return targets


def ensureconda(
    *,
    mamba: bool = True,
    micromamba: bool = True,
    conda: bool = True,
    conda_exe: bool = True,
    no_install: bool = False,
    min_conda_version: Optional[Version] = None,
    min_mamba_version: Optional[Version] = None,
) -> Optional[Path]:
    """Old API returning an executable path, or None.

    The booleans select targets in the legacy order: mamba, micromamba, conda,
    conda_standalone. Search and attempt installation for each target before
    moving on. Version probes run only when a minimum version is requested.

    For an ordered target sequence and executable metadata, use :func:`ensure`.
    """
    targets = _legacy_targets(
        mamba=mamba,
        micromamba=micromamba,
        conda=conda,
        conda_exe=conda_exe,
        min_conda_version=min_conda_version,
        min_mamba_version=min_mamba_version,
    )
    result = _ensure(
        targets,
        install="never" if no_install else "per-target",
        probe_version=False,
    )
    return None if result is None else result.path


def _parse_target(target: str) -> Tuple[Flavor, Optional[SpecifierSet]]:
    """Parse a tool name and optional version constraints.

    >>> _parse_target("Conda-Standalone")
    ('conda_standalone', None)
    >>> flavor, constraint = _parse_target("micromamba>=2,<3")
    >>> flavor
    'micromamba'
    >>> [constraint.contains(v) for v in ["1.9", "2.5", "3.0"]]
    [False, True, False]
    >>> _, constraint = _parse_target("conda==25.*")
    >>> constraint.contains("25.1")
    True
    >>> _parse_target("micromamba>=1.0+local")
    Traceback (most recent call last):
        ...
    ValueError: Invalid target 'micromamba>=1.0+local'
    >>> _parse_target("python")
    Traceback (most recent call last):
        ...
    ValueError: Unknown target 'python'; expected mamba, micromamba, conda, or conda_standalone, optionally followed by version constraints
    """
    try:
        requirement = Requirement(target)
    except InvalidRequirement as error:
        raise ValueError(f"Invalid target {target!r}") from error

    flavor = _TARGET_FLAVORS.get(canonicalize_name(requirement.name))
    if flavor is None:
        raise ValueError(
            f"Unknown target {target!r}; expected mamba, micromamba, conda, "
            "or conda_standalone, optionally followed by version constraints"
        )
    if requirement.extras or requirement.marker is not None or requirement.url:
        raise ValueError(f"Invalid target {target!r}")
    return flavor, requirement.specifier or None


def ensure(
    targets: Sequence[str] = _FLAVORS,
    *,
    install: InstallPolicy = "fallback",
    probe_version: bool = True,
) -> Optional[CondaExecutable]:
    """Find or install an executable matching an ordered sequence of targets.

    Each target names mamba, micromamba, conda, or conda_standalone, optionally
    followed by version constraints such as ">=2,<3".

    install selects when installation is attempted. Only micromamba and
    conda_standalone can be installed.

    - "fallback", the default: search all targets for installed matches, then
      attempt installation, using target order in each pass.
    - "per-target": search and attempt installation for each target before
      moving on to the next.
    - "never": search only.
    - "force": skip all existing executables and download installable tools in
      target order. Bypass the cache under the download lock, refreshing each
      tool at most once per call. Requires at least one installable target.

    Return the path, flavor, and version, or None if no compatible executable
    is found. If --version cannot start or exits unsuccessfully, report the
    failure on stderr and skip the candidate, including after installation.
    Successful commands with unparseable or undecodable output can satisfy
    unconstrained targets, with version=None.

    With probe_version=False, run --version only when a target has constraints;
    otherwise the version is None and the executable is not checked by running
    it. Unknown versions cannot satisfy a constraint.
    Installation fetches the latest available build; constraints filter the
    result and do not select an older release to download.
    Forced installation replaces the cached copy before checking the result.
    Rejection by a version constraint or execution check does not restore the
    previous copy, even if it was compatible.

    If ensureconda cannot determine which build to download for this Python
    environment, search existing executables only (unless install="force").
    They may still work without a download mapping. Raise
    UnsupportedPlatformError instead of returning
    None if installation would otherwise have been attempted.

    An empty target sequence returns None, except with install="force", which
    requires an installable target. Invalid targets and unknown install values
    raise ValueError before any search or installation.
    """
    if install not in _INSTALL_POLICIES:
        expected = ", ".join(repr(policy) for policy in _INSTALL_POLICIES)
        raise ValueError(
            f"Unknown install value {install!r}; expected one of {expected}"
        )
    if isinstance(targets, str):
        raise TypeError("targets must be a sequence of strings, not a single string")
    parsed = [_parse_target(target) for target in targets]
    return _ensure(parsed, install=install, probe_version=probe_version)


def _ensure(
    parsed: Sequence[Tuple[Flavor, _VersionConstraint]],
    *,
    install: InstallPolicy,
    probe_version: bool,
) -> Optional[CondaExecutable]:
    """Resolve typed targets shared by the old and new public APIs."""
    finders: Dict[Flavor, Callable[[], Iterator[Path]]] = {
        "mamba": mamba_executables,
        "micromamba": micromamba_executables,
        "conda": conda_executables,
        "conda_standalone": conda_standalone_executables,
    }
    installers: Dict[Flavor, Callable[[Callable[[Path], bool]], Optional[Path]]] = {
        "micromamba": install_micromamba,
        "conda_standalone": install_conda_exe,
    }
    if install == "force":
        if not any(flavor in installers for flavor, _ in parsed):
            raise ValueError(
                "force installation requires micromamba or conda_standalone"
            )

    def compatible(
        path: Path,
        flavor: Flavor,
        constraint: _VersionConstraint,
        *,
        quiet: bool = False,
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
                    if not quiet:
                        print(
                            f"Skipping {path}: could not determine its version ({reason})",
                            file=sys.stderr,
                        )
                    return None
        if isinstance(constraint, Version):
            # Only the old API compares unknown output as version 0.0.0.
            comparison_version = (
                version if version is not None else _LEGACY_UNKNOWN_VERSION
            )
            if comparison_version < constraint:
                return None
        elif constraint is not None:
            if version is None or not constraint.contains(version, prereleases=True):
                return None
        return CondaExecutable(path, flavor, version)

    def usable(
        flavor: Flavor, constraint: _VersionConstraint
    ) -> Callable[[Path], bool]:
        """Return the check that the search applies to a candidate for a target."""
        return lambda path: (
            is_exe(path)
            and compatible(path, flavor, constraint, quiet=True) is not None
        )

    # A missing download mapping need not prevent using an installed tool.
    unsupported_platform: Optional[UnsupportedPlatformError] = None
    if install != "never" and any(flavor in installers for flavor, _ in parsed):
        try:
            platform_subdir()
        except UnsupportedPlatformError as error:
            if install == "force":
                raise
            unsupported_platform = error
            install = "never"

    # False denotes searching; True denotes installation. "per-target"
    # interleaves the two, "fallback" puts every search first, and "force"
    # installs without searching.
    steps: List[Tuple[Flavor, _VersionConstraint, bool]] = []
    for flavor, constraint in parsed:
        if install != "force":
            steps.append((flavor, constraint, False))
        if install in ("per-target", "force") and flavor in installers:
            steps.append((flavor, constraint, True))
    if install == "fallback":
        steps.extend(
            (flavor, constraint, True)
            for flavor, constraint in parsed
            if flavor in installers
        )

    refreshed: Dict[Flavor, Optional[Path]] = {}
    unpublished: Set[Flavor] = set()
    for index, (flavor, constraint, installing) in enumerate(steps):
        if flavor in unpublished:
            continue
        if installing:
            try:
                if install == "force" and flavor in refreshed:
                    installed_path = refreshed[flavor]
                else:
                    installed_path = installers[flavor](
                        (lambda _: False)
                        if install == "force"
                        else usable(flavor, constraint)
                    )
                    if install == "force":
                        refreshed[flavor] = installed_path
            except NotPublishedError as error:
                if install == "force":
                    unpublished.add(flavor)
                remaining = dict.fromkeys(
                    name.replace("_", "-")
                    for name, _, _ in steps[index + 1 :]
                    if name not in unpublished
                )
                if not remaining:
                    raise
                print(
                    f"{error}; trying {' or '.join(remaining)} instead",
                    file=sys.stderr,
                )
                continue
            candidates = (
                iter([installed_path]) if installed_path is not None else iter([])
            )
        else:
            candidates = finders[flavor]()
        for path in candidates:
            result = compatible(path, flavor, constraint)
            if result is not None:
                return result
    if unsupported_platform is not None:
        raise unsupported_platform
    return None


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
    flavor = _flavor_from_name(executable) or _probe_flavor(executable)
    if flavor is None:
        raise ValueError(f"{executable} is not a recognized conda executable")
    try:
        version = _probe_version(executable, flavor)
    except (subprocess.CalledProcessError, OSError, InvalidVersion, UnicodeDecodeError):
        version = None
    return CondaExecutable(path, flavor, version)
