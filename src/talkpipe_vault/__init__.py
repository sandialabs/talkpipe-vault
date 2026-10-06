"""TalkPipe Vault - AI-powered personal information assistant.

This package provides TalkPipe Vault components and plugin initialization.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# The distribution name on PyPI; also how a running server identifies itself.
DIST_NAME = "talkpipe-vault"

try:
    __version__ = _dist_version(DIST_NAME)
except PackageNotFoundError:  # a checkout that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["DIST_NAME", "__version__"]
