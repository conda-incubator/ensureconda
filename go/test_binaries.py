import os
import subprocess

import pytest


@pytest.fixture()
def exe():
    return os.environ["ENSURECONDA_EXE"]


@pytest.mark.parametrize("flags", [[[]], [["--no-micromamba"]]])
def test_install(exe, flags):
    result = subprocess.check_output(exe)
    subprocess.check_call([result, "--help"])


def test_find():
    pass
