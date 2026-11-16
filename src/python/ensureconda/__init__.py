from .api import CondaExecutable, ensure, ensureconda, identify

__all__ = ["CondaExecutable", "ensure", "ensureconda", "identify", "__version__"]

try:
    from ._version import version as __version__
except ImportError:
    from importlib.metadata import version

    __version__ = version("ensureconda")
