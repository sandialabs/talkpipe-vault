"""Optional filesystem fences for the web interface.

Two environment variables restrict which filesystem paths the web routes
accept. Both are unset by default (an empty or whitespace-only value counts
as unset), which leaves every route unrestricted — the right default for a
single-user desktop install. Containerized or shared deployments set them so
the browser UI cannot create vaults in ephemeral container paths or browse
the server's whole filesystem:

- ``TALKPIPE_VAULT_ROOT`` — a single directory; vaults may only be created,
  opened, or deleted inside it.
- ``TALKPIPE_DOCUMENT_ROOTS`` — ``os.pathsep``-separated directories; the
  folder picker and document indexing are confined to them.

Paths are fully resolved (symlinks followed) before containment checks, so a
symlink inside an allowed root that points outside of it is rejected.
"""

import os
from pathlib import Path

VAULT_ROOT_ENV = "TALKPIPE_VAULT_ROOT"
DOCUMENT_ROOTS_ENV = "TALKPIPE_DOCUMENT_ROOTS"


def vault_root() -> Path | None:
    """The directory vaults are confined to, or None when unrestricted."""
    raw = os.environ.get(VAULT_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def document_roots() -> list[Path]:
    """Directories browsing/indexing are confined to; empty when unrestricted."""
    raw = os.environ.get(DOCUMENT_ROOTS_ENV, "")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        resolved = Path(part).expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def browse_roots() -> list[Path]:
    """Union of vault and document roots for the folder picker.

    The picker chooses both vault locations and document folders, so it may
    see every configured root. Order is vault root first, then document
    roots; empty means browsing is unrestricted.
    """
    roots: list[Path] = []
    root = vault_root()
    if root is not None:
        roots.append(root)
    for doc_root in document_roots():
        if doc_root not in roots:
            roots.append(doc_root)
    return roots


def is_allowed(path: str | Path, roots: list[Path]) -> bool:
    """True when the fully-resolved path lies inside one of the roots.

    An empty roots list means unrestricted, so everything is allowed.
    """
    if not roots:
        return True
    resolved = Path(path).expanduser().resolve()
    return any(resolved.is_relative_to(root) for root in roots)


def describe(roots: list[Path]) -> str:
    """Human-readable list of allowed roots for error messages."""
    return ", ".join(str(root) for root in roots)


def startup_errors() -> list[str]:
    """Configuration problems that should fail startup loudly.

    A configured root that does not exist would otherwise lock the user out
    of every path with no hint about why.
    """
    errors: list[str] = []
    root = vault_root()
    if root is not None and not root.is_dir():
        errors.append(f"{VAULT_ROOT_ENV} points to {root}, which is not a directory.")
    for doc_root in document_roots():
        if not doc_root.is_dir():
            errors.append(f"{DOCUMENT_ROOTS_ENV} entry {doc_root} is not a directory.")
    return errors
