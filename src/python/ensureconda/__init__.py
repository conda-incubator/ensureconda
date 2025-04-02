from .api import ensureconda

try:
    from ._version import version as __version__
except ImportError:
    from importlib.metadata import version

    __version__ = version("ensureconda")
