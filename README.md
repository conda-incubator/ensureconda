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

With `--json`, both the Python and Go CLIs print the executable's path, flavor
and version instead of just its path:

```console
$ ensureconda --json
{"path": "/home/user/.local/share/ensure-conda/micromamba", "flavor": "micromamba", "version": "2.9.0"}
```

The flavor is the matching search branch: `mamba`, `micromamba`, `conda`, or
`conda_standalone`. The version is the one used for the minimum-version check,
normalized according to PEP 440. `--json` can be combined with `--no-install`.
Compare parsed JSON values; whitespace and escaping may differ between the CLIs.

The Go CLI requires a valid UTF-8 executable path for `--json`. If the path
contains invalid UTF-8 bytes, it exits with an error and leaves stdout empty.
Plain output preserves the original path bytes.

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

`ensure()` accepts an ordered sequence of target strings. Each names `mamba`,
`micromamba`, `conda`, or `conda_standalone`, optionally followed by version
constraints such as `>=2,<3` or `==2.9.*`. The default order is `mamba`,
`micromamba`, `conda`, `conda_standalone`. An empty sequence returns `None`.
Invalid targets raise `ValueError` before any search or installation.
Target names are case insensitive; `conda-standalone` and `Conda.Standalone`
also select `conda_standalone`.

The `install` argument selects when installation is attempted. Only
`micromamba` and `conda_standalone` can be installed; `mamba` and `conda`
select existing installations.

- `"fallback"`, the default: all targets are searched for compatible installed
  executables before any installation is attempted. For example,
  `["micromamba", "conda"]` uses an installed conda if micromamba is absent.
  If nothing installed matches, the installers are tried in target order.
- `"per-target"`: each target is searched for and, if it is absent, installed
  before the next target is considered. With `["micromamba", "conda"]`,
  micromamba is installed even if a conda is already present.
- `"never"`: targets are only searched for.

Any other value raises `ValueError` before any search or installation.

Downloads are selected for a conda platform, an operating-system and
architecture combination such as `linux-64`. If ensureconda cannot determine
which build to download for the running Python environment, it still searches
for installed executables. Those may work independently of the download
mapping. If none is found and installation would otherwise have been attempted,
it raises
`ensureconda.resolve.UnsupportedPlatformError` instead of returning `None`.

Version constraints use Python packaging's version-specifier syntax. Downloads
fetch the latest available build: constraints filter the result, but do not
select an older release to download. An unpublished build allows trying the
next target; other download failures propagate.

Later releases may add fields to the result or keys to `ensureconda --json`.

The result's flavor is `"mamba"`, `"micromamba"`, `"conda"`, or
`"conda_standalone"`. Its version is `None` if `--version` doesn't report one.
The version used to check a constraint is also returned as metadata. `ensure()`
requires a known version to satisfy a constraint.

ensureconda saves downloaded conda-standalone executables as `conda_standalone`
(`conda_standalone.exe` on Windows), so that filename is used when searching
for an existing standalone installation.

By default, `ensure()` runs `--version` on each candidate it considers:

- If the command succeeds and reports a recognized version, that version is
  returned as metadata.
- If it succeeds but the version cannot be parsed or the output cannot be
  decoded, an unconstrained target can still return it with `version=None`.
- If the command cannot start or exits unsuccessfully, `ensure()` reports the
  failure on stderr and skips the candidate. It continues with other candidates,
  later targets, and installation according to the selected search order.

This check also applies to candidates returned by an installer. An installer
reuses an executable already in ensureconda's data directory only if it passes
the same check, and otherwise downloads a new one.
Set `probe_version=False` to run `--version` only when a target's constraints
require it. For an unconstrained target, this also disables checking that the
executable runs, and the returned version is `None`.

`ensureconda()`, the old API, keeps its boolean options, minimum-version
arguments, path-or-None result, and original search and installation order.
It runs version probes only when a minimum is specified. In particular, it
may install micromamba before looking for an installed conda. The CLI continues
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
