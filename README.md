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
  --json                          print the executable's path, flavor and
                                  version as JSON
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
import ensureconda

ensureconda.ensure()  # CondaExecutable(path, flavor, version), or None
ensureconda.ensure(["micromamba>=2,<3", "conda>=24"])
ensureconda.ensure(["conda", "micromamba"], no_install=True)
ensureconda.identify("/opt/conda/bin/conda")  # Describe a given executable

# The existing API still returns only a path, or None.
ensureconda.ensureconda(micromamba=False, min_conda_version=None)
```

`ensure()` accepts an ordered sequence of target strings. Each names `mamba`,
`micromamba`, `conda`, or `conda_standalone`, optionally followed by version
constraints such as `>=2,<3` or `==2.9.*`. The default order is `mamba`,
`micromamba`, `conda`, `conda_standalone`. An empty sequence returns `None`.
Invalid targets raise `ValueError` before any search or installation.

Target names use `packaging`'s name canonicalization: case is ignored, and runs
of dots, underscores, and hyphens are equivalent. For example,
`"Conda.Standalone>=25"` and `"conda-standalone>=25"` select the same tool as
`"conda_standalone>=25"`. The result still reports the flavor as
`"conda_standalone"`.

All targets are searched for compatible installed executables before any
installation is attempted. For example, `["micromamba", "conda"]` uses an
installed conda if micromamba is absent. If nothing installed matches, the
installers are tried in target order. Only `micromamba` and `conda_standalone`
can be installed; `mamba` and `conda` select existing installations. Set
`no_install=True` to disable installation. Set `prefer_installed=False` to
search and attempt installation for each target before moving to the next.

Version constraints use Python packaging's version-specifier syntax. A single
`>=` bound also accepts local version labels, preserving the legacy API's
minimum-version comparisons.
Prereleases are considered when they satisfy the constraints. Downloads still
fetch the latest available build: constraints filter the result, but do not
select an older release to download. An unpublished build allows trying the
next target; other download failures propagate.

The result's flavor is `"mamba"`, `"micromamba"`, `"conda"`, or
`"conda_standalone"`. Its version is `None` if `--version` doesn't report one.
The version used to check a constraint is also returned as metadata. Failed
version probes cannot satisfy constraints. For compatibility, the existing
probes' `0.0.0` fallback for unrecognized output is retained for constraint
checks, but reported as `None` in metadata.

By default, `ensure()` probes versions to populate the result. Set
`probe_version=False` to run `--version` only when a target's constraints
require it; an unmeasured version is returned as `None`.

`ensureconda()` is a compatibility wrapper around `ensure()`, translating its
booleans and minimum versions into targets and passing `prefer_installed=False`
and `probe_version=False`. This preserves its signature, return type, search
and installation order, and version-probing behavior. In particular, it may
install micromamba before looking for an installed conda. The CLI continues
to prefer installed executables, both with and without `--json`.

`identify()` takes the flavor from the file name, or else from the shape of the
`--version` output, which can't tell micromamba from mamba 2, nor conda from
conda-standalone; those are reported as `"micromamba"` and `"conda"`. It raises
`FileNotFoundError` or `PermissionError` for a path that doesn't exist or isn't
executable, and `ValueError` for an executable it doesn't recognize.

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
