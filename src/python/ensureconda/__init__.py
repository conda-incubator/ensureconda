from .api import CondaExecutable, ensureconda, identify

__all__ = ["CondaExecutable", "ensureconda", "identify", "__version__"]

try:
    from ._version import version as __version__
except ImportError:
    from importlib.metadata import version

    __version__ = version("ensureconda")
