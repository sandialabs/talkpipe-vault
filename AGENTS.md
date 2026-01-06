## Dev environment tips
- When running code or pytest, use the .venv environment in the root of the project.
- If the .venv environment doesn't exist and you need to run code or tests, abort and ask the user to create the environment.

## Project Overview
TalkPipe Vault (this repo) is a Python toolkit built on talkpipe that provides easy pipelines for 
building vector databases and search engines, along with a demonstration application for unifying them. 
The underlying pipelines include segments and sources for extracting text from files, monitoring 
directories for changing files, doing RAG searches, and creating both full text and vector database
indexing in a unified pipeline.  The library is intended to be used stand-along with the provided 
application, but constituent parts can also be used in other applications.


## Project Structure
- `src/talkpipe_vault/` - Main library source code
  - `apps/` - Application code (query interface, vault server)
  - `pipelines/` - Pipeline implementations (building vector DBs, searching, watching)
  - `segments.py` - Text extraction segments (Tika, Docling)
  - `watchdog.py` - File monitoring source
  - `tika.py`, `docling.py` - Text extraction modules
  - `initialize_plugin.py` - TalkPipe plugin initialization
- `tests/talkpipe_vault/` - Unit tests mirroring source structure
- `docs/` - Documentation files
- Root directory contains configuration files (pyproject.toml, docker files, etc.)

## Development Guidelines 
- When a markdown or source code file contains a line with "AGENT_TODO", replace that line with the requested code.
- Do not automatically agree with what the user suggests.  Question assumptions and point out weaknesses in logic or inconsistences with the rest of the codebase.
- pytest unit tests are under tests/
- the code for the library is under src/
- After implementing a new feature, update CHANGELOG.md. If the development section of the changelog already
has a relevant section, update that section rather than creating a new one. 
- When making a change, write a unit test that fails without the change or update an existing test, verify that the unit test fails, make the change and then verity that the unit test now passes.  Notify the user if it is not reasonable to write the unit test. 
- When writing unit tests, update and augment existing unit tests is possible and appropriate rather than writing new unit tests.
- Unit tests should be as short as possible while still testing the functionality as completely as possible.
- User interfaces should be visually appealing and maintain consistency with each other.
- When writing or updating pipeline examples in documentation, write examples with stand-alone code and 
    test the examples by running them.
- When writing documentation for segments or sources, do not document the parameters.
- When writing new sources or segments, use Annotated in the parameter lists to explain the type and purpose of each parameter.
- When writing a report or script that is not intended to be part of the repository, prefix the filename with "NOCOMMIT_".


## Important Commands
python -m build for creating a new release
pytest --cov=src for unit test coverage
