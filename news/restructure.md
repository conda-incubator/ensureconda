### Added:

* Added a public api endpoint so that this library is useful in
  non-cli usage.
  
  ```python
  def ensureconda.ensureconda(
    *,
    mamba: bool = True,
    micromamba: bool = True,
    conda: bool = True,
    conda_exe: bool = True,
    no_install: bool = False
  ) -> Optional[PathLike]:
    ...
  ```

### Changed:

* Added dependency on [filelock](https://pypi.org/project/filelock/).

### Deprecated:

* <news item>

### Removed:

* <news item>

### Fixed:

* When installing now acquire a lock on the installation.  This should
  reduce some contention when running ensureconda concurrently.

### Security:

* <news item>