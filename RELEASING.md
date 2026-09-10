# Releasing TalkPipe Vault

There is no version string to bump. The package version is derived from the
git tag by `setuptools_scm`, so a release is a tag plus a published release
that triggers the publish workflow.

This plan assumes the TalkPipe release the vault depends on is already out
and working — its own release checklist covers the library. What it does
**not** assume is that the vault still works on top of it: the automated
suite exercises the plumbing, but the application must be tried by hand
before the tag goes on. Do not skip the manual section.

## Tag conventions

Tags are PEP 440 versions with a `v` prefix, on `master`:

| Kind              | Tag          | Example      |
|-------------------|--------------|--------------|
| Final             | `vX.Y.Z`     | `v1.0.0`     |
| Beta              | `vX.Y.ZbN`   | `v1.0.0b4`   |
| Release candidate | `vX.Y.ZrcN`  | `v1.0.0rc1`  |

Do not use other spellings (`v1.0.0-beta.1`, `v1.0.0.b1`) — they only make
version sorting harder. The vault follows [semantic
versioning](https://semver.org/): PATCH for fixes only, MINOR for new
features and options that keep existing behavior, MAJOR for changes that
break an existing vault, configuration key, or command-line flag.

## Steps

1. **Check the tree.** `master` is up to date, CI is green on it, and the
   working tree is clean.
2. **TalkPipe floor.** The `talkpipe[all]>=…` floor in `pyproject.toml`
   names the lowest TalkPipe release this version works with. If this
   release relies on newer library API, raise the floor (and `uv lock`)
   before anything else, so the changelog and the tests describe the same
   install.
3. **Changelog.** Rename the `## In Development` section in `CHANGELOG.md`
   to `## X.Y.Z (YYYY-MM-DD)` and add a fresh empty `## In Development`
   above it. Land that, together with any floor change, through the normal
   branch → merge request flow.
4. **Test the application by hand** (below) on the merge commit, in a fresh
   environment. Fix anything found through another merge request and come
   back to this step; do not tag a build that was not tried.
5. **Tag** the merge commit on `master` and push the tag:

   ```bash
   git checkout master && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

   Then advance `stable`, the repository's default branch, to the release.
   `stable` only ever points at a full release, so skip this for betas and
   release candidates:

   ```bash
   git push origin vX.Y.Z^{commit}:stable
   ```

6. **Publish a release** for the tag on GitHub (*Releases → Draft a new
   release*). The CI workflow's
   `release: published` trigger runs `publish-package`, which builds the
   sdist and wheel, `twine check`s them, and uploads to PyPI with the
   `PYPI_API_TOKEN` repository secret; the container job (GitHub only)
   pushes `ghcr.io/sandialabs/talkpipe-vault:<version>` plus `latest`
   (final) or `experimental` (pre-release). Mark betas and release
   candidates as pre-releases. Watch the run finish.
7. **Verify** with a fresh environment: `pip install talkpipe-vault==X.Y.Z`,
   `python -c "from importlib.metadata import version; print(version('talkpipe-vault'))"`,
   then `vault-server --no-browser` starts and the home page loads on
   http://127.0.0.1:8002. Pull the published container image and check it
   starts the same way.

Nothing is bumped afterwards; the next commit on `master` reports itself as
`X.Y.(Z+1).devN` automatically.

## Manual application test

Run this from a **wheel installed into a new virtual environment** (`python
-m build`, then `pip install dist/*.whl` in a fresh venv), not the editable
checkout — packaging mistakes (missing templates, static files, entry
points) only show up that way. Use a scratch `TALKPIPE_VAULT_HOME` and a
small folder of real documents (a few PDFs, some text or Markdown, one image
if the install supports it). Point `TALKPIPE_OLLAMA_SERVER_URL` at a working
Ollama so the Ask page can be judged against a real model; **eliza output
proves only that the plumbing runs**, never answer quality.

Web (`vault-server --no-browser`, http://127.0.0.1:8002):

- Vaults & Documents: create a new vault from a folder via the folder
  browser; indexing shows progress and finishes; the vault appears under
  recent vaults and reopens with one click; adding a second folder to the
  open vault indexes only the new files.
- Settings: Configuration status reports the embedding and chat models as
  usable; changing the chat source/model and the Ollama URL persists across
  a server restart; a deliberately wrong URL is reported, not swallowed.
- Semantic Search returns relevant results; Keyword Search handles a phrase
  and a boolean query; each result's **Open** link serves the original
  document.
- Ask: answers cite sources that open; with a full-text index present the
  keyword-boost checkbox appears and the "Answered by" line reflects it.
- Restart the server with `--resume`: the last vault reopens and search
  still works.

Terminal (`vault-tui`):

- `vault-tui <scratch-vault>` opens the vault; Search, Keywords, and Ask
  tabs return the same results as the web UI; the Settings tab saves a
  model change; `vault-tui --resume` reopens the last vault; the
  interface is usable in a small terminal (80×24) and in tmux.

Command line and container:

- `makevectordatabase` indexes the same folder from the shell and the
  resulting vault opens in `vault-server`.
- `podman build -t talkpipe-vault .` succeeds and `podman run --rm -p
  8002:8002 -v <docs>:/documents:ro -v <home>:/vault talkpipe-vault` serves
  the home page on http://127.0.0.1:8002 (see the README for the full
  invocation); indexing `/documents` from inside the container works and
  the result survives a container restart.
- Upgrade check: install the previous release, create a vault, then upgrade
  to the wheel under test — the existing vault still opens and searches.
  Anything that requires re-indexing must be called out in the changelog.

## Downstream

Nothing in the suite pins the vault. When a release changes the vault's
storage layout or command-line interface, say so prominently in the
changelog and in the README's upgrade notes.
