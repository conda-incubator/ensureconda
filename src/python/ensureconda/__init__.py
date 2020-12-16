import pkg_resources

from .api import ensureconda


try:
    __version__ = pkg_resources.get_distribution("ensureconda").version
except Exception:
    __version__ = "unknown"
