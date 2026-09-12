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


def test_container_config_declares_no_variables_nothing_reads() -> None:
    """Every documented container variable must actually be consumed.

    VAULT_PATH was presented as the way to choose the container's vault and
    VAULT_HOST as its bind address, but no code read either: the image's CMD
    fixes host and port, and `--resume` plus the web interface decide which
    vault is open. Settings that look configured but do nothing are worse
    than absent, so they must not come back.
    """
    containerfile = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for assignment in ("VAULT_PATH=", "VAULT_HOST="):
        assert f"ENV {assignment}" not in containerfile
        assert not any(
            line.strip().startswith(assignment) for line in env_example.splitlines()
        )


def test_compose_documents_mount_is_an_absolute_required_path() -> None:
    """No compose provider expands "~" in a volume mapping.

    A `~/Documents` default bound a literal "~" directory under Docker
    Compose while podman-compose expanded it, so the same file behaved
    differently on the two providers the header claims to support.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    mounts = [
        line.strip()
        for line in compose.splitlines()
        if ":/documents:ro" in line and line.strip().startswith("-")
    ]
    assert mounts, "no /documents mount found in docker-compose.yml"
    for mount in mounts:
        assert "~" not in mount
        assert "${VAULT_DOCUMENTS_DIR:?" in mount


def test_compose_healthcheck_probes_the_running_server() -> None:
    """The healthcheck must be able to fail.

    `import talkpipe_vault` succeeds whether or not the web application is
    listening, so it could never report an unhealthy container and
    `restart: unless-stopped` could never act on one.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    healthcheck_tests = [
        line for line in compose.splitlines() if line.strip().startswith("test:")
    ]
    assert healthcheck_tests
    for line in healthcheck_tests:
        assert "/api/health" in line
        assert "import talkpipe_vault" not in line
