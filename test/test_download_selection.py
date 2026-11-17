"""Selection and cache safety through the real HTTP and archive paths."""

import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
import requests
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from ensureconda import api, determine_versions, installer
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
    "error", [requests.ConnectionError("offline"), requests.Timeout("timeout")]
)
def test_network_failure_is_not_a_constraint_miss(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> requests.Response:
        raise error

    monkeypatch.setattr(installer, "request_url_with_retry", fail)

    def unexpected(*args: Any) -> List[installer.AnacondaPkg]:
        raise AssertionError("network failures must not trigger metadata fallback")

    monkeypatch.setattr(installer, "compute_candidates", unexpected)
    with pytest.raises(type(error)):
        installer.select_micromamba("linux-64", None)


def serve_package(
    monkeypatch: pytest.MonkeyPatch, tool: str, version: str = "2"
) -> List[str]:
    archive = io.BytesIO()
    name = (
        "standalone_conda/conda.exe"
        if tool == "conda_standalone"
        else "Library/bin/micromamba.exe" if is_windows else "bin/micromamba"
    )
    with tarfile.open(fileobj=archive, mode="w:bz2") as tar:
        member = tarfile.TarInfo(name)
        member.size = 3
        tar.addfile(member, io.BytesIO(b"new"))
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
            response._content = json.dumps(
                [package(version, tool.replace("_", "-"))]
            ).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(installer, "platform_subdir", lambda: "linux-64")
    return urls


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
    assert f"micromamba: downloading {download_url}" in caplog.messages
