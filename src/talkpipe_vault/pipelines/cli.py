"""CLI entry points for building and watching pipelines."""
import argparse
import json
import sys

from talkpipe_vault.pipelines.building_and_watching import (
    build_vector_db_from_paths,
    list_into_vector_db,
    watch_into_vector_db,
)


def watch_vectordb_main() -> None:
    """Watch a directory and build vector database from file changes."""
    parser = argparse.ArgumentParser(
        description="Watch a directory and build vector database from file changes"
    )
    parser.add_argument(
        "source_path", help="Path to directory to watch"
    )
    parser.add_argument(
        "--vectordb-path", required=True, help="Path to LanceDB database"
    )
    parser.add_argument(
        "--embedding-model", required=True, help="Embedding model to use"
    )
    parser.add_argument(
        "--embedding-source", required=True, help="Source of text to embed"
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
        help="Overwrite existing table (default: don't overwrite)"
    )

    args = parser.parse_args()

    # Run the pipeline
    pipeline = watch_into_vector_db(
        source_path=args.source_path,
        vectordb_path=args.vectordb_path,
        embedding_model=args.embedding_model,
        embedding_source=args.embedding_source,
        patterns=args.patterns,
        ignore_patterns=args.ignore_patterns,
        ignore_directories=not args.include_directories,
        case_sensitive=args.case_sensitive,
        max_events=args.max_events,
        polling=args.polling,
        ignore_common=not args.include_common,
        overwrite=args.overwrite,
    )

    # Consume the pipeline (call it to get the iterator)
    for item in pipeline():
        print(json.dumps(item, default=str))


def list_vectordb_main() -> None:
    """List files matching pattern and build vector database."""
    parser = argparse.ArgumentParser(
        description="List files matching pattern and build vector database"
    )
    parser.add_argument(
        "source_pattern", help="Glob pattern for files to process"
    )
    parser.add_argument(
        "--vectordb-path", required=True, help="Path to LanceDB database"
    )
    parser.add_argument(
        "--embedding-model", required=True, help="Embedding model to use"
    )
    parser.add_argument(
        "--embedding-source", required=True, help="Source of text to embed"
    )
    parser.add_argument(
        "--overwrite", action="store_true", default=False,
        help="Overwrite existing table (default: don't overwrite)"
    )

    args = parser.parse_args()

    # Run the pipeline
    pipeline = list_into_vector_db(
        source_pattern=args.source_pattern,
        vectordb_path=args.vectordb_path,
        embedding_model=args.embedding_model,
        embedding_source=args.embedding_source,
        overwrite=args.overwrite,
    )

    # Consume the pipeline (call it to get the iterator)
    for item in pipeline():
        print(json.dumps(item, default=str))


def build_vectordb_main() -> None:
    """Build vector database from file paths (from stdin or arguments)."""
    parser = argparse.ArgumentParser(
        description="Build vector database from file paths"
    )
    parser.add_argument(
        "paths", nargs="*", help="File paths to process (or read from stdin)"
    )
    parser.add_argument(
        "--vectordb-path", required=True, help="Path to LanceDB database"
    )
    parser.add_argument(
        "--embedding-model", required=True, help="Embedding model to use"
    )
    parser.add_argument(
        "--embedding-source", required=True, help="Source of text to embed"
    )
    parser.add_argument(
        "--overwrite", action="store_true", default=False,
        help="Overwrite existing table (default: don't overwrite)"
    )

    args = parser.parse_args()

    # Get paths from args or stdin
    if args.paths:
        paths = [{"path": p} for p in args.paths]
    else:
        # Read from stdin
        paths = [{"path": line.strip()} for line in sys.stdin if line.strip()]

    if not paths:
        parser.error("No paths provided (either as arguments or via stdin)")

    # Run the pipeline
    pipeline = build_vector_db_from_paths(
        items=paths,
        vectordb_path=args.vectordb_path,
        embedding_model=args.embedding_model,
        embedding_source=args.embedding_source,
        overwrite=args.overwrite,
    )

    # Consume the pipeline
    for item in pipeline:
        print(json.dumps(item, default=str))
