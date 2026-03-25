"""
CLI entry point for running both the file watcher and web interface together.
"""
import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from talkpipe.pipe.basic import ToDict
from talkpipe.pipe.io import Print
from talkpipe.util.config import configure_logger

from talkpipe_vault.apps.query import run_app
from talkpipe_vault.pipelines.building_and_watching import watch_into_vector_db

configure_logger("root:ERROR")

# Global flag for graceful shutdown
_shutdown_event = threading.Event()
_watcher_thread = None


def signal_handler(sig, frame):
    """Handle interrupt signals for graceful shutdown."""
    print("\nShutting down...")
    _shutdown_event.set()
    sys.exit(0)


def run_watcher_in_thread(source_path: str, vault_path: str, polling: bool = False, debounce_seconds: float = 2.0, patterns=None, ignore_patterns=None):
    """Run the watcher in a separate thread."""
    global _watcher_thread
    
    def watcher_worker():
        try:
            # Build the pipeline directly (same as watch_vectordb_main but without argparse)
            pipeline = watch_into_vector_db(
                source_path=source_path,
                vault_path=vault_path,
                patterns=patterns,
                ignore_patterns=ignore_patterns,
                ignore_directories=True,
                case_sensitive=False,
                max_events=None,
                polling=polling,
                ignore_common=True,
                overwrite=False,
                delete_after_reading=False,
                debounce_seconds=debounce_seconds
            ) | \
            ToDict(field_list="shingle_id") | \
            Print()
            
            # Consume the pipeline (this is an infinite generator, so iterate until shutdown)
            pipeline_iter = pipeline()
            for _ in pipeline_iter:
                if _shutdown_event.is_set():
                    break
        except KeyboardInterrupt:
            # Expected on shutdown
            pass
        except Exception as e:
            if not _shutdown_event.is_set():
                print(f"Watcher error: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
            _shutdown_event.set()
    
    _watcher_thread = threading.Thread(target=watcher_worker, daemon=True)
    _watcher_thread.start()
    return _watcher_thread


def main() -> None:
    """CLI entry point for running both watcher and web interface."""
    parser = argparse.ArgumentParser(
        description="Run both the file watcher and web interface for TalkPipe Vault"
    )
    parser.add_argument(
        "watch_path",
        help="Path to directory to watch for file changes"
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
    parser.add_argument(
        "--polling",
        action="store_true",
        default=False,
        help="Use polling-based file watcher (default: native observer)"
    )
    parser.add_argument(
        "--debounce-seconds",
        type=float,
        default=2.0,
        help="Seconds to wait for file stability before processing (default: 2.0)"
    )
    parser.add_argument(
        "--patterns",
        nargs="+",
        default=None,
        help="Glob patterns to match files (passed to watcher)"
    )
    parser.add_argument(
        "--ignore-patterns",
        nargs="+",
        default=None,
        help="Glob patterns to ignore (passed to watcher)"
    )

    args = parser.parse_args()

    # Validate paths
    watch_path = Path(args.watch_path)
    if not watch_path.exists():
        print(f"Error: Watch directory does not exist: {watch_path}", file=sys.stderr)
        sys.exit(1)
    if not watch_path.is_dir():
        print(f"Error: Watch path is not a directory: {watch_path}", file=sys.stderr)
        sys.exit(1)

    vault_path = Path(args.vault_path)
    vault_path.mkdir(parents=True, exist_ok=True)

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 60)
    print("Starting TalkPipe Vault")
    print("=" * 60)
    print(f"Watch directory: {watch_path}")
    print(f"Vault storage: {vault_path}")
    print(f"Web interface: http://{args.host}:{args.port}")
    print()

    # Start the file watcher in a background thread
    print("Starting file watcher...")
    watcher_thread = run_watcher_in_thread(
        source_path=str(watch_path),
        vault_path=str(vault_path),
        polling=args.polling,
        debounce_seconds=args.debounce_seconds,
        patterns=args.patterns,
        ignore_patterns=args.ignore_patterns
    )
    
    # Give the watcher a moment to initialize
    time.sleep(2)
    print("File watcher started")
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
        signal_handler(signal.SIGINT, None)
    except Exception as e:
        print(f"Error starting web interface: {e}", file=sys.stderr)
        _shutdown_event.set()
        sys.exit(1)

