"""Terminal user interface for TalkPipe Vault.

``vault-tui`` (or ``python -m talkpipe_vault.tui``) offers the web
interface's functionality — vaults and indexing, semantic and keyword
search, Ask with citations, settings and diagnostics — inside a terminal,
for an SSH session, a tmux window, or any machine without a browser.

It runs in-process: the same pipelines, state, and settings the web app uses
(``apps/query.py``) are driven directly through :class:`service.VaultService`,
so no server needs to be running and nothing in the web application changes.
"""
