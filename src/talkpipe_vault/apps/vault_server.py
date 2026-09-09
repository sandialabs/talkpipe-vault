"""
CLI entry point for running the web interface.
"""

import argparse
import sys
import webbrowser
from pathlib import Path

from talkpipe.util.config import configure_logger

from talkpipe_vault import memtune
from talkpipe_vault.apps import access_control, user_settings
from talkpipe_vault.apps.query import _port_in_use, _running_instance_url, run_app
from talkpipe_vault.pipelines.config import ensure_supported_vault_layout

configure_logger("root:ERROR")

DEFAULT_PORT = 8002
PORT_SEARCH_RANGE = 20
"""How many ports above the default to try when the default is taken."""


def _choose_port(host: str, requested: int | None) -> int:
    """The port to bind: the requested one, or the default with fallback.

    An explicit ``--port`` is honoured or fails loudly. With no explicit
    port, the default is used when free; when another program holds it,
    the next free port in a small range above it is used and announced, so
    a launch from a desktop entry still comes up instead of dying with a bind
    error.
    """
    if requested is not None:
        if _port_in_use(host, requested):
            print(
                f"Error: cannot bind to {host}:{requested} — the address is "
                "already in use.\nStop the other process using the port, "
                "or start with --port <other-port>.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return requested
    if not _port_in_use(host, DEFAULT_PORT):
        return DEFAULT_PORT
    for candidate in range(DEFAULT_PORT + 1, DEFAULT_PORT + 1 + PORT_SEARCH_RANGE):
        if not _port_in_use(host, candidate):
            print(
                f"Port {DEFAULT_PORT} is in use by another program; "
                f"using port {candidate} instead."
            )
            return candidate
    print(
        f"Error: ports {DEFAULT_PORT}-{DEFAULT_PORT + PORT_SEARCH_RANGE} are all "
        f"in use on {host}. Start with --port <other-port>.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _most_recent_usable_vault() -> str:
    """Return the most recently opened vault that is still usable.

    Walks the recent-vault list recorded by the web interface, skipping entries
    that no longer exist, fall outside the configured vault root, or have an
    unsupported layout. Returns "" when none qualify.
    """
    for candidate in user_settings.get_recent_vaults():
        path = Path(candidate).expanduser()
        if not path.is_dir():
            continue
        if not access_control.vault_path_allowed(str(path)):
            continue
        try:
            ensure_supported_vault_layout(str(path))
        except ValueError:
            continue
        return str(path)
    return ""


def main() -> None:
    """CLI entry point for running the web interface only."""
    parser = argparse.ArgumentParser(
        description="Run the TalkPipe Vault web interface for searching and chat"
    )
    parser.add_argument(
        "vault_path",
        nargs="?",
        default="",
        help=(
            "Path to LanceDB directory (same semantics as makevectordatabase "
            "--path). Whoosh index lives at vault_path/fulltext_vault. When "
            "omitted, create or choose a vault from the web interface."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind web interface to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"Port to listen on (default: {DEFAULT_PORT}). Without this option, "
            "the next free port above the default is used when another "
            "program holds it."
        ),
    )
    parser.add_argument(
        "--show-source-paths",
        action="store_true",
        help=(
            "Show source file paths in search results and enable HTTP links to "
            "those files. Hidden by default."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the app in a web browser on startup.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Open the most recently used vault (as recorded by the web "
            "interface). Falls back to vault_path — or the vault manager "
            "page — when no recent vault is usable."
        ),
    )

    args = parser.parse_args()

    # A second launch (a launcher clicked again, say) should not
    # fail: if this application already serves the port, just open it.
    running = _running_instance_url(
        args.host, args.port if args.port is not None else DEFAULT_PORT
    )
    if running:
        print(f"TalkPipe Vault is already running at {running}")
        if not args.no_browser:
            print("Opening it in your web browser...")
            webbrowser.open(running)
        return
    port = _choose_port(args.host, args.port)

    # Before any worker threads exist: keep LanceDB ingestion memory flat
    # (see talkpipe_vault.memtune).
    memtune.limit_malloc_arenas()

    vault_path = ""
    resumed = False
    if args.resume:
        vault_path = _most_recent_usable_vault()
        resumed = bool(vault_path)
    if not vault_path and args.vault_path:
        vault_path = str(Path(args.vault_path).expanduser())
        try:
            Path(vault_path).mkdir(parents=True, exist_ok=True)
            ensure_supported_vault_layout(vault_path)
        except (OSError, ValueError) as exc:
            print(f"Error opening vault at {vault_path}: {exc}", file=sys.stderr)
            sys.exit(1)

    print("=" * 60)
    print("Starting TalkPipe Vault")
    print("=" * 60)
    if resumed:
        print(f"Vault storage: {vault_path} (most recently used)")
    elif vault_path:
        print(f"Vault storage: {vault_path}")
    else:
        print("Vault storage: none selected yet — create or choose a vault")
        print("in the web interface after it starts.")
    print(f"Web interface: http://{args.host}:{port}")
    if not args.no_browser:
        print("Opening in your web browser...")
    print()

    # Start the web application (this will block)
    print("Starting web interface...")
    try:
        run_app(
            vault_path=vault_path,
            host=args.host,
            port=port,
            show_source_paths=args.show_source_paths,
            open_browser=not args.no_browser,
        )
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"Error starting web interface: {e}", file=sys.stderr)
        sys.exit(1)
