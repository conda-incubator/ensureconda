import io
import os
from pathlib import Path
from typing import Optional

import pytest

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
