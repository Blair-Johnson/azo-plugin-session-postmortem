# azo-plugin-session-postmortem

Native Agent Zoo tools, a Python library and a CLI for exploring session contents and building an investigator-authored map that survives context changes. The plugin does not summarize sessions, identify episodes automatically, judge success or compare agents for you. It makes the underlying material convenient to find, read, select and annotate.

`trace_open` attaches sources or resumes a workspace. `trace_query` selects, joins and samples structured rows. `trace_search` searches source passages or authored map text. `trace_read` retrieves bounded slices, neighbors and call/result pairs. `trace_cooccur` joins retained event selections within explicit same-session sequence windows, with separate matched, unmatched and witness results. `trace_edit` maintains your episodes, notes, labels and relations. All three interfaces use the same backend and workspace format.

## Native indexed buffers

Native retrieval returns useful inline previews and a generated pointer to a complete,
indexed managed buffer. `trace_edit` returns an acknowledgment. The buffer's `INDEX`
lists item IDs and exact buffer line ranges; use ordinary `view`, `grep` or
`chunk_sources`, or select a returned item ID directly in another trace call.
Hypothetical `S17-H001` identifies a search hit, not a buffer line. Item IDs retain
exact source selections in the workspace; copied text does not become new evidence.

`preview_chars` limits only inline presentation, never selection or full buffer
contents. Native calls have no paging, output-mode or read-inspection controls.
Search keeps `follow_object_links` (default true) and its existing source-scope rules.
Python `dispatch` still returns dictionaries and the CLI JSON, with legacy pagination,
cursors and read layouts `inline`, `compact` and `buffer`. See `docs/api.md` for all
six native signatures and the separate structured API contract.

## Install into Agent Zoo

From a working Agent Zoo installation, preview the source-plugin installation, then install when ready:

```sh
azo-plugin install /path/to/azo-plugin-session-postmortem --dry-run
azo-plugin install /path/to/azo-plugin-session-postmortem
```

The manifest places the plugin in `common`, making the same tools available to interactive agents and RLMs. It introduces no runtime package dependencies. Run `/reload` in the TUI after installation, then check `list_tools` and `tool_help("trace_open")`. Installing the repository and reloading are separate operations. No backend process, session or plugin directory is modified just by importing the Python package.

## Bundled investigation skill

The plugin bundles the `session-postmortem` skill at
`azo_session_postmortem/skills/session-postmortem/SKILL.md`. After installing the
updated plugin and running `/reload`, discover it with
`list_skills(query="session-postmortem")` and activate it with
`skill(name="session-postmortem")`. It is available to interactive agents and RLMs
through the host skill catalog; it is not a separate trace capability or endpoint.

Use it for semantic exploration, long saved-trace investigations, comparisons,
open-ended discovery and decision reconstruction—not as a prerequisite for ordinary
code tasks. The skill and its `references/recipes.md` contain the agent workflow:
question-specific selection, indexed-buffer navigation, exact reads, evidence provenance,
durable hypotheses and bounded co-occurrence. It also covers bounded searches
with `follow_object_links=false` and following evidence-backed child-session navigation
without treating output reservations as completed work. This guidance lives in the
bundled skill, not an injected harness tutorial. `docs/api.md` remains the tool contract reference.

## Try the CLI without installing a plugin

The synthetic example contains a changed parser requirement and a generated completion message. It includes no private session data. From this repository:

```sh
pixi run cli trace_open --workspace .work/demo \
  --json '{"sessions":["examples/session.json"],"descendants":false}' --pretty
pixi run cli trace_search --workspace .work/demo \
  --json '{"query":"partial","mode":"literal"}' --pretty
```

Every invocation is a fresh process. Read a hit using the returned result ID and row, for example `{"targets":[{"result":"r1","rows":[0]}]}` with `trace_read`. Persist your interpretation using `trace_edit`, and use a pinned `note` to record the next unresolved question. Reopen with `trace_open --json '{}'` and the same workspace. A workspace directory contains the index, fixed result selections, saved queries and your editable map; it is not tied to the current agent's context window.

Use `--args-file request.json` to avoid shell quoting. Optional `--record calls.jsonl` saves requests and responses for an investigation; those records can contain private source text. With Python available, `python -m azo_session_postmortem` works directly from the checkout. An optional ordinary package installation exposes `azo-postmortem` as a console command; it does not register another native plugin.

## Python use

```python
from azo_session_postmortem import dispatch

workspace = ".work/demo"
result = dispatch("trace_query", {
    "workspace": workspace,
    "sql": "SELECT ref, seq, preview FROM events "
           "WHERE role='user' AND system_generated=0 ORDER BY seq",
})
print(result["rows"])
```

This intentionally small API returns ordinary dictionaries. Use Python for parsing recurring metric formats, combining authored labels, statistics or plots; use existing buffers and files for reports. No separate analysis framework or model runtime is required. Complete argument shapes, return fields, SQL tables and navigation rules are in `docs/api.md`.

## Defaults that matter

Whole-event reads and searches default to readable content rather than provider replay
or bookkeeping. Explicit field anchors and `projection="all"` remain available.
A user-role message can be a generated completion; nullable `system_generated`
distinguishes recorded origin. Source search and authored map text remain separate,
and map edits never modify saved sessions.

Native co-occurrence buffers include matched, unmatched and witness items. Explicit
`group` selectors choose `matched`, `unmatched` or `witnesses`; seed selections do not
silently include witnesses. Saved recipes reuse fixed input results, not fresh searches.
The Python/CLI interface retains its matched and unmatched pagination. Both use the
same bounded same-session sequence-window semantics and safety ceilings.

## Validation and scope

Run portable checks with `pixi run test`. Native integration tests clearly skip when the host libraries are absent. In an existing Agent Zoo Python environment, run `python -m pytest tests -q` from this repo and `python scripts/validate_host.py --agent-zoo /path/to/agent-zoo`. The latter builds both real pipelines using a temporary plugin root, leaving the live install untouched. `docs/validation.md` records current checks and reproduction commands; comparative investigation efficacy still requires fresh trials.

The earlier design was exercised on three targeted semantic investigations plus open-ended discovery, including continuation by a fresh investigator using saved map cards. That experience motivated content projections, generated-message filtering, compact packets and explicit map-kind help. It is not evidence of universal retrieval quality or causal A/B effectiveness.

Literal/regex/term search is local; embeddings are not implemented. Terms use simple per-line frequency, not BM25. Searches and plans materialize in memory, and expensive regexes have no timeout. Legacy opaque buffer stores are not decoded, binary attachments are not OCRed, and historical buffer contents are not reconstructed at every event. Available saved excerpts remain searchable, but some referenced full content can be unresolved. Managed session IDs require Agent Zoo; pinned JSON documents and the synthetic example do not. Workspaces, packet files and optional logs can contain private text—keep real investigations outside this plugin checkout and out of version control. A local source-plugin installation can copy ignored files too; `.gitignore` alone is not an installation exclusion.
