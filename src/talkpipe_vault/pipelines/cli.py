"""CLI entry points for building and watching pipelines."""
import argparse

from talkpipe.util.config import configure_logger
from talkpipe.pipe.basic import ToDict
from talkpipe.pipe.io import Print
from talkpipe_vault.pipelines.building_and_watching import (
    list_into_vector_db,
    watch_into_vector_db,
)

configure_logger("root:ERROR")

def watch_vectordb_main() -> None:
    """Watch a directory and build vector database and full-text index from file changes."""
    parser = argparse.ArgumentParser(
        description="Watch a directory and build vector database and full-text index from file changes"
    )
    parser.add_argument(
        "source_path", help="Path to directory to watch"
    )
    parser.add_argument(
        "--vault-path", required=True,
        help="Base path for vault storage. Vector DB at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"
    )
    parser.add_argument(
        "--patterns", nargs="+", default=None, help="Glob patterns to match"
    )
    parser.add_argument(
        "--ignore-patterns", nargs="+", default=None, help="Glob patterns to ignore"
    )
    parser.add_argument(
        "--include-directories", action="store_true", default=False,
        help="Include directory events (default: ignore directories)"
    )
    parser.add_argument(
        "--case-sensitive", action="store_true", default=False,
        help="Case-sensitive pattern matching (default: case-insensitive)"
    )
    parser.add_argument(
        "--max-events", type=int, default=None, help="Maximum number of events to process"
    )
    parser.add_argument(
        "--polling", action="store_true", default=False,
        help="Use polling-based observer (default: native observer)"
    )
    parser.add_argument(
        "--include-common", action="store_true", default=False,
        help="Include common temp/hidden files (default: ignore common files)"
    )
    parser.add_argument(
        "--overwrite", action="store_true", default=False,
        help="Overwrite existing tables and indexes (default: don't overwrite)"
    )

    args = parser.parse_args()

    # Run the pipeline
    pipeline = watch_into_vector_db(
        source_path=args.source_path,
        vault_path=args.vault_path,
        patterns=args.patterns,
        ignore_patterns=args.ignore_patterns,
        ignore_directories=not args.include_directories,
        case_sensitive=args.case_sensitive,
        max_events=args.max_events,
        polling=args.polling,
        ignore_common=not args.include_common,
        overwrite=args.overwrite
    ) | \
    ToDict(field_list="id") | \
    Print()

    # Consume the pipeline (call it to get the iterator)
    list(pipeline())


def list_vectordb_main() -> None:
    """List files matching pattern and build vector database and full-text index."""
    parser = argparse.ArgumentParser(
        description="List files matching pattern and build vector database and full-text index"
    )
    parser.add_argument(
        "source_pattern", help="Glob pattern for files to process"
    )
    parser.add_argument(
        "--vault-path", required=True,
        help="Base path for vault storage. Vector DB at vault_path/vector_vault, full-text index at vault_path/fulltext_vault"
    )
    parser.add_argument(
        "--overwrite", action="store_true", default=False,
        help="Overwrite existing tables and indexes (default: don't overwrite)"
    )

    args = parser.parse_args()

    # Run the pipeline
    pipeline = list_into_vector_db(
        source_pattern=args.source_pattern,
        vault_path=args.vault_path,
        overwrite=args.overwrite,
    ) | \
    ToDict(field_list="id") | \
    Print()

    # Consume the pipeline (call it to get the iterator)
    list(pipeline())


