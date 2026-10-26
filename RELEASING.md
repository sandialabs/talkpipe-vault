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
| Alpha             | `vX.Y.ZaN`   | `v1.1.0a2`   |
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
   above it. While the current release is a pre-release, the README's
   install commands and the Advanced Guide's custom-image `FROM` line use
   the pre-release forms (`pip install --pre`, `uv tool install --prerelease
   allow`, the `experimental` image tag); a full release switches them back
   to the plain forms. Land that, together with any floor change, through
   the normal branch → merge request flow.
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
   `stable` only ever points at a full release, so skip this for alphas,
   betas and release candidates:

   ```bash
   git push origin vX.Y.Z^{commit}:stable
   ```

6. **Publish a release** for the tag on GitHub (*Releases → Draft a new
   release*). The CI workflow's
   `release: published` trigger runs `publish-package`, which builds the
   sdist and wheel, `twine check`s them, and uploads to PyPI with the
   `PYPI_API_TOKEN` repository secret; the container job (GitHub only)
   pushes `ghcr.io/sandialabs/talkpipe-vault:<version>` plus `latest`
   (final) or `experimental` (pre-release). Mark alphas, betas and release
   candidates as pre-releases. Watch the run finish.
7. **Verify** with a fresh environment: `pip install talkpipe-vault==X.Y.Z`,
   `python -c "from importlib.metadata import version; print(version('talkpipe-vault'))"`,
   then `vault-server --no-browser` starts and the home page loads on
   http://127.0.0.1:8002. Pull the published container image and check it
   starts the same way, and `curl http://127.0.0.1:8002/api/health` reports
   the release version once started. For a pre-release, also check that the
   README's install commands (`pip install --pre`, `uv tool install
   --prerelease allow`, the `experimental` image tag) pick up the new
   version, and that they still read right once a full release follows.

Nothing is bumped afterwards; the next commit on `master` reports itself as
`X.Y.(Z+1).devN` automatically.

## Manual application test

Run this from a **wheel installed into a new virtual environment** (`python
-m build`, then `pip install dist/*.whl` in a fresh venv), not the editable
checkout — packaging mistakes (missing templates, static files, entry
points) only show up that way. Use a scratch `TALKPIPE_VAULT_HOME` and a
small folder of real documents (a few PDFs, some text or Markdown, one image
if the install supports it). Configure a real chat provider so the Ask page
can be judged against a real model — a working Ollama via
`TALKPIPE_OLLAMA_SERVER_URL`, or an OpenAI or Anthropic key; **eliza output
proves only that the plumbing runs**, never answer quality.

Web (`vault-server --no-browser`, http://127.0.0.1:8002):

- Folders: `vault-server ~/notes --no-browser` opens the folder; the pages
  show indexing progress, Keyword Search works before embedding finishes,
  Ask says what it is answering from, and Semantic Search turns on once the
  folder has vectors. A `.tpvault` appears inside the folder (or under app
  data when Settings says so). Adding, changing and deleting a file is
  picked up without a restart. A second folder added on Vaults & Documents
  is searched alongside the first and every result names its folder.
  **Close** keeps the folder listed but unsearched, **Open** brings it back
  at once, **Remove** with *also delete its index* removes the `.tpvault`.
  Changing the embedder in Settings offers **Re-embed** on the folder, and a
  folder indexed with another embedder still searches alongside the rest.
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
- Restart the server with `--resume`: the last vault, or the folders you
  had open (closed ones stay closed), reopen and search still works.
  Starting `vault-server ~/papers` while the first server is running adds
  the folder to the running instance instead of starting a second one.

Terminal (`vault-tui`):

- `vault-tui <scratch-vault>` opens the vault; Search, Keywords, and Ask
  tabs return the same results as the web UI; the Settings tab saves a
  model change; `vault-tui --resume` reopens the last vault; the
  interface is usable in a small terminal (80×24) and in tmux.
- `vault-tui ~/notes` opens the folder; the Vault tab shows its indexing
  status, and **Close selected**, **Open selected** and **Remove selected**
  behave as in the web UI.

Command line and container:

- `makevectordatabase` indexes the same folder from the shell and the
  resulting vault opens in `vault-server`.
- `vault-index ~/notes` indexes a folder from the shell and exits with a
  one-line summary; a rerun finishes at once without re-embedding;
  `vault-server ~/notes` then opens the folder as already indexed.
- `podman build --build-arg APP_VERSION="$(python3 -m setuptools_scm)" -t
  talkpipe-vault .` succeeds and `podman run --rm -p
  8002:8002 -v <docs>:/documents:ro -v <home>:/vault talkpipe-vault` serves
  the home page on http://127.0.0.1:8002 (see the README for the full
  invocation) and `/api/health` reports the version; indexing `/documents`
  from inside the container works and the result survives a container
  restart. Adding `/documents` as a folder puts its index under `/app/data`
  (the image sets `TALKPIPE_VAULT_ROOT`), and the folder reopens after the
  container is recreated with the same `vault_data` volume.
- Upgrade check: install the previous release, create a vault, then upgrade
  to the wheel under test — the existing vault still opens and searches.
  Anything that requires re-indexing must be called out in the changelog.

## Downstream

Nothing in the suite pins the vault. When a release changes the vault's
storage layout or command-line interface, say so prominently in the
changelog and in the README's upgrade notes.
