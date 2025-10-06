import os
import pathlib
import shutil
import subprocess
from typing import List, Optional

import pytest


@pytest.fixture()
def golang_exe() -> Optional[str]:
    exe = os.environ.get("ENSURECONDA_EXE")
    if exe is not None:
        if os.path.sep in exe:
            p = pathlib.Path(exe).resolve()
            if p.exists():
                return str(p)
        else:
            p = pathlib.Path(__file__).parent / exe
            if p.exists():
                return str(p)
            p = pathlib.Path(os.getcwd()) / exe
            if p.exists():
                return str(p)
    return None


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param([], id="default"),
        pytest.param(["--no-micromamba"], id="no-micromamba"),
    ],
)
def test_install(golang_exe: Optional[str], flags: List[str]) -> None:
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    args = [golang_exe, "--verbosity=3"]
    args.extend(flags)
    print(args)
    result = subprocess.check_output(args, encoding="utf8")
    subprocess.check_call([result, "--help"])


def test_find() -> None:
    pass
