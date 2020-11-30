# ensureconda

[![GitHub Workflow Status](https://img.shields.io/github/workflow/status/conda-incubator/ensureconda/Python%20package)](https://github.com/conda-incubator/ensureconda/actions?query=workflow%3A%22Python+package%22)
[![Licence: MIT](https://img.shields.io/github/license/conda-incubator/ensureconda)](https://github.com/conda-incubator/ensureconda/blob/master/LICENSE-MIT)
[![PyPI](https://img.shields.io/pypi/v/ensureconda)](https://pypi.org/project/ensureconda)
[![code-style Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://https://github.com/psf/black)

## Installation

ensureconda is distributed on [PyPI](https://pypi.org) as a universal
wheel and is available on Linux, macOS and Windows and supports
Python 3.6+ and PyPy.

```bash
$ pip install ensureconda
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


## License

ensureconda is distributed under the terms of the
[MIT License](https://choosealicense.com/licenses/mit).
