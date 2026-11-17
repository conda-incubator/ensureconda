from typing import Callable, get_type_hints

import pytest

from ensureconda import api, installer


@pytest.mark.parametrize(
    "function",
    [
        api.ensure,
        api.ensureconda,
        api.identify,
        api.determine_conda_version,
        api.determine_mamba_version,
        api.determine_micromamba_version,
        installer.install_conda_exe,
        installer.install_micromamba,
    ],
)
def test_public_annotations_can_be_evaluated(function: Callable[..., object]) -> None:
    """Documentation and runtime type tools must work on every supported Python."""
    assert get_type_hints(function)
