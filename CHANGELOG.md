# Changelog

## In Development

- **Agent searches judge their own results.** The agent used to be shown each search's hits and asked to keep or ignore them one by one, which cost it a turn per decision and let it skip deciding altogether: with a model that makes one tool call per turn, a question plain Ask answered could end with eight searches, nothing kept and every hit "undecided" — including a question whose key word the agent had read as something else (poetry as the Python package manager). Each agent search's hits now go through the new `judgeSearchResults` judge before the agent sees them: one batched model call, inside the agent loop, decides on every hit against the question you asked, what the agent said the search was looking for, and what is already known from the chunks kept so far, so a partial answer is kept and a duplicate or a same-word match is not. The agent's tools now take `looking_for`, its prompt asks it to name what the answer still lacks, and `read_document` parts are judged the same way. Deciding costs no turns and cannot be skipped, so an agent that runs out of iterations still has what its searches kept, and "the notes don't appear to answer this question" is only said when every hit was judged irrelevant (the "found passages but stopped before keeping any" answer is gone).
- **The agent is told its turn budget.** It never knew which iteration was its last, so it could run out while still searching. The opening prompt now says how many turns it has, every tool reply ends with how many are left, and the last one is announced ("the next one is your last, so call enough_information now rather than another tool"). The count is read from the agent before each tool call, so it is exact however many tool calls a turn holds. The trace shows the countdown too. While nothing is kept the last-turn line says to try once more or say nothing relevant was found instead, since `enough_information` is refused with nothing kept, and a run that searched, had every hit judged irrelevant and stopped is reported as "found nothing relevant to keep" rather than as a weak-tool-calling "stopped without saying it had enough".
- **For pipelines: `judgeSearchResults`.** A general segment: given an item holding a result list and a question (and optionally `looking_for`, `known` notes and `seen` ids), it sets one `{"result", "keep", "reason"}` per result in rank order, or with `drop_ignored=True` only the kept results — a relevance filter after any search, from Python or ChatterLang. A model failure keeps every result with a `not judged` reason rather than stopping the pipeline. The Developer Guide shows it after `vaultSearch`, and as a stateless agent tool through talkpipe-agents' `scriptTool`.
- **Agent retrieval runs on talkpipe-agents' findings loop.** The judging, the per-question state (finding ids, deduplication, what is already known), the stop and the turn countdown, and the `enough_information` / keep / ignore tools now come from talkpipe-agents (its `FindingsSession`, `FindingsJudge` and `JudgeFindings` hook) instead of being the vault's own copy; the vault keeps its searches, reading a document, the keyword extractor, the retrieval prompt and the answer. The Search and Ask pages and the terminal interface work as before, with the same endings and notes. What the agent reads differs slightly: ids are `f1`, `f2`, …, results are "findings", a document part read whole is labelled with the characters it covers, and a tool that cannot run (no full-text index, an unknown id, an unreadable document) returns an error result. A repeated query is no longer refused outright: its hits, already decided, are reported as nothing new without another judge call. talkpipe-agents must be 0.2.0 or later.
- **For pipelines:** `vaultAgentSearch` and `vaultAgentChat` take the same parameters, and `judgeSearchResults` keeps its name, fields and output; its prompt is now talkpipe-agents' generic judge prompt (findings labelled `f1`, `f2`, …). The vault's finding tools mix with talkpipe-agents' `@finding_tool` functions and `findingTool` scripts by name, so a judged search over another source can be added to the vault's agent without Python; the agent retrieval guide shows it.
- **For code built on the agent pack (changed since 1.1.0):** `AgentSearchSession` and the `guard` helper are gone; a run's state is talkpipe-agents' `FindingsSession` (`AgentVaultSearch.make_session(question)` returns one). The tool factories take a `SearchBackend` of plain callables instead of a session, and `build_tools(backend, names)` resolves names; one set of tools serves every run. `keep_chunk` and `ignore_chunk` are no longer vault tools: as names they resolve to talkpipe-agents' `keep_finding` and `ignore_finding`, and `enough_information` is talkpipe-agents' too. The `read_document` tool takes `finding_id` in place of `chunk_id`. `AgentVaultSearch`'s overridable bindings are `vector_search(query)`, `keyword_search(query)`, `keyword_index_exists()`, `extract_keywords(question)` and `read_document(source)`, plus `findings_judge`. `judge_results.search_findings` turns search hits into findings. `docs/AGENT_RETRIEVAL.md` describes all of it.

## 1.1.0 (2026-10-27)

The full release of folder mode: everything in 1.1.0a1 and 1.1.0a2 below, plus the changes here. The install commands no longer need a pre-release flag, and the `latest` container tag now has folder mode.

- A server URL typed with spaces into the Settings page or the terminal interface's Settings tab (`not a url`) is refused at the field, as other unusable URLs already were, instead of being saved as `http://not a url` and reported later by the configuration probe as an unreachable server.
- **`python -m talkpipe_vault.apps.query` takes the `vault-server` command line.** The module form, documented as taking the same options as the console script, kept an older parser: a folder of documents given to it opened as an empty named vault instead of in folder mode, `--dir`, `--as-vault` and `--resume` were refused, the default port was 8000 and the startup banner was not flushed. It now runs the `vault-server` entry point, so the two behave the same.
- **A path fence naming a missing folder is reported before the banner.** `vault-server` with `TALKPIPE_VAULT_ROOT` or `TALKPIPE_DOCUMENT_ROOTS` pointing at a folder that does not exist used to print the whole startup banner ("Indexed in the background; ask questions right away", the web address) and then refuse to start; the refusal now comes first, on stderr, with exit code 1.
- The Settings page's message after an embedding-model change, shown when no folder is open, now says what each kind of index needs: a folder keeps searching with its recorded model until **Re-embed**, a named vault needs re-indexing with Overwrite. It used to mention only named vaults.
- **A `--dir` folder that does not exist is reported before the banner.** `vault-server --dir` with a mistyped folder printed the whole startup banner and "Starting web interface..." before warning that the folder does not exist; the warning now comes first, on stderr, and the folder is left out of the ones the app opens, so a run whose only folders were mistyped starts with none rather than announcing a vault.
- **Docs:** the Advanced Guide's customized-image recipe can be copied as printed: its `FROM` line no longer carries a trailing comment (Dockerfiles have no trailing comments, so `podman build` refused the file), and the extra-packages step reinstalls pip for the install, since the base image ships without it. Its Compose section says the services build the image from the checkout rather than pulling the published one, how to pass the version, and how to run the published image instead.
- **Docs:** the Developer Guide shows how to run a single-file component under `chatterlang_script` with `--load-module` before it is packaged; the agent retrieval guide names `apps/pipelines.py` as where the interfaces build the agent pipelines; the README quickstart mentions `--no-browser` for headless machines.
- The retrieval-filter starter recipe that drops low-scoring results, in both interfaces, now says the default model2vec embedder scores all but the closest match or two near 0, so the filter would drop most results. The message shown after opening a named vault names the Vaults & Documents page as where to add a folder back. vault-tui's F1 help lists the Retrieval filter button with the folder controls, where it sits.
- **Docs:** the README's quickstart marks `vault-server ~/notes` as any folder of documents and says a path that does not exist becomes a new, empty named vault; the Developer Guide's hybrid-search example prints ranks instead of reciprocal-rank scores that rounded to 0.0, says the `Sources:` list names every retrieved chunk, shows `result_filter_script` from ChatterLang (single quotes inside the script), and gives a complete segment skeleton with the `talkpipe` imports and decorator stack under "Writing a component of your own".
- **Docs:** the Developer Guide gains a diagram of the `pipelines/` modules and the hierarchy of calls between them, with notes on which module to import for an answer, for hits, for result merging and filtering, or for indexing.
- **The web application's module is split by function.** `apps/query.py` held the routes and every helper behind them (4,200 lines); the helpers now live in `apps/state.py` (the state singleton, effective settings, the folder indexer), `apps/pipelines.py` (building the pipelines), `apps/folders.py` (folder mode), `apps/vaults.py` (named vaults and the background index jobs), `apps/results.py` (results and answers as the pages show them) and `apps/vault_store.py` (reading a vault's stores), with the code moved unchanged. `query.py` keeps the routes, page rendering and `run_app`, and re-exports the public helpers (`add_folder`, `folders_status`, `start_index_job`, …), so code written against it keeps working; the terminal interface imports the modules directly. No behaviour changes.
- **For pipelines: the search module is split by function.** `searching_and_prompting` grew to every search component at once; its contents now live in `search_results` (the result shape, `mergeSearchResults`, reciprocal rank fusion, `filterSearchResults`), `keyword_search` (`searchLance`, `extractSearchKeywords`, keyword-query helpers), `vault_paths` (folders to indexes, searchable checks, the segments' shared base), `vault_search` (`vaultSearch`, `vaultTextSearch`) and `vault_chat` (`vaultChat`), so an application can import the piece it needs. `talkpipe_vault.pipelines.searching_and_prompting` still exports every name, so existing imports keep working; ChatterLang is unaffected.
- **A folder index built on another operating system keeps its citations.** Sources are stored as absolute paths from where the folder was indexed; a `.tpvault` carried from Windows to Linux or macOS (or back) left every result and open-file link pointing at a path of the other system. The rebase that already follows a moved folder now reads the stored paths in the convention they were written with, so the index is searchable and its files open without a rebuild.

## 1.1.0a2 (2026-10-25)

### Folders: point at folders and ask
- **Search folders directly, with no vault to create.** Add a folder on the Vaults & Documents page or the terminal interface's Vault tab, or start `vault-server ~/notes` / `vault-tui ~/notes`, and it is searched at once. Each folder, with everything under it, is indexed in the background into a hidden `.tpvault` folder inside it, keywords first and then meaning, and kept up to date as files are added, changed, renamed or deleted. Hidden paths, editor temporary files and formats no extractor reads are skipped. Several folders can be open together; every result and citation names its folder.
- **Searching while indexing.** Every page shows how far indexing has got. Keyword Search works as soon as the first files are indexed; Ask answers from keyword matches until the first documents are embedded, and says so when nothing is searchable yet; Semantic Search turns on by itself once a folder has vectors. Files not yet embedded are found by keyword, and when a folder's embedder is unreachable only that folder falls back to keywords. Ask's "Answered by" line names a partial index.
- **Indexing survives quitting and sharing.** Stopping part-way loses at most one batch, and the next run resumes where it was. A second vault-server or vault-tui on the same folder shares the index and takes over the indexing if the first exits; across processes only one batch runs at a time, which bounds memory. A file whose size or modification time changed is hashed, so a touched file is not re-embedded.
- **Where the index lives.** In the folder, or in the app's data directory when the folder is read-only, on a network share, synced by a cloud client, when `TALKPIPE_VAULT_ROOT` is set (the container), or when Settings says so. It carries a `.gitignore` and a `CACHEDIR.TAG`, so git and most backup tools skip it. **Close** stops searching and indexing a folder while it stays in the list with its index kept, and **Open** brings it back; both interfaces have the buttons, and a closed folder stays closed across restarts. Removing a folder, open or closed, keeps its index unless you tick **also delete its index**; documents are never touched. Each folder keeps the embedder it was indexed with (the Settings choice applies to new folders); a folder offers **Re-embed** when Settings names another embedder and **Rebuild** when the chunk settings differ.
- **`vault-index FOLDER ...` indexes folders from the command line** and exits: the same index `vault-server` builds in the background (`.tpvault`, keywords then vectors, only what changed on a rerun), so a cron job or a build step can index ahead of the interfaces. It takes the Settings page's embedder, chunk settings and index location as defaults, `--embedding-source` / `--embedding-model` for a new folder, `--keywords-only`, `--json` for the status dicts, and exits non-zero when a file failed, the embedder was unreachable or another process was indexing the folder. It runs the `indexFolder` segment as a ChatterLang script.
- **Command line:** a folder of documents given to `vault-server` or `vault-tui` opens in folder mode (`--as-vault` keeps the old behavior), `--dir` adds folders (not together with a vault path), and started without a path they reopen the last session's folders, as `--resume` does. A path that does not exist still creates a named vault there, and the startup banner says it is new and empty.
- **Named vaults are unchanged**, and existing vaults need no re-indexing. The one command-line change: `vault-server <vault> --dir` is refused, since `--dir` now adds folders. Named vaults stay under **Named vaults (advanced)** on the Vaults & Documents page, which now leads with folders. A named vault whose documents all lie in one folder offers to search that folder instead. Two vault processes can no longer index one named vault at the same time.
- **For pipelines:** the `indexFolder` segment and the `watchFolders` source build and maintain folder indexes outside the interfaces. `vaultSearch`, `vaultTextSearch`, `vaultChat` and the agent segments take a list of vaults or folders as `vault_path`; a documents folder stands for its index, and a folder indexed after the segment was built is searched once its index exists. `vaultSearch` gains `keyword_fallback`, and `mergeSearchResults` a reciprocal-rank `strategy="rrf"`. A path that does not exist, or a folder of documents with no index yet, raises a `ValueError` that says so instead of creating an empty index and finding nothing. `vaultChat` builds the same retrieval, prompt and completion pipeline whatever its options; the plain single-vault case no longer keeps conversation memory between questions (Ask is single-turn).
- **Follow a folder as a stream:** the `folderChanges` source emits every document file under a folder, then each one again as it is added, changed (by content, not modification time) or deleted, for a pipeline to read with talkpipe's `readFile`. It remembers per `name` what it has sent, in `<folder>/.tpstream/`, so a restarted pipeline gets only what changed meanwhile, and a file being handled when the pipeline stopped is sent again. The new [Developer Guide](docs/DEVELOPER_GUIDE.md) shows every component in Python and ChatterLang. The file rules, change tracking and folder comparison that the indexer and the stream share live in `talkpipe_vault.pipelines.folder_files`, for other components to build on.
- **A second `vault-server` hands its folders to the running one.** `vault-server ~/papers` while a vault-server is already up used to open the browser on the running instance and drop the folder without a word; it now adds the folder there (and prints what the Vaults & Documents page would have said), and says when a named vault given this way was not opened. The startup banner is flushed at once, so it shows in a log or service output, and says to visit the printed address if no browser opens.
- **Opening a named vault says that the folders were closed.** The page's message now adds that the folders you had open are no longer searched and how to go back to them; the README's Named vaults section says so too.
- **The retrieval filter can be edited for an open folder.** It already applied to the one open folder, but the Vaults & Documents page only showed the editor for a named vault, inside the Named vaults panel. The panel now sits in the Folders box when one folder is open (and stays in the vault panel for a named vault), names the folder or vault it belongs to, and is withheld while several folders are open, when a save is refused with the reason. The terminal interface's **Retrieval filter** button moved next to the folder controls and says the same.
- **vault-tui's Search tab notices when embedding is done.** Its "Semantic search starts once the first documents are embedded" notice was set at startup and stayed until the next full refresh, so it contradicted the header once the folder was ready; the folder poll now clears it.
- **Docs:** the Advanced Guide's compose note describes `--resume` in folder terms, its project tree lists the folder-mode modules, and its agent-search section explains "undecided"; Folder pipelines' keyword example uses a query with hits and says a no-hit query prints nothing, and its last example says `llmPrompt` takes any provider.
- `watchFolders` settles each folder's role before its first status instead of first reporting it as nobody's; `watchFolders` and `folderChanges` end their stream cleanly on Ctrl+C instead of printing a traceback. A folder status names its writer as "this process" rather than "this window", and the add-folder message no longer nests its parentheses when Settings keep indexes in app data.
- **Docs:** the home page's example queries no longer assume notes about this project; Folder pipelines states the merge rule for several folders as the code applies it (same embedder → by distance; otherwise reciprocal rank), that pipelines read provider URLs and keys from the environment rather than the Settings page, and that `chat_source` takes any provider; the Advanced Guide drops a stale PEP 668 cross-reference, the old "Add Documents" page name, and an unneeded `load_plugins()` call.
- **The Advanced Guide is split in two.** [Advanced Usage](docs/ADVANCED.md) keeps what operators need (command lines, terminal interface, containers, model configuration, agent search, retrieval filters, storage layout). The new [Developer Guide](docs/DEVELOPER_GUIDE.md) documents every source and segment the vault registers, with a Python and a ChatterLang example each, recipes for pipelines of your own, the Python helpers, and how to contribute; it absorbs the folder pipelines guide.
- **A shorter README.** It now orients and points: install, folders, the two interfaces at a glance, providers. The terminal interface's key-by-key notes, the podman machine notes for macOS and Windows, and the agent-search behaviour and model notes moved to the Advanced Guide (Terminal interface notes, Containers, Agent search).
- Requires talkpipe 1.2.0 or later, whose `build_rag_database` can embed chunks that are already made (`chunks=`).
- **The `dev` and `security` extras no longer include `safety`.** talkpipe 1.2.0 requires filelock 4.2.1 or later (a security fix), and every safety release caps filelock below 4.0, so the two cannot be installed together. Run safety in an environment of its own, e.g. `pip freeze --exclude-editable | (cd "$(mktemp -d)" && uvx safety check --stdin)`; CI already does. The `authlib<1.9` cap, which only existed for safety, is gone too.

### Agent-Selected Search Results and Answers
- **An agent that runs out of iterations before deciding is no longer answered as if the notes lacked the answer.** When the agent kept nothing but left chunks it had found undecided, Ask used to say the notes don't appear to answer the question, though the agent never judged what it found. It now says the agent found passages but stopped before keeping any, and that the chat model is the likely cause. The results note ends with how many chunks were left undecided, and the outcome carries the count as `undecided`.
- `minimax-m3.1-flash:cloud` on Ollama is named in the README and the agent retrieval guide as a model that usually spends its iterations searching and reading without deciding. Its reasoning is not carried from one turn to the next by the Ollama provider talkpipe-agents uses, so it keeps losing its plan. A larger iteration cap, a smaller search limit and a firmer prompt did not fix it; replaying the reasoning did.

## 1.1.0a1 (2026-10-23)

### Search
- **Ask now retrieves 8 chunks by default instead of 5.** With the default chunking, 5 chunks often included the right document but not the passage the answer needed, so answers left out key facts or said the notes did not cover them. On a 100-question benchmark of a personal notes wiki (default embedder and template), the share of fully correct answers rose from 63% to 70% and answers that missed entirely fell from 7 to 1; 13 questions were answered better and 2 worse. The cost is a longer prompt: the median answer took about 18 s instead of 13 s. The Settings page's **Ask & Keyword Result Count** still overrides it, and keyword search and the keyword boost use the same count. Existing vaults need no re-indexing.
- **Queries are now embedded as typed, without the `task: search result | query:` prefix.** That prefix is EmbeddingGemma's, left over from when it was the default embedder, and it makes the default model2vec embedder retrieve worse. On a 100-question benchmark of a personal notes wiki, the default index (default embedder and chunking, 5 results) found a right source file for 93% of questions instead of 87%, with recall up from 0.73 to 0.81 and MRR from 0.67 to 0.72; another embedder tested improved slightly too. EmbeddingGemma models keep the prefix, and a configured `retrieval_template` is used unchanged. Existing vaults need no re-indexing: the template only shapes queries.

### Agent-Selected Search Results and Answers
- **Extra Effort**: Ask and Semantic Search, in the web and terminal interfaces, have a new box next to the agent option (greyed out until it is ticked) that lets the agent take twice its usual iterations for that question — 16 with the default Agent Search Iterations of 8. On a 120-question benchmark of a personal notes wiki, doubling the cap lifted the strongest tool-calling model tested from 0.93 to 0.96 overall and from 0.75 to 0.83 on questions that need one fact to find the next; for the two other models it changed little, and plain retrieval still answered better than the agent with either. The "Answered by" line and the results note say when it was used. `AgentVaultSearch.with_max_iterations` returns the copy of a segment with the raised cap, sharing its vault and model bindings.
- **The retrieval agent can read a whole document.** A new `read_document` tool loads the document behind a chunk the agent has already seen — when the chunk is cut off, is one step of a procedure, or comes from the document plainly about the question — and hands it back as a chunk of its own to keep or ignore; long documents come in 6000-character parts. The agent names a chunk, never a path, so only documents the vault returned can be read, through the same extractors indexing used. It is part of the default tool pack and the retrieval prompt says when to use it. Small models rarely call it unprompted: the tool is there for the hunch, not yet a habit.
- `talkpipe-agents` now comes from PyPI (0.1.1 or later); the package-registry index, its source entry and the CI index variables are gone. 0.1.1 retries a model call whose tool call the server could not parse (gpt-oss on Ollama sometimes stops mid-object), so such a reply no longer ends an agent run with an error.
- Added `docs/AGENT_RETRIEVAL.md`, a developer's guide to the agent retrieval code: using the tools over another search backend or behind another agent, choosing tools and prompt, adding a tool to the pack, reading the four ways a run can end, and testing the loop without a model.
- **The agent pipeline is now assembled from talkpipe and talkpipe-agents pieces rather than hand-built.** The retrieval segments configure talkpipe-agents' `llmAgent` — model resolution, tool dispatch, progress reporting and reply cleanup are the library's — instead of assembling a Strands agent themselves.
- **Tools are chosen by name.** `vaultAgentSearch` and `vaultAgentChat` take `tools="vector_search,keep_chunk,…"` (a string or a list) and a replaceable `system_prompt`, so a pipeline adds or drops a tool without touching either module. Names the vault does not own fall through to talkpipe-agents' registry, so generic tools (`think`, `calculator`, …) mix in by name alone; an unknown name is refused when the pipeline is built, which for a ChatterLang script means at compile time.
- **What the tools do now lives apart from the pipeline that runs them**, in `pipelines/agent_tools.py`: the retrieval tools as session-bound factories in a name registry shaped like talkpipe-agents' own, knowing nothing about agents, models, or prompts. That is the piece that would move out if these tools were ever pulled into a generic package; `pipelines/agent_search.py` keeps only the vault concerns.
- The agent is talkpipe-agents' `llmAgent` used as is: the vault no longer subclasses it to capture the stop reason, reading `last_result.stop_reason` instead (talkpipe-agents 0.0.1a2 or later).
- The rule that ends a run is enforced inside the tools rather than by an agent hook, so it holds behind any agent and there is no callback of ours to keep in step with the library's.
- **The agent stops when it judges it could answer the question well from the chunks it has kept, not when it has collected a set number of results.** It has a new `enough_information` tool and is told to ask itself, after every search, what the question still lacks — a fact, a date, the other side of a comparison — and to search for that specific gap before stopping. A run may therefore end after one chunk or after eight, whichever answering the question actually took.
- **Agent Result Count** is accordingly now **Agent Chunk Limit**: a ceiling on how much context one run may collect, not a target to fill, and its default rose from 5 to 10 so it bounds a run rather than dictating it. **Agent Search Iterations** is unchanged. Neither is a goal; both exist only to stop a run that never reaches the agent's own judgement.
- The results note now tells the four endings apart — the agent judged it had enough (and says why, in its own words), it filled the chunk limit, it hit the iteration cap, or it stopped without ever saying it had enough. The last is what a model with weak tool-calling does, and it now looks different from a run that finished properly.
- `enough_information` is refused while nothing has been kept, so a run cannot declare success with no context to answer from.
- **Ask** gained a **Use Agent** checkbox, in the web interface and in the terminal interface alike. With it ticked, the agent gathers the context the same way it does on the Semantic Search page — searching, reading, keeping and discarding chunks — and the answer is built from just the chunks it kept, which are also the citations shown beneath it. The agent's tool calls are listed under the answer as they are under search results. It supersedes the keyword boost, since the agent already has keyword search as one of its tools, and an agent that could not be set up is reported in the "Answered by" line rather than quietly falling back.
- The terminal interface gained the **Use Agent** option on its Search tab too, and the Agent chunk limit and Agent search iterations settings on its Settings tab, so it matches what the web interface offers.
- Added the `vaultAgentChat` segment: the same selection loop followed by the RAG prompt and completion `vaultChat` uses, so an agent-gathered answer is worded and cited exactly like a plain one. When the agent keeps nothing, no prompt is sent and the answer says so instead of a model inventing one from an empty context.
- An active per-vault retrieval filter is never applied to an agent run — the agent searches through its own tools, which the filter never sees — and Search and Ask now say so when both are switched on, instead of ignoring the filter silently.
- The Semantic Search page gained a **Let an agent select the results** option. Instead of showing the top vector hits, an agent (built on the new `talkpipe-agents` dependency) searches the vault iteratively: it runs semantic and keyword searches as tools, reads the chunks that come back, keeps the relevant ones and ignores the rest, and rewrites its queries from what it learns — new terms, names, and phrasings from useful chunks — until it can answer the question. It can also ask the chat model to distill a question into keyword-search terms rather than composing a keyword query itself.
- Two new settings bound it, on Search and on Ask: **Agent Chunk Limit** (the most chunks one run may keep) and **Agent Search Iterations** (cap on agent turns per search or question).
- Every tool call and decision is printed to the server console as it happens and listed under the results in a collapsible "Agent tool calls" panel, and the results note says how the run ended and how many chunks were kept and ignored. The option is hidden when the chat model cannot be set up as an agent model; plain search is unaffected.
- Added the `vaultAgentSearch` segment (`pipelines/agent_search.py`), which exposes the same loop to TalkPipe pipelines and yields the kept chunks with the trace.
- When the chat model cannot be run as an agent, the Search and Ask pages now say so where the agent option would be, with the reason and a pointer to Settings, instead of dropping the option without a word.
- Agent-selected results are no longer described as "ranked by vector similarity, the closest 10 chunks"; the page says they are the agent's choice, in keep order. An agent run that kept nothing points at the agent's note and tool calls (and at the chat model) rather than suggesting a broader query.
- A run that "stopped without saying it had enough" now says in its note that this is how models weak at tool calling end a run, and the nothing-to-answer message suggests a stronger tool-calling model. The Settings page says the agent runs on the chat model, which must support tool calling, and the README names the models that ran the agent cleanly in testing (the default chat model often does not).
- The agent's keep and ignore lines in the tool-call trace name the document behind each chunk id.
- `docs/AGENT_RETRIEVAL.md`: the first examples pass the chat model (a script does not see the Settings page's choice) and show how to run the ChatterLang version and print only the answer; the extension example reports through the trace, extends the default prompt, and shows `chatterlang_script --load-module` for loading a custom tool; the snippets that left out imports or test data are now complete. The Advanced Guide's segment list includes `vaultAgentSearch` and `vaultAgentChat`.
- An agent run that fails before keeping anything — the model is not pulled, or its server refuses tool calls — is no longer answered on Ask with "the agent did not find any chunk in this vault": the answer now says the agent stopped with an error and points at the chat model, and the note says "it kept nothing" instead of "showing what it kept". The outcome carries the exception text as `error` (empty for every other ending), so an application can tell a failed run from an empty selection without parsing the note.
- When the agent keeps nothing, Ask now leads with what that most often means: "The notes in this vault don't appear to answer this question". The old message said only that the agent found no chunk, which read as a fault of the agent rather than as an answer to a question the notes do not cover; the suggestions for checking it is not the agent follow as before.
- `keep_chunk` and `ignore_chunk` calls with an id no search has returned are now listed as refusals in the agent's tool-call trace. Models that keep chunks before reading them (guessing ids, or keeping in the same step as the search that returns them) used to leave a trace full of keep calls and nothing kept, with no explanation; the refusal also tells the model that ids come only from results it has already received.
- Chunk ids are assigned under a lock. The agent runs the tool calls of one step concurrently, so two searches in the same step could hand out the same id and lose a chunk.
- `vaultAgentSearch` / `vaultAgentChat` refuse a vault path that does not exist when they are built (compile time for ChatterLang), instead of creating an empty folder and running a whole agent search over it.
- The Semantic Search page's loading message says the agent is working when the agent option is ticked, as the Ask page already did.
- **The agent no longer rejects useful chunks wholesale.** It was told to "be strict" and that keeping fewer chunks beat keeping a wrong one, and it saw only the first 500 characters of chunks that run to about 900 at the default indexing settings — so a chunk whose useful part came late was ignored, and some runs ignored every result their queries found. The agent now sees up to 1000 characters (a whole default-sized chunk), and is told to keep any chunk that is on the question's topic and helps answer even part of it, to keep it when unsure (the answer is written from the kept chunks alone), and to judge a cut-off chunk by what it is about.
- The README no longer suggests the vault can tell whether a chat model handles tool calls before it is tried: the agent option is replaced by an explanation only when the provider cannot run an agent at all; a model or server without tool calling shows up as an error note on the first run.
- `docs/AGENT_RETRIEVAL.md`: documents the `progress` parameter (now also described in `help(AgentVaultSearch)`) for keeping the trace off a program's stdout, and that ChatterLang output always includes it; the other-backend example asks a real question and uses the same model as the rest of the guide; the subclassing section says what `vault_path` means with your own backend and shows how to get answers from it too; corrects the claim that a session is used from one thread only.

### CI
- The `dev` extra moves ruff to the 0.17 series (`>=0.17.0,<0.18`), with
  the ruff-pre-commit hook on v0.17.0 to match. The code needed no changes
  for the new release's rules or formatting.

## 1.0.2 (2026-10-13)

### Security
- **HTML in an indexed document no longer runs in the browser.** Search and
  Keyword Search highlighted the query terms in each result by writing the
  snippet's text back as HTML, so markup inside a document became live in
  the results page — an `<img onerror=…>` in a text file ran its script
  when the file came up in a search. Highlighting now builds the snippet
  from text, and markup in a document shows as the text it is.
- **Ask answers no longer render raw HTML.** An answer is model output and
  can repeat text from the vault's documents; its Markdown is still
  rendered, but HTML in it is shown as text and `javascript:` (and similar)
  links are dropped instead of becoming clickable.
- Require talkpipe 1.1.0 or later, which brings pymongo 4.18.2 and pypdf
  6.19.0 or later (crafted-PDF runtime and memory exhaustion advisories).
- Add security floors for transitive dependencies: urllib3 2.8.0 (via
  requests and other HTTP clients; proxy TLS and chunked-response
  advisories) and, in the `dev` extra, virtualenv 21.7.13 (via pre-commit;
  activation script and seed wheel advisories).

### CI
- All CodeQL steps use the same `github/codeql-action` release again
  (4.38.3). Two dependency updates had left the `init` step one release
  behind the others, and CodeQL refuses to run with mismatched versions.
  Dependabot now updates the CodeQL steps together, in one PR.
- The `dev` extra pins ruff to the 0.16 series again (`<0.17`), in step with
  the ruff-pre-commit hook; a dependency update had widened it to `<0.18`.
- Dependabot version-update PRs target `master` instead of the default
  branch `stable`, which only ever points at a release. Security alerts
  still come from `stable`, so they reflect what is released.
- The security scanners run from a virtual environment of their own, and
  the dependency scan is given the packages installed in the project's
  environment. An advisory against one of the scanners' own dependencies
  (`nltk`, which `safety` requires and vault does not) no longer fails the
  build with nothing in the project to fix; the scan still fails on any
  finding in what the project installs.
- Steps that only work on GitHub (CodeQL, the container push, artifact
  uploads, PyPI publishing) are skipped when the workflow runs elsewhere.

## 1.0.1 (2026-09-25)

### Terminal interface
- `vault-tui` no longer stalls on quit. Every service call (a search, an
  Ask, a settings probe) runs on a pool thread that stays alive, idle, after
  the call returns, and the exit path counted each of those idle threads as
  "still blocked" and waited its full two-second grace period for it in
  turn — so `Ctrl+Q` took two seconds per pool thread the session had ever
  started (four seconds right after startup, longer on machines with more
  cores or after a session of searches and asks) and looked like a hang
  that needed `Ctrl+C`. The pool is now shut down as the interface closes,
  which releases idle threads at once; only a thread actually inside a
  service call is waited for, the grace period is shared across such
  threads rather than paid per thread, and `Ctrl+C` during it skips
  straight to leaving.
- An unticked checkbox no longer looks ticked. Textual draws the "X" inside
  an unchecked box in a dark colour on a dark box; it stayed legible, so
  **Overwrite existing index**, **Boost with keyword search** and **Clear the
  saved key** all read as already on — and on a terminal without 24-bit
  colour the two states were identical. The mark is now genuinely absent when
  off and bold when on.

### Added

- **Packaged icons.** The package now ships `icon-256.png` and `icon.ico`
  (cropped from the logo) under `apps/static/`, for desktop launchers.
- **Second launch opens the running instance.** Starting `vault-server` —
  or clicking the launcher — while it is already running opens the browser
  at the running server instead of failing on the busy port, using the new
  `GET /api/health` route, which reports the application name and version
  and works before any vault is opened.
- **Free-port fallback.** When port 8002 is held by some other program and
  no `--port` was given, the server uses the next free port in the
  8003–8022 range and announces it. An explicit port that is taken now
  fails before the banner with a clear message instead of a uvicorn
  traceback afterwards.

### Documentation

- **Provider-neutral documentation.** The README, Advanced Guide,
  `.env.example`, and compose comments no longer read as if Ollama were
  required. A new "LLM providers" section in the README is the single place
  that lists what vault can use — model2vec, Ollama or OpenAI for
  embeddings; Ollama, OpenAI or Anthropic for chat, plus any provider a
  TalkPipe plugin registers — with how to select one and where its API key
  or server URL goes; the quickstart, container, terminal-interface, and
  requirements sections link to it instead of repeating Ollama-only advice.
  The guide also spells out the two ways to configure a provider in a
  container: on the Settings page, which persists in the data volume, or
  from the environment — naming the variable under a compose service's
  `environment:` key (or an `env_file`), since compose passes a service
  only the variables it names.
- **Keyword Search's one-off index is now in the quickstart.** The README
  presented all three ways of exploring a vault as working the moment
  indexing finished, but Keyword Search starts disabled until its separate
  full-text index is built from the page's own button — a step the README
  mentioned only in passing, inside the terminal-interface section. The
  quickstart and the web-interface list now say so, including that indexing
  more documents later does not refresh that index.
- **`vault-tui` is no longer described as unreleased.** It has shipped on
  PyPI since 1.0.0, so the terminal-interface section no longer sends readers
  through a `git+https://…` install to get it.
- **The "install from source" instruction now names the branch.** The
  repository's default branch is the release-only `stable`, so the
  `git+https://…` URL offered for getting ahead of the PyPI release installed
  exactly the release. It is now spelled `…talkpipe-vault.git@master`, with
  the reason, and the Advanced Guide's development setup checks out `master`
  after cloning.
- **The vault-name suggestion is documented correctly.** The Advanced Guide
  said an unfenced install suggests a vault "next to" the documents folder; it
  suggests `~/<folder-name>-vault`.
- The Advanced Guide's OpenAI configuration examples no longer name `gpt-4`.

### Fixed

- `talkpipe_vault.__version__` reported a hard-coded `0.1.0`; it now
  reports the installed distribution's version.
- **The container image reports the release version.** The image derived
  its version from a copy of `.git` next to only part of the source tree,
  so setuptools_scm saw every other tracked file as deleted and reported an
  unreleased version (an image built at `v1.0.1` said `1.0.2.dev0+…`), and
  CI's shallow checkout had no tags to derive it from at all. The version
  now comes from an `APP_VERSION` build argument: CI computes it with
  setuptools_scm, compose passes `${APP_VERSION:-0.1.0}`, and a plain
  `podman build` without it reports `0.1.0`. `.git` is no longer copied
  into the build.
- The Settings page's configuration status described eliza as a "rule-based
  responder", which read like a working chat provider. It now says the
  replies do not use your documents and that a model provider is needed for
  real answers.
- **A bare `host:port` typed into the web Settings page is completed to
  `http://host:port`,** as it already was in the terminal interface. The web
  page stored the value as typed, reported "Connection settings saved", and
  then failed every request with an unsupported-scheme error. A URL that can
  never work is now refused at the field instead of being saved, and both
  interfaces share one normalization step.
- **The first screen no longer looks like an error.** Opening the app with no
  vault redirected to the Vaults & Documents page with "Choose the documents
  to index to get started." in the red alert style, announced as an alert —
  a welcome message dressed as a failure. It is now a neutral status message.
- **The Semantic Search result count no longer comes from outside the
  application.** That page's pipeline left its result limit unset and
  inherited whatever default the vector store shipped. It now passes the
  application's own limit, as the filtered variant already did. Ask's citation
  list retrieves enough rows to show the configured Ask result count when
  that is set above ten.
- **The "Ask result count" setting said it controlled Semantic Search.** It
  does not — that page always shows its top ten matches. The field is now
  labelled "Ask & keyword result count" in both interfaces and its help text
  says what it actually governs.
- **Container environment variables that nothing read are gone.**
  `VAULT_PATH`, documented in `.env.example` and set by the Containerfile and
  both compose services, was presented as the way to choose which vault the
  container serves; no code read it, so editing it did nothing and said
  nothing. `VAULT_HOST` was likewise inert for the published image, whose CMD
  fixes the host and port. Both are removed, and the files now explain that
  `--resume` plus the web interface decide which vault is open.
- **The compose documents mount works under Docker Compose.** Its default was
  `~/Documents`, and no compose provider expands `~` in a volume mapping —
  Docker Compose treated it as a relative path while podman-compose happened
  to expand it, so the same file behaved differently on the two providers the
  header claims to support. `VAULT_DOCUMENTS_DIR` is now required and must be
  absolute, and the services fail immediately with a message saying so
  instead of coming up with nothing to index.
- **The compose healthcheck can fail.** It ran `import talkpipe_vault`, which
  succeeds whether or not the web application is listening, so an unhealthy
  container was never restarted. It now requests `GET /api/health`.
- **Better hints when a search finds nothing, and when Ask has no provider.**
  The Keyword Search page's empty-result suggestions now mention that very
  common words are not indexed and point at the rebuild button, rather than
  suggesting a shorter query for a one-word search. The Semantic Search
  results note explains that the closest ten chunks are always returned, so
  weak matches at the end of the list are expected. A provider error on the
  Ask page now leads with the Settings page rather than offering it as a tip
  after two environment-variable suggestions.

## 1.0.0 (2026-08-29)

First stable release. TalkPipe Vault turns folders of documents into a
searchable vault — semantic search, exact keyword search, and RAG question
answering ("Ask") over your own files — with a web interface and a terminal
interface over the same engine. It runs fully locally by default: embeddings
are computed in-process with no server or API key, and chat answers use
whichever provider you configure (Ollama by default; OpenAI, Anthropic, and
a keyless smoke-test responder are supported).

The 0.0.x releases were previews, so this section describes the application
as it stands at 1.0.0 rather than itemizing every change since them; the
full development history is in the git log. Changes that require action when
upgrading from a 0.0.x install are listed at the end.

### What ships in 1.0.0

- **Web interface** (`vault-server`): a Vaults & Documents page that creates
  or opens a vault and indexes a documents folder into it in one submit,
  with a server-side folder picker, live indexing progress, and a remembered
  recent-vaults list; Semantic Search and Keyword Search pages with
  full-chunk and source-document viewers and copy buttons; and an Ask page
  whose answers cite their sources and carry an "Answered by" line stating
  exactly how the answer was produced.
- **Terminal interface** (`vault-tui`): the web interface's functionality as
  a Textual application for tmux, SSH sessions, and machines without a
  browser. It runs in-process — no `vault-server` needed — against the same
  state, pipelines, settings, and credentials as the web app, and fits an
  80×24 terminal. New dependency: `textual`.
- **Settings from the UI** (both interfaces): embedding and chat
  provider/model choices, chunking and retrieval sizes, and connection
  credentials (Ollama server URL, OpenAI/Anthropic API keys, an
  OpenAI-compatible base URL for vLLM/LM Studio/llama.cpp), with a live
  configuration-status panel that probes each provider and suggests fixes.
  Choices persist under `~/.talkpipe-vault` (`TALKPIPE_VAULT_HOME`).
- **Keyword-boosted Ask**: when the vault has a full-text index, Ask can
  distill the question into a keyword query and merge those hits with
  vector retrieval; the answer's meta line reports whether the boost
  applied and how many keyword hits it contributed.
- **Per-vault retrieval filters** (advanced): a ChatterLang script stored
  with the vault (`retrieval_filter.tps`) filters or transforms retrieved
  results before they reach Ask or the search pages. Whether it runs is a
  per-machine, per-vault choice — a vault received from someone else never
  executes its bundled script just by being opened — and an optional Strict
  mode makes a failing filter fail closed for filters that remove sensitive
  content.
- **Per-vault embedding restore**: indexing records the embedding
  source/model to `vault_metadata.json`, and opening a vault restores that
  embedder over the current settings — so a vault is always searched with
  the model it was indexed with, even after the default or the settings
  change.
- **Path fences for shared deployments**: `TALKPIPE_VAULT_ROOT` confines
  vault create/open/delete to one directory and `TALKPIPE_DOCUMENT_ROOTS`
  confines the folder picker and indexing; every request-driven filesystem
  access is resolved (symlinks followed) and prefix-checked server-side.
  Unset means unrestricted — the desktop default. HTTP error responses
  return fixed messages instead of echoing exception text.
- **TalkPipe components**: the pipeline pieces are registered entry points
  usable in your own TalkPipe scripts — `vaultSearch`, `vaultChat`,
  `vaultTextSearch`, `searchLance`, `extractSearchKeywords`,
  `mergeSearchResults`, `filterSearchResults`, `buildVectorDBFromPaths`,
  and the `fileWatcher` source (plus the experimental `watchIntoVectorDB` /
  `listIntoVectorDB` watcher sources, which write a separate layout the
  interfaces do not read).
- **Vault format**: a vault is a single folder holding a LanceDB `docs`
  table (compatible with TalkPipe's `makevectordatabase --path`), an
  on-demand Whoosh full-text index (`fulltext_vault/`), and
  `vault_metadata.json`; it can be copied or moved as a unit.

### Upgrading from a 0.0.x release

- The default embedding model is now model2vec
  (`minishlab/potion-retrieval-32M`), computed in-process. Vaults indexed
  under the old `ollama`/`embeddinggemma` default predate
  `vault_metadata.json`, so their embedder cannot be restored
  automatically: re-index them (Overwrite) or switch the embedding settings
  back before searching. The Settings page's configuration status flags
  such legacy vaults.
- The legacy `vault_path/vector_vault` layout is rejected when opened, with
  migration guidance: LanceDB now lives directly at the vault path.
- `OLLAMA_BASE_URL` was documented in earlier releases but never read. The
  Ollama server URL comes from `TALKPIPE_OLLAMA_SERVER_URL` (or
  `OLLAMA_SERVER_URL` in `~/.talkpipe.toml`), or from the Settings page.
- Requires Python 3.11.4+ and `talkpipe[all]>=1.0.0b2`, the first TalkPipe
  release that ships typed decorators and a `py.typed` marker.
