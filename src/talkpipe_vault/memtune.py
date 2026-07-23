"""Limit glibc malloc arenas so long ingestions return memory to the OS.

LanceDB's native writer allocates from many worker threads; glibc gives
thread groups their own malloc arenas, and memory freed on those threads
parks in the arenas instead of returning to the OS. Over a long document
ingestion this looks like an unbounded leak — measured at roughly 10-17 KiB
of resident memory per indexed chunk, never released — and OOM-kills
memory-capped containers. Capping the arena count keeps ingestion memory
flat (repeat runs added ~1/10th the RSS in measurement) at no measured
throughput cost.

The cap must be applied before the allocating threads exist, so call
``limit_malloc_arenas`` at process startup. It is a no-op on platforms
without glibc (macOS, musl) and defers to an explicit MALLOC_ARENA_MAX
environment variable (as set in the container image).
"""

import ctypes
import logging
import os

logger = logging.getLogger(__name__)

# glibc mallopt parameter number for M_ARENA_MAX (malloc.h).
_M_ARENA_MAX = -8


def limit_malloc_arenas(max_arenas: int = 2) -> bool:
    """Cap the number of glibc malloc arenas for this process.

    Returns True when the cap is in effect (applied here or already set via
    the MALLOC_ARENA_MAX environment variable), False when unavailable on
    this platform.
    """
    if os.environ.get("MALLOC_ARENA_MAX"):
        return True
    try:
        libc = ctypes.CDLL("libc.so.6")
        applied = bool(libc.mallopt(_M_ARENA_MAX, max_arenas))
    except (OSError, AttributeError):
        return False
    if not applied:
        logger.debug("mallopt(M_ARENA_MAX, %d) was not accepted", max_arenas)
    return applied
