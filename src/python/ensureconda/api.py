import errno
import logging
import os
import subprocess
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ensureconda.determine_versions import CondaExecutable as CondaExecutable
from ensureconda.determine_versions import Flavor as Flavor
from ensureconda.determine_versions import (
    _probe_version,
    _StrPath,
    _VersionConstraint,
    check_executable,
)
from ensureconda.determine_versions import (
    determine_conda_version as determine_conda_version,
)
from ensureconda.determine_versions import (
    determine_mamba_version as determine_mamba_version,
)
from ensureconda.determine_versions import (
    determine_micromamba_version as determine_micromamba_version,
)
from ensureconda.installer import (
    NotPublishedError,
    install_conda_exe,
    install_micromamba,
)
from ensureconda.resolve import (
    UnsupportedPlatformError,
    conda_executables,
    conda_standalone_executables,
    mamba_executables,
    micromamba_executables,
    platform_subdir,
)

logger = logging.getLogger(__name__)

InstallPolicy = Literal["never", "fallback", "per-target", "force"]

_FLAVORS: Tuple[Flavor, ...] = ("mamba", "micromamba", "conda", "conda_standalone")
_TARGET_FLAVORS = {canonicalize_name(flavor): flavor for flavor in _FLAVORS}
_INSTALL_POLICIES: Tuple[InstallPolicy, ...] = (
    "never",
    "fallback",
    "per-target",
    "force",
)


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


def _probe_flavor(executable: _StrPath) -> Optional[Flavor]:
    try:
        output = subprocess.check_output(
            [executable, "--version"], stderr=subprocess.DEVNULL, encoding="utf-8"
        )
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
    is found. If --version cannot start or exits unsuccessfully, log a warning
    and skip the candidate, including after installation.
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
    installable = ("micromamba", "conda_standalone")
    if install == "force":
        if not any(flavor in installable for flavor, _ in parsed):
            raise ValueError(
                "force installation requires micromamba or conda_standalone"
            )

    report_rejections = install == "force"

    # A missing download mapping need not prevent using an installed tool.
    unsupported_platform: Optional[UnsupportedPlatformError] = None
    if install != "never" and any(flavor in installable for flavor, _ in parsed):
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
        if install in ("per-target", "force") and flavor in installable:
            steps.append((flavor, constraint, True))
    if install == "fallback":
        steps.extend(
            (flavor, constraint, True)
            for flavor, constraint in parsed
            if flavor in installable
        )

    refreshed: Dict[Flavor, Path] = {}
    unpublished: Set[Flavor] = set()
    for index, (flavor, constraint, installing) in enumerate(steps):
        if flavor in unpublished:
            continue
        if installing:
            try:
                if install == "force" and flavor in refreshed:
                    installed_path = refreshed[flavor]
                else:
                    if flavor == "micromamba":
                        installed_path = install_micromamba(
                            constraint,
                            force=install == "force",
                            probe_version=probe_version,
                        )
                    else:
                        installed_path = install_conda_exe(
                            constraint,
                            force=install == "force",
                            probe_version=probe_version,
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
                logger.warning("%s; trying %s instead", error, " or ".join(remaining))
                continue
            candidates = iter([installed_path])
        else:
            candidates = finders[flavor]()
        for path in candidates:
            result = check_executable(
                path,
                flavor,
                constraint,
                probe_version=probe_version,
                report_rejections=report_rejections,
            )
            if result is not None:
                return result
    if unsupported_platform is not None:
        raise unsupported_platform
    return None


def identify(executable: _StrPath) -> CondaExecutable:
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
