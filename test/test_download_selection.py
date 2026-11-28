"""Selection and cache safety through the real HTTP and archive paths."""

import io
import json
import logging
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pytest
import requests
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from ensureconda import api, determine_versions, installer
from ensureconda.determine_versions import Flavor
from ensureconda.resolve import is_windows


def package(version: str, tool: str = "micromamba", **changes: Any) -> Dict[str, Any]:
    result = {
        "size": 0,
        "type": "conda",
        "version": version,
        "labels": ["main"],
        "attrs": {
            "subdir": "linux-64",
            "build": "0",
            "build_number": 0,
            "timestamp": 0,
        },
        "download_url": f"//conda.anaconda.org/conda-forge/linux-64/{tool}-{version}-0.tar.bz2",
    }
    result.update(changes)
    return result


class UnreadableBody(io.BytesIO):
    def read(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("latest lookup read the archive body")


@pytest.mark.parametrize("latest_status", [200, 307, 404])
@pytest.mark.parametrize("constraint", [">=2", "<2", ">=3"])
def test_latest_lookup_and_metadata_fallback(
    latest_status: int,
    constraint: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    urls: List[str] = []

    latest_body = UnreadableBody()

    def send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: Any,
    ) -> requests.Response:
        url = request.url
        assert url is not None
        urls.append(url)
        response = requests.Response()
        response.request = request
        response.url = url
        if url.endswith("/latest"):
            response.status_code = latest_status
            response.headers["Location"] = (
                "//conda.anaconda.org/conda-forge/linux-64/micromamba-2.10.0-0.tar.bz2"
            )
            response.raw = latest_body
        else:
            assert url.endswith("/micromamba/files")
            response.status_code = 200
            response.raw = io.BytesIO(
                json.dumps(
                    [package("1.5"), package("2.10"), package("9", labels=["broken"])]
                ).encode()
            )
        return response

    # Keep Requests' Session logic: even stream=True and allow_redirects=False
    # ordinarily consume redirect bodies to prepare Response.next.
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    with caplog.at_level("INFO", logger="ensureconda"):
        if constraint == ">=3":
            with pytest.raises(
                installer.NoCompatibleVersionError, match="newest available is 2.10"
            ):
                installer.select_micromamba("linux-64", SpecifierSet(constraint))
        else:
            url, version, _ = installer.select_micromamba(
                "linux-64", SpecifierSet(constraint)
            )
            assert version == Version("1.5" if constraint == "<2" else "2.10")
            assert url.endswith(".tar.bz2")
    assert len(urls) == (1 if latest_status == 307 and constraint == ">=2" else 2)
    assert latest_body.closed
    if latest_status == 404:
        assert (
            "micromamba: no build of the latest release for linux-64; checking release metadata"
            in caplog.messages
        )
        assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    if latest_status == 307 and constraint != ">=2":
        assert any("does not satisfy" in message for message in caplog.messages)


@pytest.mark.parametrize(
    "location",
    [
        "",
        "https://other.invalid/tool-2-0.tar.bz2",
        "//conda.anaconda.org/conda-forge/win-64/micromamba-2-0.tar.bz2",
        "//conda.anaconda.org/conda-forge/linux-64/micromamba-invalid-0.tar.bz2",
        "https://[broken",
        "//conda.anaconda.org/conda-forge/linux-64/micromamba-2-.tar.bz2",
    ],
)
def test_unrecognized_latest_redirect_uses_metadata(
    location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest_body = UnreadableBody()
    urls: List[str] = []

    def send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: Any,
    ) -> requests.Response:
        assert request.url is not None
        urls.append(request.url)
        response = requests.Response()
        response.request = request
        response.url = request.url
        if request.url.endswith("/latest"):
            response.status_code = 307
            response.headers["Location"] = location
            response.raw = latest_body
        else:
            assert request.url.endswith("/micromamba/files")
            response.status_code = 200
            response.raw = io.BytesIO(json.dumps([package("2")]).encode())
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    assert installer.select_micromamba("linux-64", None)[1] == Version("2")
    assert latest_body.closed
    assert len(urls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        429,
        503,
        pytest.param(requests.ConnectionError("offline"), id="connection"),
        pytest.param(requests.Timeout("timeout"), id="timeout"),
    ],
)
@pytest.mark.parametrize("metadata_status", [200, 403])
def test_latest_outage_uses_release_metadata(
    failure: Union[int, requests.RequestException],
    metadata_status: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    latest = "https://micro.mamba.pm/api/micromamba/linux-64/latest"
    metadata = "https://api.anaconda.org/package/conda-forge/micromamba/files"
    urls: List[str] = []
    digest = "a" * 64

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        urls.append(url)
        response = requests.Response()
        response.url = url
        if url == latest:
            if isinstance(failure, requests.RequestException):
                raise failure
            response.status_code = failure
            response.raw = UnreadableBody()
        else:
            assert url == metadata
            response.status_code = metadata_status
            response.raw = io.BytesIO(
                json.dumps([package("1.5", sha256=digest), package("2")]).encode()
            )
        return response

    def unexpected_sleep(seconds: float) -> None:
        pytest.fail(f"unexpected retry after {seconds}s")

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(time, "sleep", unexpected_sleep)
    if metadata_status == 200:
        url, version, sha256 = installer.select_micromamba(
            "linux-64", SpecifierSet("<2")
        )
        assert (
            url
            == "https://conda.anaconda.org/conda-forge/linux-64/micromamba-1.5-0.tar.bz2"
        )
        assert version == Version("1.5") and sha256 == digest
    else:
        with pytest.raises(requests.HTTPError) as error:
            installer.select_micromamba("linux-64", None)
        assert error.value.response.url == metadata
        assert error.value.response.status_code == metadata_status
    assert urls == [latest, metadata]
    assert any(
        latest in message and "checking release metadata" in message
        for message in caplog.messages
    )


def serve_package(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    version: str = "2",
    content: bytes = b"new",
    published_versions: Optional[List[str]] = None,
) -> List[str]:
    archive = io.BytesIO()
    name = (
        "standalone_conda/conda.exe"
        if tool == "conda_standalone"
        else "Library/bin/micromamba.exe" if is_windows else "bin/micromamba"
    )
    with tarfile.open(fileobj=archive, mode="w:bz2") as tar:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    urls: List[str] = []

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        urls.append(url)
        response = requests.Response()
        response.raw = io.BytesIO()
        setattr(response, "_content_consumed", True)
        response.url = url
        response.status_code = 200
        response._content = archive.getvalue()
        if url.endswith("/latest"):
            response.status_code = 307
            response.headers["Location"] = (
                f"//conda.anaconda.org/conda-forge/linux-64/micromamba-{version}-0.tar.bz2"
            )
        elif url.endswith("/files"):
            versions = [version] if published_versions is None else published_versions
            response._content = json.dumps(
                [package(v, tool.replace("_", "-")) for v in versions]
            ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(installer, "platform_subdir", lambda: "linux-64")
    return urls


@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
@pytest.mark.parametrize("constraint, selected", [("", "1.5"), (">=2.0rc1", "2.0rc1")])
def test_download_prerelease_policy(
    tool: Flavor,
    constraint: str,
    selected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    urls = serve_package(
        monkeypatch, tool, version="2.0rc1", published_versions=["1.5", "2.0rc1"]
    )
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda *args: Version(selected)
    )
    result = api.ensure([tool + constraint], install="force")
    assert result is not None and result.version == Version(selected)
    assert [url.split("/")[-1] for url in urls if url.endswith(".tar.bz2")] == [
        f"{tool.replace('_', '-')}-{selected}-0.tar.bz2"
    ]


@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
@pytest.mark.parametrize(
    "constraint, published, hint_for_latest, hint_for_metadata",
    [
        ("", ["2.0rc1"], True, True),
        (">=1", ["2.0rc1"], True, True),
        (">=99", ["1.0", "2.0"], False, False),
        ("<2", ["2.0rc1", "3.0rc1"], False, False),
        ("<2", ["1.8rc1", "3.0rc1"], False, True),
    ],
)
def test_explain_published_version_miss(
    tool: Flavor,
    constraint: str,
    published: List[str],
    hint_for_latest: bool,
    hint_for_metadata: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    latest = published[-1]
    serve_package(monkeypatch, tool, version=latest, published_versions=published)
    with caplog.at_level(logging.INFO, logger="ensureconda"):
        assert api.ensure([tool + constraint], install="force") is None
    hint = " (prereleases must be requested explicitly)"
    requirement = constraint or "a stable release"
    assert (
        f"{tool.replace('_', '-')}: no published version for linux-64 satisfies "
        f"{requirement}{hint if hint_for_metadata else ''}; newest available is {latest}"
    ) in caplog.messages
    if tool == "micromamba":
        assert (
            f"micromamba: latest version {latest} does not satisfy "
            f"{requirement}{hint if hint_for_latest else ''}; checking release metadata"
        ) in caplog.messages


@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
def test_staged_prerelease_is_checked_against_executable_constraint(
    tool: Flavor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    cached = tmp_path / (tool + installer.exe_suffix())
    cached.write_bytes(b"old")
    serve_package(monkeypatch, tool)
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda *args: Version("2.0rc1")
    )
    with pytest.raises(
        installer.DownloadedExecutableError,
        match="prereleases must be requested explicitly",
    ):
        api.ensure([tool + ">=1"], install="force")
    with pytest.raises(
        installer.DownloadedExecutableError,
        match=r"reports 2.0rc1, expected 2 satisfying >=2$",
    ):
        api.ensure([tool + ">=2"], install="force")
    assert cached.read_bytes() == b"old"
    assert api.ensure([tool], install="force") == api.CondaExecutable(
        cached, tool, Version("2.0rc1")
    )
    assert cached.read_bytes() == b"new"
    assert api.ensure([tool], install="force", probe_version=False) == (
        api.CondaExecutable(cached, tool, None)
    )


@pytest.mark.skipif(is_windows, reason="the stand-ins are shell scripts")
@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
def test_install_in_current_directory(
    tool: Flavor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", ".")
    output = "conda 25" if tool == "conda_standalone" else "25"
    serve_package(
        monkeypatch, tool, version="25", content=f"#!/bin/sh\necho {output}\n".encode()
    )
    other = tmp_path / "other"
    other.mkdir()
    request = installer.request_url_with_retry

    def change_directory_during_download(url: str, **kwargs: Any) -> requests.Response:
        monkeypatch.chdir(other)
        return request(url, **kwargs)

    monkeypatch.setattr(
        installer, "request_url_with_retry", change_directory_during_download
    )

    installed = api.ensure([tool], install="force")
    assert installed == api.CondaExecutable(tmp_path / tool, tool, Version("25"))
    monkeypatch.chdir(tmp_path)
    assert api.ensure([tool], install="never") == installed


@pytest.mark.skipif(is_windows, reason="the stand-ins are shell scripts")
@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
def test_one_call_runs_each_unchanged_file_once(
    tool: Flavor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "runs.log"
    prefix = "conda " if tool == "conda_standalone" else ""

    def stand_in(name: str, version: str) -> bytes:
        return f'#!/bin/sh\necho {name} >> "{log}"\necho {prefix}{version}\n'.encode()

    on_path, cached = tmp_path / "bin" / tool, tmp_path / "data" / tool
    for path in (on_path, cached):
        path.parent.mkdir()
        path.write_bytes(stand_in(path.parent.name, "0.1.0"))
        path.chmod(0o755)
    monkeypatch.setattr(installer, "site_path", lambda: cached.parent)
    monkeypatch.setattr(api, f"{tool}_executables", lambda: iter([on_path, cached]))
    serve_package(monkeypatch, tool, content=stand_in("download", "2"))

    assert api.ensure([f"{tool}>=1"]) == api.CondaExecutable(cached, tool, Version("2"))
    # The installer's check under the lock and the check of the installed file
    # reuse the search's and the validation's results.
    assert log.read_text().splitlines() == ["bin", "data", "download"]


@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
@pytest.mark.parametrize(
    "failure", ["execution", "version", "unknown", "flavor", "none"]
)
def test_validate_before_replacing_cache(
    tool: str,
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    target = tmp_path / (tool + installer.exe_suffix())
    target.write_bytes(b"old")
    urls = serve_package(monkeypatch, tool)

    def probe(path: Path, observations: object) -> str:
        assert path != target and path.read_bytes() == b"new"
        assert target.read_bytes() == b"old"
        if failure == "execution":
            raise OSError("cannot run")
        if failure == "unknown":
            return ""
        if failure == "flavor":
            return "conda 25.1" if tool == "micromamba" else "2.9"
        prefix = "conda " if tool == "conda_standalone" else ""
        return prefix + ("1" if failure == "version" else "2")

    monkeypatch.setattr(determine_versions, "_version_output", probe)
    install = (
        installer.install_micromamba
        if tool == "micromamba"
        else installer.install_conda_exe
    )
    if failure == "none":
        assert install(SpecifierSet(">=2"), force=True) == target
        assert target.read_bytes() == b"new"
    else:
        with pytest.raises(installer.DownloadedExecutableError):
            # A wrong tool must also be rejected for an unconstrained download.
            install(SpecifierSet("" if failure == "flavor" else ">=2"), force=True)
        assert target.read_bytes() == b"old"
    assert len(urls) == 2
    assert all(
        path == target or path.name.endswith(".lock") for path in tmp_path.iterdir()
    )


@pytest.mark.parametrize("policy", ["fallback", "per-target", "force"])
def test_later_requirement_can_install_after_constraint_miss(
    policy: api.InstallPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(api, "micromamba_executables", lambda: iter([]))
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda *args: Version("2")
    )
    urls = serve_package(monkeypatch, "micromamba")
    with caplog.at_level(logging.INFO, logger="ensureconda"):
        result = api.ensure(["micromamba>=3", "micromamba>=1"], install=policy)
    assert result is not None and result.version == Version("2")
    (download_url,) = [url for url in urls if url.endswith(".tar.bz2")]
    assert (
        f"micromamba: downloading {download_url} and extracting to {result.path}"
        in caplog.messages
    )


@pytest.mark.parametrize("tool", ["micromamba", "conda_standalone"])
def test_download_log_names_destination(
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda *args: Version("2")
    )
    urls = serve_package(monkeypatch, tool)
    install = (
        installer.install_micromamba
        if tool == "micromamba"
        else installer.install_conda_exe
    )
    with caplog.at_level(logging.INFO, logger="ensureconda"):
        target = install(force=True)
    assert target.parent == tmp_path
    (download_url,) = [url for url in urls if url.endswith(".tar.bz2")]
    name = tool.replace("_", "-")
    assert (
        f"{name}: downloading {download_url} and extracting to {target}"
        in caplog.messages
    )


@pytest.mark.parametrize("target", ["micromamba", "micromamba>=1"])
@pytest.mark.parametrize(
    "output", ["", "new-format", "undecodable", "nonzero", "0.0.0.dev0+placeholder"]
)
def test_download_with_unknown_version(
    target: str,
    output: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    serve_package(monkeypatch, "micromamba")
    cached = tmp_path / ("micromamba" + installer.exe_suffix())
    cached.write_bytes(b"old")

    def check_output(*args: Any, **kwargs: Any) -> bytes:
        if output == "undecodable":
            return b"\xff"
        if output == "nonzero":
            raise subprocess.CalledProcessError(1, "micromamba --version")
        return output.encode()

    monkeypatch.setattr(subprocess, "check_output", check_output)
    if target == "micromamba" and output != "nonzero":
        assert api.ensure([target], install="force") == api.CondaExecutable(
            cached, "micromamba", None
        )
        assert cached.read_bytes() == b"new"
    else:
        with pytest.raises(installer.DownloadedExecutableError) as error:
            api.ensure([target], install="force")
        assert "prereleases" not in str(error.value)
        assert cached.read_bytes() == b"old"


@pytest.mark.skipif(is_windows, reason="the downloaded executable is a shell script")
def test_failed_download_with_undecodable_output_preserves_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    cached = tmp_path / "micromamba"
    original = b"#!/bin/sh\necho 2.0\n"
    cached.write_bytes(original)
    cached.chmod(0o755)
    serve_package(
        monkeypatch,
        "micromamba",
        content=b"#!/bin/sh\nprintf 'error\\r\\n\\377'\nexit 7\n",
    )

    with pytest.raises(installer.DownloadedExecutableError) as error:
        api.ensure(["micromamba"], install="force")

    cause = error.value.__cause__
    assert isinstance(cause, subprocess.CalledProcessError)
    assert cause.returncode == 7
    assert cause.output == "error\n\ufffd"
    assert cached.read_bytes() == original


def test_legacy_zero_minimum_rejects_download_with_unknown_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENSURECONDA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    serve_package(monkeypatch, "micromamba")
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: b"")

    with pytest.raises(
        installer.DownloadedExecutableError, match="reports unknown version"
    ):
        api.ensureconda(
            mamba=False, conda=False, conda_exe=False, min_mamba_version=Version("0")
        )
    assert not (tmp_path / ("micromamba" + installer.exe_suffix())).exists()


def test_standalone_package_revision_can_differ_from_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda *args: Version("26.9.4")
    )
    serve_package(monkeypatch, "conda_standalone", "26.9.4.post1")
    with caplog.at_level("INFO", logger="ensureconda"):
        installer.install_conda_exe(SpecifierSet(">=26.9"), force=True)
    assert any(
        "executable version 26.9.4 (package version 26.9.4.post1)" in message
        for message in caplog.messages
    )


def test_matching_package_version_is_not_repeated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(
        determine_versions, "_probe_version", lambda *args: Version("26.9.4")
    )
    serve_package(monkeypatch, "conda_standalone", "26.9.4")
    with caplog.at_level("INFO", logger="ensureconda"):
        installer.install_conda_exe(SpecifierSet(">=26.9"), force=True)
    assert "conda-standalone: validated executable version 26.9.4" in caplog.messages
