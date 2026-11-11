import pytest
from click.testing import CliRunner

from ensureconda.cli import ensureconda_cli


@pytest.mark.parametrize("option", ["--min-conda-version", "--min-mamba-version"])
def test_invalid_version_is_a_usage_error(option: str) -> None:
    result = CliRunner().invoke(ensureconda_cli, [option, "4.8,2", "--no-install"])

    assert result.exit_code == 2
    assert "'4.8,2' is not a valid version" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
