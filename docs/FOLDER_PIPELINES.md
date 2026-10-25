# Folder pipelines

The pieces behind folder mode are ordinary TalkPipe sources and segments, so
other applications can index folders, keep them indexed, follow their
changes and search them without the vault's interfaces. This guide shows
each one two ways:

- **Python** — TalkPipe's internal DSL: segment objects joined with `|`.
- **ChatterLang** — TalkPipe's external DSL: a script string, compiled with
  `talkpipe.compile(...)` or run from a shell with `chatterlang_script
  --script '...'` (installed with TalkPipe).

| Component | Kind | What it does |
|---|---|---|
| `indexFolder` | segment | Brings each folder it is given up to date, then emits its status |
| `watchFolders` | source | Keeps folders indexed in the background; emits status as it changes |
| `folderChanges` | source | Emits a folder's document files, then each one again as it is added, changed or deleted |
| `vaultSearch`, `vaultTextSearch`, `vaultChat` | segments | Search or ask across one or more folders |

All of them cover a folder's whole tree. Hidden files and folders (`.git`,
`.tpvault`, …), editor temporary files, and files no TalkPipe extractor
reads are skipped. The examples use `/path/to/notes` and `/path/to/papers`;
put your own folders there.

The vault registers these components with TalkPipe when it is installed, so
a script finds them by name. In Python, `talkpipe.compile` loads them on its
own.

## Index a folder: `indexFolder`

`indexFolder` takes folder paths and brings each folder's index up to date:
new and changed files are indexed, deleted ones removed, keywords first and
then vectors. The index is kept in `<folder>/.tpvault` (or in the app's data
directory for a read-only, network or synced folder). Running it again only
processes what changed. It emits one status dict per folder: `files_total`,
`files_keyword`, `files_embedded`, `phase`, `complete`, `vector_error`, …

Python:

```python
from talkpipe_vault.pipelines.folder_index import index_folder

index = index_folder().as_function(single_in=False)
for status in index(["/path/to/notes", "/path/to/papers"]):
    print(status["root"], status["phase"], status["files_embedded"], "embedded")
```

ChatterLang:

```sh
chatterlang_script --script '
  INPUT FROM echo[data="/path/to/notes,/path/to/papers"]
  | indexFolder
  | print'
```

Options: `vectors=False` builds only the keyword index (fast, no embedder
needed) — search such a folder with `vaultTextSearch` or
`vaultSearch[keyword_fallback=True]`, since a plain `vaultSearch` finds
nothing in it until its vectors are built; `embedding_source` /
`embedding_model` choose the embedder for a folder that has no vectors yet
(a folder already embedded keeps the model it was embedded with). When another process is already indexing a folder,
`indexFolder` leaves it alone and its status says so (`"role": "reader"`).

## Keep folders indexed: `watchFolders`

`watchFolders` runs the background indexer the vault's interfaces use: it
indexes each folder, watches it for changes, rescans it periodically, and
resumes where it left off when restarted. It can run next to a
`vault-server` on the same folders; whichever starts first does the
indexing, and the other takes over if it exits. It emits a folder's status
whenever its phase or counts change, and runs until stopped (Ctrl+C) or
until `max_events` statuses have been emitted.

Python:

```python
from talkpipe.pipe.io import Print
from talkpipe_vault.pipelines.folder_index import watch_folders

pipeline = watch_folders(paths="/path/to/notes,/path/to/papers") | Print()
for _ in pipeline():  # runs until interrupted
    pass
```

ChatterLang:

```sh
chatterlang_script --script '
  INPUT FROM watchFolders[paths="/path/to/notes,/path/to/papers"]
  | print'
```

## Follow a folder's changes: `folderChanges`

`folderChanges` emits every document file under a folder once, then each
file again when it is added, changed or deleted:

```python
{"event": "added",          # "added", "changed" or "deleted"
 "path": "/path/to/notes/2026/plan.md",
 "relpath": "2026/plan.md",  # relative to the folder, "/"-separated
 "folder": "/path/to/notes"}
```

It reads no contents; read them with TalkPipe's `readFile`. A change means
new content: a file that is only touched (saved unchanged, checked out
again) is not emitted. A rename is a `deleted` for the old path, then an
`added` for the new one.

**It remembers what it has sent.** The record is a small file per name,
`<folder>/.tpstream/<name>.db` (`name` defaults to `default`; letters,
digits, `.`, `_` and `-`), so a restarted pipeline emits only what changed
while it was stopped. Folders that cannot
hold it (read-only, network, synced) keep it in the app's data directory
instead, and `state="/some/file.db"` puts it anywhere. Each pipeline that
follows a folder on its own needs its own `name`: one name is one record,
and only one process may use it at a time.

**Nothing is lost if the pipeline stops.** A file counts as handled when the
pipeline asks for the next item, so one being handled when the process
stopped is emitted again on the next start; at worst an item comes twice,
so handle each one as "make it so" (key it by `path`). Segments that hold
items back (`flushN`, `fork`, …) give up that guarantee for what they hold.

### The text of each new or changed document

Python:

```python
from talkpipe.data.extraction import ReadFile
from talkpipe.pipe.fields import ToDict
from talkpipe.pipe.filters import FilterExpression
from talkpipe_vault.pipelines.folder_changes import folder_changes

pipeline = (
    folder_changes(path="/path/to/notes", name="documents")
    | FilterExpression("event != 'deleted'")
    | ReadFile(field="path", set_as="extracted")
    | ToDict(field_list="event,path,extracted.content:content")
)
for document in pipeline():  # runs until interrupted
    print(document["event"], document["path"], len(document["content"]))
```

ChatterLang:

```sh
chatterlang_script --script '
  INPUT FROM folderChanges[path="/path/to/notes", name="documents"]
  | isNotIn[field="event", value="deleted"]
  | readFile[field="path", set_as="extracted"]
  | toDict[field_list="event,path,extracted.content:content"]
  | print'
```

In ChatterLang, `isNotIn` / `isIn` test the event without the nested quotes
an expression would need (ChatterLang strings have no escapes); Python's
`FilterExpression` (`lambdaFilter` in a script) takes any expression.
`readFile` stores an extraction result, not a string; `toDict` lifts its
`content` (and, if wanted, `title`) into plain fields. Files an extractor
splits into parts, such as CSV or JSONL rows, come out as one item per part.

### Deletions

`readFile` cannot read a file that is gone, so the pipeline above filters
deletions out (it still records them: they are not sent again). To act on
deletions, follow the folder with a second pipeline under a name of its own:

```sh
chatterlang_script --script '
  INPUT FROM folderChanges[path="/path/to/notes", name="deletions"]
  | isIn[field="event", value="deleted"]
  | print'
```

The two pipelines are independent, so their order relative to each other is
not kept: a file created and deleted quickly may reach the deletions
pipeline before the documents pipeline has handled it.

### One pass, then stop

`watch=False` makes one pass and stops: everything not sent before, then
the end of the stream. It suits a batch job run from cron:

```python
from talkpipe_vault.pipelines.folder_changes import folder_changes

for change in folder_changes(path="/path/to/notes", watch=False)():
    print(change["event"], change["relpath"])
```

`max_events=N` stops after N items; the last one is recorded, so the next
run starts after it.

## Search folders

`vaultSearch` (semantic), `vaultTextSearch` (keyword) and `vaultChat`
(question answering) take a folder as `vault_path`, or a list of folders and
vaults to search together. Results from several folders are merged, and
each hit names its folder in `document["folder"]`. The embedder a folder was
indexed with is used for it automatically. A folder has to be indexed
first (`indexFolder`, `watchFolders`, or opening it in `vault-server`):
searching one with no index yet, or a path that does not exist, raises a
`ValueError` that says so. The check runs on every query, so a segment
built before its folder was indexed (while `watchFolders` is still
starting, say) searches the folder as soon as its index exists.

The two searches emit different shapes. `vaultSearch` emits one list per
query of `SearchResult` objects (`hit.score`, `hit.doc_id`, and
`hit.document` with `content`, `source`, `title` and `folder`);
`vaultTextSearch` emits each hit as its own dict: `{"doc_id", "score",
"document": {"content", "path", "filename", "folder"}}`, with a Whoosh
relevance score rather than a similarity. Hits from several folders are
interleaved by their rank within each folder (reciprocal rank fusion), so a
score compares only with others from the same folder and the scores need
not descend down the list.

Python:

```python
from talkpipe_vault.pipelines.searching_and_prompting import VaultSearch

search = VaultSearch(
    vault_path=["/path/to/notes", "/path/to/papers"], limit=3
).as_function(single_in=True, single_out=True)
for rank, hit in enumerate(search("deployment rollback"), start=1):
    print(rank, hit.document["folder"], hit.document["source"])
```

ChatterLang:

```sh
chatterlang_script --script '
  INPUT FROM echo[data="lighthouse storm"]
  | vaultTextSearch[vault_path=["/path/to/notes", "/path/to/papers"]]
  | print'
```

Asking a question needs a chat model; here, one served by Ollama (set
`TALKPIPE_OLLAMA_SERVER_URL` if it is not on this machine):

```python
import talkpipe

ask = talkpipe.compile(
    '| vaultChat[vault_path=["/path/to/notes", "/path/to/papers"], '
    'chat_source="ollama", chat_model="qwen3:8b"]'
).as_function(single_in=True, single_out=True)
print(ask("What is the rollback plan for the deployment?"))
```

While a folder is still being embedded, `vaultSearch[keyword_fallback=True]`
adds keyword matches for its files that have no vectors yet; `vaultChat`
does this on its own when it searches several folders.

## Index and follow together

`watchFolders` keeps the index current while another pipeline follows the
same folder with `folderChanges`; the two keep separate state and do not
interfere. For example, run each in its own process:

```sh
chatterlang_script --script 'INPUT FROM watchFolders[paths="/path/to/notes"] | print' &
chatterlang_script --script '
  INPUT FROM folderChanges[path="/path/to/notes", name="summaries"]
  | isNotIn[field="event", value="deleted"]
  | readFile[field="path", set_as="extracted"]
  | toDict[field_list="path,extracted.content:content"]
  | llmPrompt[field="content", set_as="summary", source="ollama", model="qwen3:8b",
              system_prompt="Summarize this document in one sentence."]
  | print'
```
