"""
CLI entry point for running the web interface.
"""
import argparse
import sys
from pathlib import Path

from talkpipe.util.config import configure_logger

from talkpipe_vault.apps.query import run_app

configure_logger("root:ERROR")

def main() -> None:
    """CLI entry point for running the web interface only."""
    parser = argparse.ArgumentParser(
        description="Run the TalkPipe Vault web interface for searching and chat"
    )
    parser.add_argument(
        "vault_path",
        help="Path to vault storage directory"
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

    args = parser.parse_args()

    vault_path = Path(args.vault_path)
    vault_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Starting TalkPipe Vault")
    print("=" * 60)
    print(f"Vault storage: {vault_path}")
    print(f"Web interface: http://{args.host}:{args.port}")
    print()

    # Start the web application (this will block)
    print("Starting web interface...")
    try:
        run_app(
            vault_path=str(vault_path),
            host=args.host,
            port=args.port
        )
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"Error starting web interface: {e}", file=sys.stderr)
        sys.exit(1)

