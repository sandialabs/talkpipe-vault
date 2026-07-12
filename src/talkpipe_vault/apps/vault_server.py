"""
CLI entry point for running the web interface.
"""
import argparse
import sys
from pathlib import Path

from talkpipe.util.config import configure_logger

from talkpipe_vault.apps.query import run_app
from talkpipe_vault.pipelines.config import ensure_supported_vault_layout

configure_logger("root:ERROR")

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
        help="Host to bind web interface to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8002,
        help="Port to listen on (default: 8002)"
    )
    parser.add_argument(
        "--show-source-paths",
        action="store_true",
        help=(
            "Show source file paths in search results and enable HTTP links to "
            "those files. Hidden by default."
        ),
    )

    args = parser.parse_args()

    vault_path = ""
    if args.vault_path:
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
    if vault_path:
        print(f"Vault storage: {vault_path}")
    else:
        print("Vault storage: none selected yet — create or choose a vault")
        print("in the web interface after it starts.")
    print(f"Web interface: http://{args.host}:{args.port}")
    print()

    # Start the web application (this will block)
    print("Starting web interface...")
    try:
        run_app(
            vault_path=vault_path,
            host=args.host,
            port=args.port,
            show_source_paths=args.show_source_paths,
        )
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"Error starting web interface: {e}", file=sys.stderr)
        sys.exit(1)
