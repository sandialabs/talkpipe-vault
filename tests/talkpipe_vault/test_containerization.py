"""Containerization contract tests."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_container_startup_is_web_only() -> None:
    """The default container startup should serve a vault, not watch files."""
    containerfile = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")

    assert "vault-server" in containerfile
    assert "vault-watch-into-vectordb" not in containerfile
    assert "VAULT_WATCH_DIR" not in containerfile


def test_container_sets_path_fences() -> None:
    """The image should confine vaults to the data volume and browsing to /documents."""
    containerfile = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")

    assert "TALKPIPE_VAULT_ROOT=/app/data" in containerfile
    assert "TALKPIPE_DOCUMENT_ROOTS=/documents" in containerfile


def test_containerization_does_not_use_shell_scripts() -> None:
    """Container setup should not rely on root-level shell wrappers."""
    shell_scripts = sorted(path.name for path in REPO_ROOT.glob("*.sh"))

    assert shell_scripts == []
