import gzip
import http.client
import io
import os
import tarfile
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple, Union

import pytest
import requests
from conda_package_streaming.create import conda_builder
from urllib3.response import HTTPResponse

from ensureconda import installer


class FakeClock:
    def __init__(self) -> None:
        self.now_sec = 0.0
        self.delays_sec: List[float] = []

    def sleep(self, delay_sec: float) -> None:
        self.delays_sec.append(delay_sec)
        self.now_sec += delay_sec


@pytest.fixture
def retry_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(time, "monotonic", lambda: clock.now_sec)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


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
    assert calls[0][2]["timeout"] == (
        installer.CONNECT_TIMEOUT_SEC,
        installer.READ_TIMEOUT_SEC,
    )


@pytest.mark.parametrize("failure", ["http", "interrupted", "timeout"])
def test_download_failure_preserves_existing_executable(
    failure: str,
    tmp_path: Path,
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / ("conda_standalone" + installer.exe_suffix())
    executable.write_bytes(b"existing executable")
    attempts = 0
    responses: List[requests.Response] = []
    delays_sec = retry_clock.delays_sec

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

    with pytest.raises(requests.RequestException):
        installer.stream_conda_executable("https://example.invalid/package.conda")

    assert executable.read_bytes() == b"existing executable"
    assert list(tmp_path.iterdir()) == [executable]
    assert attempts == 7
    assert delays_sec == [1, 2, 4, 8, 16, 32]
    for response in responses:
        assert response.raw.closed


@pytest.mark.parametrize(
    "failure", [429, 500, 502, 503, 504, "connect", "read-timeout", "interrupted"]
)
@pytest.mark.parametrize("to_file", [False, True], ids=["memory", "file"])
def test_download_retries_transient_failures(
    failure: Union[int, str],
    to_file: bool,
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"download": "complete"}'
    attempts = 0
    responses: List[requests.Response] = []
    delays_sec = retry_clock.delays_sec

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
    assert delays_sec == [1]
    if failure != "connect":
        assert responses[0].raw.closed


def test_exponential_backoff_fits_retry_time_budget(
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays_sec = retry_clock.delays_sec

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        response = requests.Response()
        response.status_code = 429
        response.raw = io.BytesIO()
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    with pytest.raises(requests.HTTPError) as error:
        installer.request_url_with_retry("https://example.invalid/package.conda")
    assert error.value.response.status_code == 429
    assert attempts == 7
    assert delays_sec == [1, 2, 4, 8, 16, 32]
    assert sum(delays_sec) == 63
    assert sum(delays_sec) <= installer.RETRY_TIME_BUDGET_SEC < sum(delays_sec) + 64


@pytest.mark.parametrize(
    "request_duration_sec, expected_starts_sec, expected_delays_sec",
    [
        (30, [0, 31, 63], [1, 2]),
        (60, [0, 61], [1]),
        (70, [0], []),
        (120, [0], []),
    ],
)
def test_retry_budget_includes_failed_request_time(
    request_duration_sec: float,
    expected_starts_sec: List[float],
    expected_delays_sec: List[float],
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_clock.now_sec = 1000.0
    started_sec = retry_clock.now_sec
    starts_sec = []
    errors = []

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        starts_sec.append(retry_clock.now_sec - started_sec)
        retry_clock.now_sec += request_duration_sec
        error = requests.ReadTimeout("Server did not respond")
        errors.append(error)
        raise error

    monkeypatch.setattr(requests.Session, "request", get)
    with pytest.raises(requests.ReadTimeout) as error:
        installer.request_url_with_retry("https://example.invalid/package.conda")

    assert error.value is errors[-1]
    assert starts_sec == expected_starts_sec
    assert retry_clock.delays_sec == expected_delays_sec
    assert all(start_sec <= installer.RETRY_TIME_BUDGET_SEC for start_sec in starts_sec)


@pytest.mark.parametrize("to_file", [False, True], ids=["memory", "file"])
@pytest.mark.parametrize("retry_first", [False, True])
def test_active_download_can_finish_after_retry_deadline(
    to_file: bool,
    retry_first: bool,
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts_sec = []

    def slow_body(chunk_size: int) -> Iterator[bytes]:
        retry_clock.now_sec += 45
        yield b"slow "
        retry_clock.now_sec += 45
        yield b"download"

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        starts_sec.append(retry_clock.now_sec)
        if retry_first and len(starts_sec) == 1:
            retry_clock.now_sec += 60
            raise requests.ReadTimeout("Server did not respond")
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO()
        monkeypatch.setattr(response, "iter_content", slow_body)
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    with io.BytesIO() as archive:
        response = installer.request_url_with_retry(
            "https://example.invalid/package.conda",
            destination=archive if to_file else None,
        )
        assert (archive.read() if to_file else response.content) == b"slow download"

    assert starts_sec == ([0, 61] if retry_first else [0])
    assert retry_clock.delays_sec == ([1] if retry_first else [])
    assert retry_clock.now_sec > installer.RETRY_TIME_BUDGET_SEC


@pytest.mark.parametrize("to_file", [False, True], ids=["memory", "file"])
@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "gzip"])
@pytest.mark.parametrize("recover", [False, True], ids=["exhausted", "recovered"])
def test_download_retries_truncated_http_body(
    to_file: bool,
    compressed: bool,
    recover: bool,
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"download": "complete"}'
    body = gzip.compress(payload) if compressed else payload
    attempts = 0
    delays_sec = retry_clock.delays_sec

    class Socket:
        def __init__(self, wire: bytes) -> None:
            self.wire = wire

        def makefile(self, *args: Any, **kwargs: Any) -> io.BytesIO:
            return io.BytesIO(self.wire)

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        headers = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
        if compressed:
            headers += "Content-Encoding: gzip\r\n"
        received = body if recover and attempts > 1 else body[:-5]
        # Parse a real HTTP response and exercise urllib3's body reader. Start
        # with the permissive urllib3 1.x default, even when testing on 2.x.
        http_response = http.client.HTTPResponse(
            Socket(headers.encode() + b"\r\n" + received)  # type: ignore[arg-type]
        )
        http_response.begin()
        response = requests.Response()
        response.status_code = 200
        response.raw = HTTPResponse(
            body=http_response,
            headers=dict(http_response.getheaders()),
            status=200,
            original_response=http_response,
            preload_content=False,
            enforce_content_length=False,
        )
        return response

    monkeypatch.setattr(requests.Session, "request", get)
    with io.BytesIO() as archive:
        if recover:
            response = installer.request_url_with_retry(
                "https://example.invalid/package.conda",
                destination=archive if to_file else None,
            )
            assert (archive.read() if to_file else response.content) == payload
            assert attempts == 2
            assert delays_sec == [1]
        else:
            with pytest.raises(requests.exceptions.ChunkedEncodingError):
                installer.request_url_with_retry(
                    "https://example.invalid/package.conda",
                    destination=archive if to_file else None,
                )
            assert attempts == 7
            assert delays_sec == [1, 2, 4, 8, 16, 32]


@pytest.mark.parametrize("status", [401, 403, 404])
@pytest.mark.parametrize("to_file", [False, True], ids=["memory", "file"])
def test_download_does_not_retry_permanent_http_errors(
    status: int, to_file: bool, retry_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    delays_sec = retry_clock.delays_sec

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
    with io.BytesIO() as archive, pytest.raises(requests.HTTPError) as error:
        installer.request_url_with_retry(
            "https://example.invalid/package.conda",
            destination=archive if to_file else None,
        )
    assert error.value.response.status_code == status
    assert attempts == 1
    assert not delays_sec


@pytest.mark.parametrize("failure", ["stall", 503, 404])
def test_download_logs_each_failure_with_details(
    failure: Union[str, int],
    retry_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "https://example.invalid/package.conda"
    attempts = 0
    delays_sec = retry_clock.delays_sec

    def get(
        session: requests.Session, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        nonlocal attempts
        attempts += 1
        if isinstance(failure, int):
            response = requests.Response()
            response.status_code = failure
            response.reason = "Service Unavailable" if failure == 503 else "Not Found"
            response.url = url
            response.raise_for_status()
        # Requests wraps a timeout during body reads in ConnectionError.
        raise requests.ConnectionError("Read timed out.")

    monkeypatch.setattr(requests.Session, "request", get)
    monkeypatch.setattr(installer, "RETRY_TIME_BUDGET_SEC", 1)
    with pytest.raises(requests.RequestException) as error:
        installer.request_url_with_retry(url)

    lines = capsys.readouterr().err.splitlines()
    retryable = failure != 404
    assert attempts == len(lines) == (2 if retryable else 1)
    assert delays_sec == ([1] if retryable else [])
    for attempt, line in enumerate(lines, start=1):
        assert f"attempt {attempt};" in line
        assert url in line
        assert type(error.value).__name__ in line
        assert str(error.value) in line
    if retryable:
        assert "retrying in 1s" in lines[0]
        assert "retrying again would exceed the remaining time budget" in lines[-1]
    else:
        assert "not retrying" in lines[-1]


def test_download_does_not_retry_disk_errors(
    retry_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnwritableArchive(io.BytesIO):
        def write(self, data: Any) -> int:
            raise OSError("Disk full")

    attempts = 0
    delays_sec = retry_clock.delays_sec

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
    with UnwritableArchive() as archive, pytest.raises(OSError, match="Disk full"):
        installer.request_url_with_retry(
            "https://example.invalid/package.conda", destination=archive
        )
    assert attempts == 1
    assert not delays_sec


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
