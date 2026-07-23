"""Tests for the glibc malloc-arena cap."""

import ctypes

from talkpipe_vault import memtune


def test_env_var_takes_precedence(monkeypatch):
    """An explicit MALLOC_ARENA_MAX (e.g. the container) is left in charge."""
    monkeypatch.setenv("MALLOC_ARENA_MAX", "2")

    def fail_if_loaded(*args, **kwargs):
        raise AssertionError("mallopt must not run when the env var governs")

    monkeypatch.setattr(ctypes, "CDLL", fail_if_loaded)
    assert memtune.limit_malloc_arenas() is True


def test_applies_or_degrades_gracefully(monkeypatch):
    """On glibc the cap applies; elsewhere it reports False, never raises."""
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    assert memtune.limit_malloc_arenas() in (True, False)


def test_missing_libc_reports_false(monkeypatch):
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)

    def no_libc(*args, **kwargs):
        raise OSError("no libc here")

    monkeypatch.setattr(ctypes, "CDLL", no_libc)
    assert memtune.limit_malloc_arenas() is False
