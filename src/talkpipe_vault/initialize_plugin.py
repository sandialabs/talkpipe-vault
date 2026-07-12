"""Plugin initialization module for talkpipe_vault.

This module is loaded by talkpipe's plugin system.
"""

import logging

logger = logging.getLogger(__name__)


def initialize_plugin() -> None:
    """Initialize the talkpipe_vault plugin.

    Sources and segments are registered via the ``talkpipe.sources`` and
    ``talkpipe.segments`` entry points in pyproject.toml, so no additional
    setup is required here.
    """
