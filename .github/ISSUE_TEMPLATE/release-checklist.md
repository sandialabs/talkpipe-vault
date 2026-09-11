---
name: Release Checklist
about: Steps for doing a new release (see RELEASING.md for detail)
title: Release vX.Y.Z
labels: ''
assignees: ''

---

Assumes the TalkPipe release this depends on is already out and working.
The application itself must be tested by hand — the suite is not enough.

- [ ] `master` up to date, CI green, working tree clean
- [ ] `talkpipe[all]` floor in `pyproject.toml` matches what this release needs (`uv lock` if changed)
- [ ] CHANGELOG: `## In Development` → `## X.Y.Z (YYYY-MM-DD)`, fresh empty section above; merged via MR
- [ ] Build the wheel and install it into a **new** virtual environment
- [ ] Manual test — web UI (`vault-server --no-browser`, scratch `TALKPIPE_VAULT_HOME`, real chat model — Ollama, OpenAI or Anthropic — not eliza)
    - [ ] create vault from a folder, indexing completes, reopens from recent vaults, add a second folder
    - [ ] Settings: configuration status OK, model/URL change persists across restart, bad URL reported
    - [ ] Semantic search, keyword search (phrase + boolean), **Open** links serve documents
    - [ ] Ask: answer cites sources; keyword-boost checkbox and "Answered by" line behave
    - [ ] `--resume` restarts into the same vault
- [ ] Manual test — `vault-tui`: search/keywords/ask match the web UI, Settings saves, `--resume` works, usable at 80×24 and in tmux
- [ ] Manual test — `makevectordatabase` output opens in `vault-server`
- [ ] Manual test — `podman build -t talkpipe-vault .`, run per README, index `/documents`, survives restart
- [ ] Upgrade check: vault created by the previous release still opens (note any re-index requirement in the changelog)
- [ ] Anything found: fix via MR, re-test
- [ ] Tag the merge commit on `master` (`git tag -a vX.Y.Z -m vX.Y.Z`) and push the tag
- [ ] Publish the release on GitHub (pre-release for betas/rcs); watch `publish-package` and the container build finish
- [ ] Verify: `pip install talkpipe-vault==X.Y.Z` in a fresh venv, `vault-server --no-browser` serves http://127.0.0.1:8002; published container image starts
