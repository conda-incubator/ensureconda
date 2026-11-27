# ensureconda

[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/conda-incubator/ensureconda/test.yml?branch=main)](https://github.com/conda-incubator/ensureconda/actions/workflows/test.yml)
[![Licence: MIT](https://img.shields.io/github/license/conda-incubator/ensureconda)](https://github.com/conda-incubator/ensureconda/blob/main/LICENSE-MIT)
[![PyPI](https://img.shields.io/pypi/v/ensureconda)](https://pypi.org/project/ensureconda)
[![code-style Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

## Installation

### Python-based

ensureconda is distributed on [PyPI](https://pypi.org) as a universal
wheel and is available on Linux, macOS and Windows and supports
Python 3.8+ and PyPy.

```bash
$ pip install ensureconda
```

It's also available from conda-forge:

```bash
$ conda install -c conda-forge ensureconda
```

### Go based

ensureconda is also available as a stand-alone Go binary; the Linux builds are
statically linked. It finds or installs an executable and prints its path.

These can be downloaded from [releases](https://github.com/conda-incubator/ensureconda/releases/latest)

Alternatively, if you have Go installed, you can install it from GitHub using

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
  --force-install                 skip existing executables and download a
                                  fresh copy
  --min-conda-version VERSIONNUMBER
                                  minimum version of conda to accept (defaults
                                  to 4.8.2)
  --min-mamba-version VERSIONNUMBER
                                  minimum version of mamba/micromamba to
                                  accept (defaults to 0.7.3)
  --json                          print the executable's path, flavor and
                                  version as JSON
  -v, --verbosity INTEGER RANGE   0: warnings; 1: decisions; 2-3: debug
                                  details  [default: 1; 0<=x<=3]
  --help                          Show this message and exit.
```

Use `--force-install` to download a fresh executable into ensureconda's data
directory, even if a compatible copy exists there or on `PATH`. To refresh
conda-standalone instead of micromamba, use
`ensureconda --force-install --no-micromamba`.

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
ensureconda.ensure(["conda", "micromamba"], install="never")
ensureconda.identify("/opt/conda/bin/conda")  # Describe a given executable

# The old API still returns only a path, or None.
ensureconda.ensureconda(micromamba=False, min_conda_version=None)
```

`ensure()` tries an ordered sequence of targets, by default `mamba`,
`micromamba`, `conda` and `conda_standalone`. A target can add version
constraints in Python packaging's version-specifier syntax, such as `>=2,<3` or
`==2.9.*`. The result's version is `None` if the executable doesn't report one,
and such an executable can't satisfy a constraint. Later releases may add
fields to the result or keys to `ensureconda --json`.

Version constraints skip prereleases unless you request one, as in
`micromamba>=2.0rc1`. A plain `micromamba` target can use an installed
prerelease and downloads a stable release if needed.

The `install` argument selects when installation is attempted. Only
`micromamba` and `conda_standalone` can be installed.

- `"fallback"`, the default: use any installed target before installing one.
  With `["micromamba", "conda"]`, an installed conda is used if micromamba is
  absent.
- `"per-target"`: search for and install each target in turn, so
  `["micromamba", "conda"]` installs micromamba even if conda is present.
- `"never"`: only search for installed executables.
- `"force"`: skip installed and cached executables and download fresh copies.

When installing, ensureconda downloads the newest published release that
satisfies the target, so `ensure(["micromamba<2"])` can install micromamba 1.x.
A download that fails to run or doesn't satisfy the target never replaces the
cached executable. For unconstrained targets, `probe_version=False` skips
checking that installed executables run and returns `version=None`.

`ensureconda()`, the old API, keeps its options, its path-or-`None` result and
its original search order.

### Diagnostics

Both command lines write diagnostics to stderr, keeping stdout for the
executable path or `--json` result; `--verbosity` selects how much they say.
The Python API logs its decisions at INFO under the `ensureconda` logger.

### Cache location

By default, ensureconda stores downloaded executables in:

- Linux: `~/.local/share/ensure-conda` (or `$XDG_DATA_HOME/ensure-conda` if set)
- macOS: `~/Library/Application Support/ensure-conda`
- Windows: `%LOCALAPPDATA%\ensure-conda\ensure-conda`

To use a different directory, set `ENSURECONDA_DATA_DIR` to its path.
This applies to both Python and Go.

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
