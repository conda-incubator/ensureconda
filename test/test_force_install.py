import hashlib
import io
import json
import logging
import sys
import tarfile
from pathlib import Path
from typing import IO, Callable, List, Optional
from unittest.mock import Mock

import filelock
import pytest
import requests
from click.testing import CliRunner
from packaging.version import Version

from ensureconda import api, cli, determine_versions, installer
from ensureconda.resolve import UnsupportedPlatformError, is_windows


@pytest.fixture
def no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    for finder in [
        "mamba_executables",
        "micromamba_executables",
        "conda_executables",
        "conda_standalone_executables",
    ]:
        monkeypatch.setattr(
            api,
            finder,
            Mock(side_effect=AssertionError("searched for an existing tool")),
        )


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
@pytest.mark.parametrize(
    "entry_point", ["new-api", "new-api-incompatible", "cli", "json-cli"]
)
def test_force_install_replaces_fresh_cache(
    tool: str, entry_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / (tool + installer.exe_suffix())
    target.write_bytes(b"old executable")
    target.chmod(0o755)
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(
        determine_versions,
        "_probe_version",
        lambda path, flavor: Version(
            "1.5" if path.read_bytes() == b"old executable" else "25.1"
        ),
    )
    member_name = "Library/bin/micromamba.exe" if is_windows else "bin/micromamba"
    lock_name = "micromamba_install.lock"
    if tool == "conda_standalone":
        member_name = "standalone_conda/conda.exe"
        lock_name = "conda_exe_install.lock"
        package = installer.AnacondaPkg(
            0,
            installer.AnacondaPkgAttr("linux-64", "0", 0, 0),
            "conda",
            "25.1",
            "//example.invalid/conda.tar.bz2",
        )
        monkeypatch.setattr(installer, "compute_candidates", lambda *args: [package])

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:bz2") as tar:
        member = tarfile.TarInfo(member_name)
        member.size = len(b"fresh executable")
        tar.addfile(member, io.BytesIO(b"fresh executable"))
    downloads: List[str] = []

    def download(
        url: str, *, destination: Optional[IO[bytes]] = None
    ) -> requests.Response:
        with pytest.raises(filelock.Timeout):
            filelock.FileLock(str(tmp_path / lock_name)).acquire(timeout=0)
        downloads.append(url)
        response = requests.Response()
        response._content = archive.getvalue()
        if destination is not None:
            destination.write(archive.getvalue())
            destination.seek(0)
        return response

    monkeypatch.setattr(installer, "request_url_with_retry", download)

    # Each force request downloads again, even immediately after another one.
    for _ in range(2):
        if entry_point == "new-api-incompatible":
            # Rejecting the new version does not restore the old compatible copy.
            assert api.ensure([f"{tool}<2"], install="force") is None
        elif entry_point == "new-api":
            # Search-only tools are skipped, and target order is respected.
            result = api.ensure(["conda", tool, "micromamba"], install="force")
            assert result is not None and result.path == target
        else:
            args = ["--force-install"]
            if tool == "conda_standalone":
                args.append("--no-micromamba")
            if entry_point == "json-cli":
                args.append("--json")
            result_cli = CliRunner().invoke(cli.ensureconda_cli, args)
            assert result_cli.exit_code == 0, result_cli.output
            output = result_cli.output.strip().splitlines()[-1]
            if entry_point == "json-cli":
                assert json.loads(output) == {
                    "path": str(target),
                    "flavor": tool,
                    "version": "25.1",
                }
            else:
                assert output == str(target)
        assert target.read_bytes() == b"fresh executable"
    assert len(downloads) == 2
    with filelock.FileLock(str(tmp_path / lock_name)).acquire(timeout=0):
        pass


@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
def test_normal_install_reuses_usable_cache(
    tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / (tool + installer.exe_suffix())
    target.write_bytes(b"existing executable")
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(
        installer,
        "request_url_with_retry",
        Mock(side_effect=AssertionError("downloaded")),
    )
    install = (
        installer.install_micromamba
        if tool == "micromamba"
        else installer.install_conda_exe
    )
    target.chmod(0o755)
    probe = Mock(return_value=Version("2.0"))
    monkeypatch.setattr(determine_versions, "_probe_version", probe)
    assert install(Version("1.0")) == target
    probe.assert_called_once_with(target, tool)
    assert target.read_bytes() == b"existing executable"


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
def test_force_download_failure_preserves_existing_executable(
    tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / (tool + installer.exe_suffix())
    target.write_bytes(b"old executable")
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(
        installer,
        "request_url_with_retry",
        Mock(side_effect=requests.ConnectionError("offline")),
    )
    with pytest.raises(requests.ConnectionError, match="offline"):
        api.ensure([tool], install="force")
    assert target.read_bytes() == b"old executable"


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("failure", ["checksum", "missing-member"])
def test_force_standalone_rejects_archive_without_replacing_cache(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ("conda_standalone" + installer.exe_suffix())
    target.write_bytes(b"old executable")
    target.chmod(0o755)
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:bz2") as tar:
        name = "standalone_conda/conda.exe" if failure == "checksum" else "other"
        member = tarfile.TarInfo(name)
        member.size = len(b"new executable")
        tar.addfile(member, io.BytesIO(b"new executable"))
    payload = archive.getvalue()
    digest = "0" * 64 if failure == "checksum" else hashlib.sha256(payload).hexdigest()
    package = installer.AnacondaPkg(
        len(payload),
        installer.AnacondaPkgAttr("linux-64", "0", 0, 0),
        "conda",
        "25.1",
        "//example.invalid/conda.tar.bz2",
        digest,
    )
    monkeypatch.setattr(installer, "compute_candidates", lambda *args: [package])

    def download(url: str, *, destination: IO[bytes]) -> requests.Response:
        destination.write(payload)
        destination.seek(0)
        return requests.Response()

    request = Mock(side_effect=download)
    monkeypatch.setattr(installer, "request_url_with_retry", request)
    message = (
        "SHA-256 mismatch" if failure == "checksum" else "Could not find conda.exe"
    )
    with pytest.raises(RuntimeError, match=message):
        api.ensure(["conda_standalone"], install="force")
    request.assert_called_once()
    assert target.read_bytes() == b"old executable"


@pytest.mark.usefixtures("no_discovery")
def test_force_install_checks_version_constraints(
    monkeypatch: pytest.MonkeyPatch,
    log_messages: Callable[[], str],
) -> None:
    install = Mock(return_value=Path("micromamba"))
    monkeypatch.setattr(api, "install_micromamba", install)
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda path, flavor: Version("1.0")
    )
    assert api.ensure(["micromamba>=2"], install="force", probe_version=False) is None
    install.assert_called_once()
    assert log_messages() == "Rejected micromamba 1.0: requires >=2\n"


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "tool, version, minimum, exit_code",
    [
        ("micromamba", "0.7.2", None, 1),
        ("micromamba", "0.7.3", None, 0),
        ("conda_standalone", "4.8.1", None, 1),
        ("conda_standalone", "4.8.2", None, 0),
        ("micromamba", "2.0", "2.0+local", 1),
        ("conda_standalone", "2.0", "2.0+local", 1),
    ],
)
def test_force_cli_keeps_version_minimums(
    tool: str,
    version: str,
    minimum: Optional[str],
    exit_code: int,
    as_json: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = Mock(return_value=Path(tool))
    micro = tool == "micromamba"
    monkeypatch.setattr(
        api, "install_micromamba" if micro else "install_conda_exe", install
    )
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda path, flavor: Version(version)
    )
    args = ["--force-install", "--no-conda-exe" if micro else "--no-micromamba"]
    if minimum is not None:
        args.extend(
            ["--min-mamba-version" if micro else "--min-conda-version", minimum]
        )
    if as_json:
        args.append("--json")

    result = CliRunner().invoke(cli.ensureconda_cli, args)

    assert result.exit_code == exit_code, result.output
    if exit_code:
        assert isinstance(result.exception, SystemExit)
        assert "Could not find compatible executable" in result.output
        required = minimum or ("0.7.3" if micro else "4.8.2")
        assert f"Rejected {tool} {version}: requires >={required}" in result.output
    install.assert_called_once()


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("entry_point", ["api", "cli", "json-cli"])
def test_force_reports_version_rejection_before_fallback(
    entry_point: str,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: Callable[[], str],
) -> None:
    monkeypatch.setattr(
        api, "install_micromamba", lambda *args, **kwargs: Path("micromamba")
    )
    monkeypatch.setattr(
        determine_versions,
        "_probe_version",
        lambda path, flavor: Version("2.3.0" if flavor == "micromamba" else "25.1"),
    )

    def standalone(constraint: object, **kwargs: object) -> Path:
        logging.getLogger("ensureconda.test").warning("Downloading conda-standalone")
        return Path("conda_standalone")

    monkeypatch.setattr(api, "install_conda_exe", standalone)
    if entry_point == "api":
        result = api.ensure(["micromamba>=3", "conda_standalone"], install="force")
        assert result is not None and result.path == Path("conda_standalone")
        output = log_messages()
    else:
        args = ["--force-install", "--min-mamba-version", "3"]
        if entry_point == "json-cli":
            args.append("--json")
        cli_result = CliRunner().invoke(cli.ensureconda_cli, args)
        assert cli_result.exit_code == 0, cli_result.output
        output = cli_result.output
        assert "conda_standalone" in output.splitlines()[-1]
    rejected = "Rejected micromamba 2.3.0: requires >=3"
    assert rejected in output
    assert output.index(rejected) < output.index("Downloading conda-standalone")


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("standalone", [False, True])
def test_force_install_falls_back_only_to_installable_targets(
    standalone: bool,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: Callable[[], str],
) -> None:
    monkeypatch.setattr(
        api,
        "install_micromamba",
        Mock(side_effect=installer.NotPublishedError("micromamba is not published")),
    )
    install = Mock(return_value=Path("conda_standalone"))
    monkeypatch.setattr(api, "install_conda_exe", install)
    if standalone:
        result = api.ensure(install="force", probe_version=False)
        assert result is not None and result.path == Path("conda_standalone")
        install.assert_called_once()
        assert log_messages() == (
            "micromamba is not published; trying conda-standalone instead\n"
        )
    else:
        with pytest.raises(installer.NotPublishedError):
            api.ensure(["mamba", "micromamba", "conda"], install="force")
        install.assert_not_called()


@pytest.mark.usefixtures("no_discovery")
def test_force_install_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "platform_subdir",
        Mock(side_effect=UnsupportedPlatformError("unsupported")),
    )
    with pytest.raises(UnsupportedPlatformError):
        api.ensure(install="force")


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("targets", [[], ["conda"], ["mamba", "conda"]])
def test_force_requires_installable_target(targets: List[str]) -> None:
    with pytest.raises(ValueError, match="force installation requires"):
        api.ensure(targets, install="force")


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "options", [["--no-install"], ["--no-micromamba", "--no-conda-exe"]]
)
def test_invalid_force_cli_options(options: List[str], as_json: bool) -> None:
    args = ["--force-install", *options]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(cli.ensureconda_cli, args)
    assert result.exit_code == 2
    assert "Error: --force-install" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("second_target", ["micromamba>=2", "micromamba<2"])
def test_force_downloads_repeated_tool_only_once(
    second_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path("micromamba")
    install = Mock(return_value=path)
    monkeypatch.setattr(api, "install_micromamba", install)
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda path, flavor: Version("2.5")
    )

    result = api.ensure(["micromamba>=3", second_target], install="force")

    if second_target == "micromamba>=2":
        assert result == api.CondaExecutable(path, "micromamba", Version("2.5"))
    else:
        assert result is None
    install.assert_called_once()


@pytest.mark.usefixtures("no_discovery")
@pytest.mark.parametrize("standalone", [False, True])
def test_force_does_not_retry_an_unpublished_tool_for_later_constraints(
    standalone: bool,
    monkeypatch: pytest.MonkeyPatch,
    log_messages: Callable[[], str],
) -> None:
    micro = Mock(side_effect=installer.NotPublishedError("micromamba is not published"))
    conda = Mock(return_value=Path("conda_standalone"))
    monkeypatch.setattr(api, "install_micromamba", micro)
    monkeypatch.setattr(api, "install_conda_exe", conda)
    targets = ["micromamba>=3", "micromamba"]
    if standalone:
        targets.append("conda_standalone")
        result = api.ensure(targets, install="force", probe_version=False)
        assert result is not None and result.path == Path("conda_standalone")
        conda.assert_called_once()
        assert log_messages() == (
            "micromamba is not published; trying conda-standalone instead\n"
        )
    else:
        with pytest.raises(installer.NotPublishedError):
            api.ensure(targets, install="force")
        conda.assert_not_called()
        assert log_messages() == ""
    micro.assert_called_once()
