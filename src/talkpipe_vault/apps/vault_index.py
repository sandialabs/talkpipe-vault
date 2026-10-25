"""
CLI entry point that indexes folders for folder-mode search.

``vault-index`` runs the ``indexFolder`` segment as a ChatterLang script, so
the command does exactly what the documented pipeline does: each folder's
index (``<folder>/.tpvault``, or app data) is brought up to date, keywords
first and then vectors, and ``vault-server`` or ``vault-tui`` can open it.
"""

import argparse
import contextlib
import json
import os
import sys
from typing import Any

from talkpipe.chatterlang import compiler
from talkpipe.util.config import configure_logger

from talkpipe_vault.apps import user_settings


def _quote(value: str, flag: str) -> str:
    """Quote *value* as a ChatterLang string, refusing what it cannot hold."""
    if '"' in value or "\\" in value:
        raise SystemExit(f"error: {flag} may not contain a double quote or backslash")
    return f'"{value}"'


def build_script(
    *,
    embedding_source: str | None,
    embedding_model: str | None,
    index_location: str,
    chunk_size: int | None,
    shingle_size: int | None,
    overlap: int | None,
    vectors: bool,
) -> str:
    """The ChatterLang script the command runs: ``| indexFolder[...]``.

    Only options that were given appear, so the segment's own defaults (and
    the embedder recorded in an existing index) apply otherwise.
    """
    options = [f"index_location={_quote(index_location, '--index-location')}"]
    if embedding_source:
        options.append(
            f"embedding_source={_quote(embedding_source, '--embedding-source')}"
        )
    if embedding_model:
        options.append(
            f"embedding_model={_quote(embedding_model, '--embedding-model')}"
        )
    if chunk_size is not None:
        options.append(f"chunk_size={int(chunk_size)}")
    if shingle_size is not None:
        options.append(f"shingle_size={int(shingle_size)}")
    if overlap is not None:
        options.append(f"overlap={int(overlap)}")
    if not vectors:
        options.append("vectors=False")
    return f"| indexFolder[{', '.join(options)}]"


def summarize(status: dict[str, Any]) -> str:
    """One line per folder, like the interfaces' folder list."""
    root = status["root"]
    if status["role"] != "writer":
        return f"{root}: left alone, {status['writer']} is indexing it"
    line = (
        f"{root}: {status['files_total']} files, "
        f"{status['files_keyword']} keyword-indexed, "
        f"{status['files_embedded']} embedded"
    )
    if status["files_error"]:
        line += f", {status['files_error']} failed"
    line += f" ({status['embedding']})"
    for problem in (status["error"], status["vector_error"]):
        if problem:
            line += f"\n  {problem}"
    return line


def main(argv: list[str] | None = None) -> int:
    """Index each folder given and print a summary; non-zero on any failure."""
    parser = argparse.ArgumentParser(
        prog="vault-index",
        description=(
            "Index folders of documents for TalkPipe Vault, the way vault-server "
            "does in the background. Each folder's index is kept in a .tpvault "
            "subfolder (or in app data) and brought up to date: new and changed "
            "files are indexed, deleted ones removed, keywords first and then "
            "vectors. Run it again and only what changed is processed."
        ),
        epilog=(
            "The embedder, chunk settings and index location saved on the "
            "Settings page are the defaults; a folder already indexed keeps the "
            "embedder its vectors were made with. Equivalent ChatterLang: "
            'INPUT FROM echo[data="FOLDER"] | indexFolder | print'
        ),
    )
    parser.add_argument("folders", nargs="+", metavar="FOLDER", help="Folder to index")
    parser.add_argument(
        "--embedding-source",
        help="Embedding provider for a folder with no vectors yet (e.g. model2vec, ollama)",
    )
    parser.add_argument(
        "--embedding-model", help="Embedding model for a folder with no vectors yet"
    )
    parser.add_argument(
        "--keywords-only",
        action="store_true",
        help="Build only the keyword index; no embedder is needed",
    )
    parser.add_argument(
        "--index-location",
        choices=user_settings.INDEX_LOCATIONS,
        help=(
            "Where a new index lives: 'auto' (in the folder when possible) or "
            "'home' (the app's data directory)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print each folder's status as a JSON line instead of a summary",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show indexing log messages"
    )
    args = parser.parse_args(argv)
    configure_logger("root:INFO" if args.verbose else "root:ERROR")
    if not args.verbose:
        # The first run downloads the default embedder; its progress bar is
        # noise in a log.
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    saved = user_settings.get_model_overrides()
    script = build_script(
        embedding_source=args.embedding_source or saved.get("embedding_source"),
        embedding_model=args.embedding_model or saved.get("embedding_model"),
        index_location=args.index_location or user_settings.get_index_location(),
        chunk_size=saved.get("chunk_size"),
        shingle_size=saved.get("shingle_size"),
        overlap=saved.get("shingle_overlap"),
        vectors=not args.keywords_only,
    )
    index = compiler.compile(script)

    failed = False
    out, err = sys.stdout, sys.stderr
    # talkpipe's document processor prints each file name and progress dots
    # as it goes; without --verbose only this command's own lines are shown.
    # Log messages keep their stream: the handler was bound above.
    quiet = contextlib.ExitStack()
    if not args.verbose:
        sink = quiet.enter_context(open(os.devnull, "w"))  # noqa: SIM115
        quiet.enter_context(contextlib.redirect_stdout(sink))
        quiet.enter_context(contextlib.redirect_stderr(sink))
    try:
        with quiet:
            for status in index(args.folders):
                print(
                    json.dumps(status) if args.json else summarize(status),
                    file=out,
                    flush=True,
                )
                failed = failed or bool(
                    status["role"] != "writer"
                    or status["error"]
                    or status["vector_error"]
                )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=err)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; the next run resumes where this one stopped.", file=out)
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
