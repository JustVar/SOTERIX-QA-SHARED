"""Shared QA pipeline for the Soterix QA repos."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("soterix-qa-shared")
except PackageNotFoundError:      # running from a source checkout
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
