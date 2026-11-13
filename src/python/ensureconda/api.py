import errno
import os
import re
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
    Tuple,
)

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
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


_FLAVORS: Tuple[Flavor, ...] = ("mamba", "micromamba", "conda", "conda_standalone")
_TARGET_FLAVORS = {canonicalize_name(flavor): flavor for flavor in _FLAVORS}


def _probe_version(executable: "StrPath", flavor: Flavor) -> Version:
    probes: Dict[Flavor, Callable[["StrPath"], Version]] = {
        "mamba": determine_mamba_version,
        "micromamba": determine_micromamba_version,
        "conda": determine_conda_version,
        "conda_standalone": determine_conda_version,
    }
    return probes[flavor](executable)


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
    """Compatibility API returning an executable path, or None.

    The booleans select targets in the legacy order: mamba, micromamba, conda,
    conda_standalone. Search and attempt installation for each target before
    moving on. Version probes run only when a minimum version is requested.

    For an ordered target sequence and executable metadata, use :func:`ensure`.
    """
    targets = [
        name if minimum is None else f"{name}>={minimum}"
        for enabled, name, minimum in [
            (mamba, "mamba", min_mamba_version),
            (micromamba, "micromamba", min_mamba_version),
            (conda, "conda", min_conda_version),
            (conda_exe, "conda_standalone", min_conda_version),
        ]
        if enabled
    ]
    result = ensure(
        targets,
        no_install=no_install,
        prefer_installed=False,
        probe_version=False,
    )
    return None if result is None else result.path


def _parse_target(target: str) -> Tuple[Flavor, _VersionCheck]:
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)", target.strip())
    flavor = _TARGET_FLAVORS.get(canonicalize_name(match[1])) if match else None
    if match is None or flavor is None:
        raise ValueError(
            f"Unknown target {target!r}; expected mamba, micromamba, conda, "
            "or conda_standalone, optionally followed by version constraints"
        )
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


def ensure(
    targets: Sequence[str] = _FLAVORS,
    *,
    no_install: bool = False,
    prefer_installed: bool = True,
    probe_version: bool = True,
) -> Optional[CondaExecutable]:
    """Find or install an executable matching an ordered sequence of targets.

    Each target names mamba, micromamba, conda, or conda_standalone, optionally
    followed by version constraints such as ">=2,<3". Names follow packaging's
    canonicalization: case is ignored, and runs of dots, underscores, and
    hyphens are equivalent. Metadata uses the fixed Flavor values above.
    Search all targets for installed matches before attempting installation,
    using target order in each pass. With prefer_installed=False, search and
    attempt installation for each target before moving on. Only micromamba
    and conda_standalone can be installed.

    Return the path, flavor, and version, or None if no compatible executable
    is found. If --version cannot start or exits unsuccessfully, report the
    failure on stderr and skip the candidate, including after installation.
    Successful commands with unparseable or undecodable output can satisfy
    unconstrained targets, with version=None.

    With probe_version=False, run --version only when a target has constraints;
    otherwise the version is None and the executable is not checked by running
    it. Failed version probes cannot satisfy a constraint. The version probes
    retain their legacy 0.0.0 fallback for unrecognized output, reported as None
    in metadata.
    Prereleases are considered when they satisfy the version constraints.
    Installation fetches the latest available build; constraints filter the
    result and do not select an older release to download.

    Set no_install=True to search only. An empty target sequence returns None.
    Invalid targets raise ValueError before any search or installation.
    """
    if isinstance(targets, str):
        raise TypeError("targets must be a sequence of strings, not a single string")
    parsed = [_parse_target(target) for target in targets]
    finders: Dict[Flavor, Callable[[], Iterator[Path]]] = {
        "mamba": mamba_executables,
        "micromamba": micromamba_executables,
        "conda": conda_executables,
        "conda_standalone": conda_standalone_executables,
    }
    installers: Dict[Flavor, Callable[[], Optional[Path]]] = {
        "micromamba": install_micromamba,
        "conda_standalone": install_conda_exe,
    }

    def compatible(
        path: Path, flavor: Flavor, accepts_version: _VersionCheck
    ) -> Optional[CondaExecutable]:
        version = None
        if accepts_version is not None or probe_version:
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
                if accepts_version is not None or isinstance(
                    error, (subprocess.CalledProcessError, OSError)
                ):
                    reason = (
                        f"`--version` exited with status {error.returncode}"
                        if isinstance(error, subprocess.CalledProcessError)
                        else str(error)
                    )
                    print(
                        f"Skipping {path}: could not determine its version ({reason})",
                        file=sys.stderr,
                    )
                    return None
        if accepts_version is not None and (
            version is None or not accepts_version(version)
        ):
            return None
        return CondaExecutable(path, flavor, _known_version(version))

    # False denotes searching; True denotes installation. Interleave the two
    # operations for the legacy API, or put every search before installation.
    steps: List[Tuple[Flavor, _VersionCheck, bool]] = []
    for flavor, accepts_version in parsed:
        steps.append((flavor, accepts_version, False))
        if not prefer_installed and not no_install and flavor in installers:
            steps.append((flavor, accepts_version, True))
    if prefer_installed and not no_install:
        steps.extend(
            (flavor, accepts_version, True)
            for flavor, accepts_version in parsed
            if flavor in installers
        )

    for index, (flavor, accepts_version, installing) in enumerate(steps):
        if installing:
            try:
                installed_path = installers[flavor]()
            except NotPublishedError as error:
                remaining = dict.fromkeys(
                    name.replace("_", "-") for name, _, _ in steps[index + 1 :]
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
            result = compatible(path, flavor, accepts_version)
            if result is not None:
                return result
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
    flavor = _flavor_from_name(executable) or _flavor_from_output(executable)
    if flavor is None:
        raise ValueError(f"{executable} is not a recognized conda executable")
    try:
        version = _known_version(_probe_version(executable, flavor))
    except (subprocess.CalledProcessError, OSError, InvalidVersion, UnicodeDecodeError):
        version = None
    return CondaExecutable(path, flavor, version)
