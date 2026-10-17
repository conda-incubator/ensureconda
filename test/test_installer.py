import io
import os
import tarfile
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple, Union

import pytest
import requests
from conda_package_streaming.create import conda_builder

from ensureconda import installer


def test_copy_executable_with_bounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BoundedReader(io.BytesIO):
        def read(self, size: Optional[int] = -1) -> bytes:
            assert size is not None and 0 < size <= 1024 * 1024
            return super().read(size)

    payload = b"executable content" * (256 * 1024)
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    with BoundedReader(payload) as source:
        executable = installer.write_executable_from_file_object(source, "executable")

    assert executable.read_bytes() == payload
    assert os.access(executable, os.X_OK)


@pytest.fixture(params=[".conda", ".tar.bz2"])
def package(request: pytest.FixtureRequest, tmp_path: Path) -> Tuple[str, bytes, bytes]:
    # Incompressible data exercises many decompressor reads in the old downloader.
    payload = os.urandom(256 * 1024)
    member = tarfile.TarInfo("standalone_conda/conda.exe")
    member.size = len(payload)
    filename = "standalone-test" + request.param
    if request.param == ".conda":
        with conda_builder("standalone-test", tmp_path) as archive:
            archive.addfile(member, io.BytesIO(payload))
    else:
        with tarfile.open(tmp_path / filename, "w:bz2") as archive:
            archive.addfile(member, io.BytesIO(payload))
    return filename, (tmp_path / filename).read_bytes(), payload


def test_download_before_extracting(
    package: Tuple[str, bytes, bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename, data, payload = package
    calls = []

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        calls.append((method, url, kwargs))
        assert "Range" not in kwargs.get("headers", {})
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(data)
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path / "cache")
    url = f"https://example.invalid/{filename}?download=1"
    executable = installer.stream_conda_executable(url)

    assert executable.read_bytes() == payload
    assert executable.name == "conda_standalone" + installer.exe_suffix()
    assert os.access(executable, os.X_OK)
    assert len(calls) == 1
    assert calls[0][2]["stream"] is True
    assert calls[0][2]["timeout"] == (10, 60)


@pytest.mark.parametrize("failure", ["http", "interrupted", "timeout"])
def test_download_failure_preserves_existing_executable(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / ("conda_standalone" + installer.exe_suffix())
    executable.write_bytes(b"existing executable")
    attempts = 0
    responses: List[requests.Response] = []
    delays: List[float] = []

    def interrupted(chunk_size: int) -> Iterator[bytes]:
        yield b"partial archive"
        raise requests.ConnectionError("Download interrupted")

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        if failure == "timeout":
            raise requests.Timeout("Server did not respond")
        response = requests.Response()
        response.status_code = 502 if failure == "http" else 200
        response.raw = io.BytesIO(b"partial archive")
        responses.append(response)
        if failure == "interrupted":
            monkeypatch.setattr(response, "iter_content", interrupted)
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    monkeypatch.setattr(time, "sleep", delays.append)

    with pytest.raises(requests.RequestException):
        installer.stream_conda_executable("https://example.invalid/package.conda")

    assert executable.read_bytes() == b"existing executable"
    assert list(tmp_path.iterdir()) == [executable]
    assert attempts == 4
    assert delays == [1, 2, 4]
    for response in responses:
        assert response.raw.closed


@pytest.mark.parametrize(
    "failure", [500, 502, 503, 504, "connect", "read-timeout", "interrupted"]
)
@pytest.mark.parametrize("to_file", [False, True], ids=["memory", "file"])
def test_download_retries_transient_failures(
    failure: Union[int, str], to_file: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b'{"download": "complete"}'
    attempts = 0
    responses: List[requests.Response] = []
    delays: List[float] = []

    def interrupted(chunk_size: int) -> Iterator[bytes]:
        # Longer than the successful response, so seeking alone cannot discard it.
        yield b"partial contents from an unsuccessful download" * 10
        if failure == "read-timeout":
            raise requests.ReadTimeout("Read timed out")
        raise requests.exceptions.ChunkedEncodingError("Truncated response")

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure == "connect":
            raise requests.ConnectionError("Connection failed")
        response = requests.Response()
        response.status_code = (
            failure if attempts == 1 and isinstance(failure, int) else 200
        )
        response.raw = io.BytesIO(body)
        responses.append(response)
        if attempts == 1 and failure in ("read-timeout", "interrupted"):
            monkeypatch.setattr(response, "iter_content", interrupted)
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(time, "sleep", delays.append)
    with io.BytesIO() as archive:
        response = installer.request_url_with_retry(
            "https://example.invalid/package.conda",
            destination=archive if to_file else None,
        )
        if to_file:
            assert not archive.closed
            assert archive.tell() == 0
            assert archive.read() == body
        else:
            assert response.content == body
            assert response.json() == {"download": "complete"}
    assert attempts == 2
    assert delays == [1]
    if failure != "connect":
        assert responses[0].raw.closed


@pytest.mark.parametrize("status", [401, 403, 404])
@pytest.mark.parametrize("to_file", [False, True], ids=["memory", "file"])
def test_download_does_not_retry_permanent_http_errors(
    status: int, to_file: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    delays: List[float] = []

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO()
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(time, "sleep", delays.append)
    with io.BytesIO() as archive, pytest.raises(requests.HTTPError) as error:
        installer.request_url_with_retry(
            "https://example.invalid/package.conda",
            destination=archive if to_file else None,
        )
    assert error.value.response.status_code == status
    assert attempts == 1
    assert not delays


def test_download_does_not_retry_disk_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnwritableArchive(io.BytesIO):
        def write(self, data: Any) -> int:
            raise OSError("Disk full")

    attempts = 0
    delays: List[float] = []

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(b"archive contents")
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(time, "sleep", delays.append)
    with UnwritableArchive() as archive, pytest.raises(OSError, match="Disk full"):
        installer.request_url_with_retry(
            "https://example.invalid/package.conda", destination=archive
        )
    assert attempts == 1
    assert not delays


def test_missing_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:bz2"):
        pass
    response = requests.Response()
    response.status_code = 200
    response.raw = io.BytesIO(archive.getvalue())

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(installer, "site_path", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="Could not find conda.exe"):
        installer.stream_conda_executable("https://example.invalid/package.tar.bz2")
    assert not list(tmp_path.iterdir())
