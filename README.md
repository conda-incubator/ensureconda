# ensureconda

[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/conda-incubator/ensureconda/test.yml?branch=main)](https://github.com/conda-incubator/ensureconda/actions?query=workflow%3A%22Python+package%22)
[![Licence: MIT](https://img.shields.io/github/license/conda-incubator/ensureconda)](https://github.com/conda-incubator/ensureconda/blob/master/LICENSE-MIT)
[![PyPI](https://img.shields.io/pypi/v/ensureconda)](https://pypi.org/project/ensureconda)
[![code-style Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://https://github.com/psf/black)

## Installation

### Python-based

ensureconda is distributed on [PyPI](https://pypi.org) as a universal
wheel and is available on Linux, macOS and Windows and supports
Python 3.8+ and PyPy.

```bash
$ pip install ensureconda
```

### Go based

Additionally ensureconda is also available as a statically linked fully stand-alone
golang binary.  This is a full reimplementation with the same cli as the python version

These can be downloaded from [releases](https://github.com/conda-incubator/ensureconda/releases/latest)

Alternatively if you have go installed you can install from githubg using

```shell
$ go install github.com/conda-incubator/ensureconda@latest
$ ensureconda --help
```

## Usage

Ensureconda is a cli tool that will

1. Find a preexisting conda/mamba executable
2. Install one if nothing was found and installation is allowed.
3. Return the path of the executable found/installed on stdout

```
ensureconda --help
Usage: ensureconda [OPTIONS]

  Ensures that a conda/mamba is installed.

Options:
  --mamba / --no-mamba            search for mamba
  --micromamba / --no-micromamba  search for micromamba, install if not
                                  present

  --conda / --no-conda            search for conda
  --conda-exe / --no-conda-exe    search for conda.exe / conda-standalone,
                                  install if not present

  --no-install                    don't install conda/mamba if no version can
                                  be discovered

  --min-conda-version VERSIONNUMBER
                                  minimum version of conda to accept (defaults
                                  to 4.8.2)

  --min-mamba-version VERSIONNUMBER
                                  minimum version of mamba/micromamba to
                                  accept (defaults to 0.7.3)

  --help                          Show this message and exit.
```

With `--json`, ensureconda prints the executable's path, flavor and version
instead of just its path:

```console
$ ensureconda --json
{"path": "/home/user/.local/share/ensure-conda/micromamba", "flavor": "micromamba", "version": "2.9.0"}
```

### Python API

```python
from ensureconda import ensureconda, ensureconda_info, identify

ensureconda()  # the path of a conda executable, or None
ensureconda_info()  # CondaExecutable(path, flavor, version), or None
identify("/opt/conda/bin/conda")  # CondaExecutable for a given executable
```

`ensureconda_info()` accepts the same arguments as `ensureconda()`. The flavor
is `"mamba"`, `"micromamba"`, `"conda"` or `"conda_standalone"`, and the version
is `None` if `--version` doesn't report one. `identify()` takes the flavor from
the file name, or else from the shape of the `--version` output, which can't tell
micromamba from mamba 2; both are reported as `"micromamba"`.

### Configuring conda-standalone channel

When ensureconda downloads the conda-standalone executable, you can select the
channel via the environment variable `ENSURECONDA_CONDA_STANDALONE_CHANNEL`.
It defaults to `conda-forge`, and you can set it to `anaconda` if you prefer:

```bash
export ENSURECONDA_CONDA_STANDALONE_CHANNEL=anaconda
ensureconda --conda-exe
```

The corresponding package page used is:

```text
https://anaconda.org/$ENSURECONDA_CONDA_STANDALONE_CHANNEL/conda-standalone
```

## License

ensureconda is distributed under the terms of the
[MIT License](https://choosealicense.com/licenses/mit).
