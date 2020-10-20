import os
import pathlib
import shutil
import subprocess

import pytest


@pytest.fixture()
def golang_exe():
    exe = os.environ.get("ENSURECONDA_EXE")
    if exe is not None:
        if os.path.sep in exe:
            p = pathlib.Path(exe).resolve()
        else:
            p = pathlib.Path(__file__).parent / exe
        if p.exists():
            return str(p)


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--no-micromamba"],
    ],
)
def test_install(golang_exe, flags):
    if golang_exe is None:
        raise pytest.skip("environment variable not set")
    args = [golang_exe]
    args.extend(flags)
    print(args)
    result = subprocess.check_output(args, encoding="utf8")
    subprocess.check_call([result, "--help"])


def test_find():
    pass
