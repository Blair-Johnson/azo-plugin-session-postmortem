# Tool and Python API

All operations require a `workspace` directory. CLI arguments are JSON objects; Python uses `dispatch("trace_read", {...})`. Native relative paths follow the active harness workspace. CLI/Python relative paths follow their process cwd. Native `state` is injected by the harness and never a model argument.

## Native indexed-buffer interface

The five retrieval tools return useful inline text plus a generated managed-buffer
pointer. The buffer contains the complete selected result, including material omitted
from the preview, and is saved under the workspace's `results` directory. `trace_edit`
returns an acknowledgment without a retrieval buffer. Native retrieval requires the
host BufferManager.

```python
trace_open(workspace, sessions=None, descendants=True, refresh=False,
           preview_chars=6000)
trace_query(workspace, sql=None, params=None, sample=None, saved=None,
            save_as=None, preview_chars=6000)
trace_search(workspace, query=None, scope=None, mode="terms", context_lines=2,
             on="source", collapse="none", projection="content",
             follow_object_links=True, saved=None, save_as=None, preview_chars=6000)
trace_read(workspace, targets, before=0, after=0, pair=True, line_context=10,
           projection="content", preview_chars=12000)
trace_cooccur(workspace, seed=None, against=None, saved=None, save_as=None,
              preview_chars=6000)
trace_edit(workspace, put=None, delete=None, link=None, unlink=None)
```

These are effective defaults. Nullable native defaults let saved recipes retain their
settings; omit selection options when using `saved`. `preview_chars` is a nonnegative
integer, not a boolean. It budgets inline presentation, not selection or buffer
contents; zero suppresses inline evidence, not materialization. Native schemas have
no `cursor`, `page_size`, `max_chars`, `output`, `inspect` or `top_k`.

### Previews and complete buffers

Search previews show up to six match-centered passages, then compact index entries as
space permits. Search buffer blocks contain matching source lines plus `context_lines`;
item IDs still select exact matches. Query buffers retain complete chosen cell values,
including SQL columns named `row`. Read previews allocate space to distinct requested
targets before contextual expansion: short targets can be complete, long whole fields
use head/tail excerpts, and anchored excerpts begin at the match. Open prioritizes
pinned notes and includes catalog, map, recipe and prior-result pointers. Co-occurrence
shows counts, matched and unmatched examples, and condition links with witness IDs,
signed deltas or nearest-outside labels, and literal excerpts.

Every full buffer has a title/workspace, `## INDEX`, entries with exact buffer line
ranges, and `## ITEM_ID` detail headings. Blocks and relations are complete for that
operation: search is not a whole-event export, and co-occurrence contains relations
and short excerpts rather than full event bodies. A read contains all requested
passages and expansion, without the structured API's per-page block cap. Source text
is preserved; generated headings and buffer line numbers are navigation, not anchors.
Use the generated pointer with `view`, `grep` or `chunk_sources`; a known item ID can
also go straight to `trace_read` without first traversing its buffer.

For SQL joins over `result_items`, use the `SQL membership: rN` pointer in a search,
query or co-occurrence buffer header as `result_items.result_id`. Native IDs such as
`S17` remain tool selectors, not SQL table values. This pointer is omitted from inline
previews; the complete buffer provides it when the structural join is needed.
### Item IDs and source selectors

IDs are workspace-scoped immutable aliases. Hypothetical forms include `S17-H001`
(search hit), `Q18-R001` (query row), `R19-B001` (read block), `C20-M001`, `C20-U001`,
`C20-W001` (matched, unmatched, witness), and `O21-...` (open items). Use actual returned
IDs, not inferred numbering. A whole result alias such as `S17` retains its original
membership. Canonical source addresses and result-row selectors still work.

- Read or annotate items: `targets=["S17-H001"]` or `anchors=["S17-H001"]`.
- Scope search or a join selection: `{"items":["S17-H001","S17-H004"]}`.
- Select a join group: `{"result":"C20","group":"matched"}`, `"unmatched"` or
  `"witnesses"`; optional `rows` uses zero-based retained positions. The plural
  selector `witnesses` corresponds to individual `witness` blocks.

Matched/unmatched selections contain seed anchors only; witnesses require their own
IDs or group. Aliases preserve co-occurrence's no-implicit-object-expansion rule.
`follow_object_links=False` still suppresses only implicit expansion from ordinary
whole events: explicit objects and workspace/session objects remain. Explicit field
and line scopes remain exact. Aggregate-only rows have values to view but cannot be
selected as source evidence.

Identity is persisted in the workspace database, survives reopen or a workspace copy,
and must not silently rebind after source refresh. Missing or foreign IDs fail. Map
edits resolve aliases to real anchors immediately. Copied/chunked buffers are text
copies, not new source identities; retain the workspace and exact item/source IDs for
durable evidence. Buffer line numbers are not trace source selectors.

## Structured Python/CLI API

The remainder documents the existing structured engine, not native request shapes.
Python `dispatch` returns dictionaries; the CLI returns JSON. They retain pagination,
cursors, `max_chars`, read inspection and `inline`/`compact`/`buffer` layouts.
File-backed buffer output is not a managed host buffer. Search accepts both
`follow_object_links` and legacy `include_references`; saved recipes retain their
policy. New native aliases and delivery controls belong to the native cache adapter,
not a replacement structured dispatch contract.

No operation interprets a session. Query/search results are durable selections;
recipes are rerun explicitly. Sources and investigator-authored annotations stay separate.

## Open and resume

```python
trace_open(workspace, sessions=None, descendants=True, refresh=False,
           page_size=8, cursor=None)
```

`sessions` contains saved session IDs/prefixes or explicit source paths. Managed IDs use Agent Zoo's reader and state home. A source path can point to a standalone or pinned JSON document. `descendants` includes discoverable child sources when attaching; unavailable children are warnings, not invented content. Reopening without `sessions` restores the existing catalog and map without re-analysis. `refresh=True` requests source reindexing; it does not rewrite authored notes or result selections. Prefer pinned sources when you need repeatable exploration.

Returns `workspace`, source descriptions/warnings, `capabilities`, `counts`, pinned/recent entry previews, saved query recipes and `next_cursor`. Page the catalog with the cursor and optional `page_size`, without source-changing arguments. Previews are short; read complete cards when resuming. Capabilities advertise modes, map kinds, projections and packet behavior.

Structured refresh preserves stored map/result records but does not rebase legacy sequence-based anchors or remove previously indexed descendants. If source ordering changes, a still-valid sequence address can change meaning. Use a pinned source for a fixed investigation; reread evidence after refresh. Invalid stale anchors fail explicitly, while the authored card remains readable so it can be repaired.
## Structural selection and sampling

```python
trace_query(workspace, sql=None, params=None, saved=None, sample=None,
            page_size=20, max_chars=6000, cursor=None, save_as=None)
```

Supply a read-only SQL query and optional bound parameters, a saved recipe name, or a continuation cursor—not multiple selection methods. `save_as` names the recipe. Returns `result`, `columns`, `rows`, total `row_count`, sampling information and `next_cursor`. The result retains all selected rows; page size and response budgets limit only the displayed page. SQL mutations, private tables and file-access extensions are blocked. This is a constrained local query surface, not a general SQL execution service.

| Public table | Main columns |
|---|---|
| `sessions` | `id, parent_id, title, source, warnings` |
| `events` | `ref, session, seq, elapsed_s, kind, role, system_generated, tool, arguments, preview` |
| `objects` | `ref, session, kind, line_count, char_count, available, reason` |
| `links` | `src, dst, kind` for recorded call/result, reference and child links |
| `session_links` | `src, dst, kind, job_id, output_name, anchor` for derived launch/output associations |
| `map_entries` | `id, kind, title, note, anchors, tags, fields, pinned, details_path, revision` |
| `map_members` | `entry_id, ref` for distinct containers touched by anchors |
| `map_links` | `id, src, dst, label, note` for authored relations |
| `result_items` | `result_id, row, ref, anchor, entry_id` |
| `saved_queries` | `name, tool, arguments, last_result` |

JSON columns can be inspected with SQLite JSON functions. Full source text is retrieved with `trace_read`; previews are mechanically clipped. `seq` is a within-session position, not elapsed time. `elapsed_s` can be null. `system_generated` is 0 for explicit false, 1 for explicit true and null when unknown; a user-role event is not necessarily a human instruction. Do not treat unknown as false.

`sample={"n":24,"by":["bin","kind"],"seed":7}` samples named query columns, allocating round-robin across strata and uniformly within each stratum. Strata that received no sample are reported. Use SQL to create bins, join labels, find unmapped material, or compute counts; sampling does not declare what is important. Map membership means the container is touched, not that its whole contents were selected or reviewed.

Query/search `max_chars` budgets the visible row JSON rather than the outer response envelope. Oversized previews carry `_display_truncated`; complete rows and source anchors remain in the result. An extremely small budget cannot remove required navigation keys, so it is not a hard cap on every response byte.
## Search source content or the map

```python
trace_search(workspace, scope=None, query=None, mode="terms", on="source",
             collapse="none", top_k=100, page_size=12, max_chars=6000,
             saved=None, cursor=None, save_as=None, projection="content",
             follow_object_links=True)
```

A scope has exactly one key: `{"workspace":true}`, `{"sessions":["A","B"]}`, `{"refs":[...]}`, `{"result":"r17"}`, or `{"entries":["episode-id"]}`. Omitted scope means workspace. Entry scopes expand source anchors, not note prose. With boolean `follow_object_links=True` (the default; legacy `include_references` is also accepted by dispatch), ordinary whole-event scopes include directly referenced objects (searchable content or unavailable notices in structured output). `False` suppresses only this implicit expansion: explicit objects remain, as do objects inherently selected by workspace/session scopes. Explicit fields and slices remain within their field and lines. Co-occurrence result selectors never implicitly follow object references under either setting, as detailed below. Session scopes do not silently expand to other sessions. New saved search recipes persist this policy; legacy recipes default to true. Omit it when paging a cursor or rerunning `saved`, rather than overriding the retained policy.

`mode` is `literal`, `regex` or `terms`. Terms use simple per-line token-frequency ranking. `similar` is unsupported and never silently falls back to lexical matches. `top_k` is reserved for the unsupported similarity path; current modes retain the complete matching selection. `on` is `source`, `map` or `both`; `both` returns separate hit lists, never a shared relevance ranking. Source search does not search your interpretation in place of raw evidence.

`projection="content"` selects text, arguments/raw arguments, structured blocks and recognized readable-reasoning fields for whole events. `all` includes saved metadata and provider replay. An explicit event-field anchor overrides projection. These are mechanical field rules, not semantic filtering. Nested content blocks can still contain non-prose structure.

Returns `result`, `hits={"source":[...],"map":[...]}`, `matched_total`, `selected_count` and `next_cursor`. Counts are separate for source/map. Hits have row IDs, matched field, excerpt, origin and retrieval mode. Source hits retain `ref` plus exact `anchor`; map hits identify `entry_id`. A returned excerpt is a preview, not the complete evidence.

Search responses also include `unavailable=[{"anchor":...,"reason":...}]` (at most eight notices) and `unavailable_count`, the full count of distinct indexed sources with no stored content in the effective source scope. These notices are separate from hits and survive zero-hit responses, small display budgets and pagination; their snapshot is retained with the selection, including collapsed occurrence results. Unknown references still fail. `on="map"` does not check source coverage and returns no source notices, with a null count. Older stored results without coverage metadata likewise return a null count (unknown), not zero. A zero count means no known unavailable sources in that scope, not that all historical content was captured or searched under the selected projection.

`scope_summary` is persisted with search and collapsed-occurrence results and replayed
unchanged on cursor pages (null for legacy results without it). It records requested
`include_references`, `projection`, aggregate `event_count`, `object_count`,
`field_count`, `unavailable_object_count`, `session_count`, and at most eight sorted
`sessions` entries with `session`, those four source counts, `seq_min` and `seq_max`.
`sessions_truncated` reports a clipped session list; aggregate counts remain complete.
Counts describe effective source windows after scope filtering, not hits. Fields count
distinct searchable `(ref,field)` pairs; object counts include known unavailable objects,
which contribute no searchable fields. `sequence_coverage="envelopes_not_contiguous"`
means min/max bounds can contain gaps; object-only sessions have null bounds. These
counts do not establish semantic completeness or historical capture completeness.
For map-only search, source numeric counts are null and `sessions=[]`. The reported
policy is not a promise to expand every scope type, notably co-occurrence results.

`collapse="exact"` groups identical source passage text for display and supplies `occurrence_count` and `occurrences_result`. Selecting the grouped row selects all occurrences; select individual rows of its occurrence result when needed. Repeated actions are not deleted. Cursor calls page the existing selection; `saved` reruns a named recipe. Old saved recipes retain their original projection.

## Co-occurrence of retained selections

```python
trace_cooccur(workspace, seed=None, against=None, page_size=10,
              max_chars=6000, cursor=None, saved=None, save_as=None)
```

Join existing search, occurrence or query results; the tool does not run text searches
or interpret relevance. `seed` is `{"result":"r1"}` or
`{"result":"r1","rows":[0,3]}`. Omitted `rows` selects all retained rows; `[]`
selects none. Indexes refer to the original result, not positions on a displayed page.
`against` contains one to three objects, each requiring all four keys:

```json
{"name":"receipt","selection":{"result":"r2"},"before":0,"after":4}
```

Names must be nonempty, unique and not `seed`. Windows and row indexes must be
nonnegative integers, not booleans. Extra keys and nested nulls are invalid, including
`rows:null`. Result selectors distinguish omitted `rows` from an explicit empty list.

Each candidate seed event must satisfy every condition in the same session. A witness
qualifies when `-before <= witness.seq - seed.seq <= after`; boundaries are inclusive.
Zero/zero means the same event, potentially different fields or lines. Windows are
relative to the seed, not pairwise diameters, elapsed time or cross-session distances.
There are no Cartesian combinations of witnesses and no graph-neighbor expansion.

Exact event fields and lines survive selection. Collapsed source rows expand to every
retained occurrence, not just the displayed representative. Identical anchors are
deduplicated; multiple seed anchors in one event form one candidate seed. Whole-event
query refs remain whole-event selected scopes, not claimed literal matches. Object
hits have no event position and are excluded; map cards and aggregate-only rows are
also non-event exclusions. Map anchors and referenced objects are not implicitly
expanded. Malformed or stale source anchors fail rather than silently disappearing.

### Results and navigation

Returns `operation="cooccur"`, `status="complete"`, matched `result`,
`unmatched_result`, `counts` (`seed_events`, `matched_seed_events`,
`unmatched_seed_events`), `inputs`, matched `rows`, matched total `row_count`,
`next_cursor`, and `unmatched_cursor` (null when no unmatched seeds exist).

Each seed row includes `row`, `ref`, `session`, `seq`, its first original `anchor`,
`seed_anchor_count`, `conditions`, and `failed_conditions`. A condition includes its
`name`, `before`, `after`, `status`, `witness_count`, `witnesses_result`, and at most
two witness previews. Status is `within_window`, `outside_window`, or
`no_same_session_match`. Positive conditions retain **all** qualifying anchors;
a failed condition retains one deterministic nearest same-session anchor, even if
outside the window, or none when that session has no witness. Witness rows contain
`row`, `ref`, exact `anchor`, signed `delta_seq`, `kind`, `role`, `tool` and nullable
`system_generated`. No source bodies are copied into the join response.

Seed ordering is session/sequence/ref. Witness ordering is absolute delta, sequence,
ref, field, line and canonical anchor. Matched and unmatched results are ordinary
query results with column metadata and `sampling:null`. Their hidden anchor membership
preserves **all seed anchors only**. Reading, searching or annotating a seed row does
not silently include its witnesses; select a condition's `witnesses_result` separately.
All witness results are ordinary query results, including the single nearest witness
for a failed condition. Result selections work with `trace_read`, scoped
`trace_search`, `trace_edit` anchors and SQL `result_items`.

Search scopes using a co-occurrence matched, unmatched or witness result—directly
through `scope.result` or through result selectors in `scope.refs`—do not implicitly
follow object references. Whole-event anchors remain stored unchanged; searches
respect `content`/`all` projection. Explicit field anchors retain their usual meaning.
This rule belongs to those retained results, not to the underlying event forever:
copying their references into a map or rematerializing them through SQL creates an
ordinary selection with the usual search behavior. Read expansion remains controlled
by the existing read arguments, and objects can still be selected explicitly.

- Page `next_cursor` with **`trace_cooccur`**; it pages matched rows only and preserves
  counts and input metadata. Supply only workspace, cursor and optional page_size/max_chars.
- Page `unmatched_cursor` with **`trace_query`**; it starts at unmatched offset zero.
- To enumerate any seed or witness result without a supplied query cursor, use
  `trace_query(sql="SELECT row,ref,anchor FROM result_items WHERE result_id=:r ORDER BY row",
  params={"r":"<result>"})`, then its query cursor. SQL exposes navigable membership,
  not additional join-metadata columns. Original retained results remain unchanged.

`page_size` and `max_chars` affect display only. Small budgets can exceed the requested
size for one irreducible row/navigation; clipped display rows carry `_display_truncated`.
All retained witnesses remain navigable even when not previewed.

`save_as` stores selectors and windows as a `cooccur` recipe, with `last_result` set
to the matched result. `saved` reruns that recipe against the same immutable named
input results, **not a fresh underlying text search**; it cannot accompany seed/against.
Cursor calls reject source selectors and saved/save_as before writes. Page limits may
change on continuation.

### Coverage and safety ceilings

`inputs` contains seed plus each named condition (at most four entries). Each reports
`name`, `result`, `selected_rows`, `event_count`, `anchor_count`, `excluded_non_event`,
`duplicate_anchors`, and `coverage={unavailable_count,unavailable,scope:"input_result"}`.
Coverage is inherited from the whole input result, not recomputed for a row subset;
the unavailable preview has at most eight notices. Query/legacy coverage is unknown
(null), not zero. Do not sum overlapping unavailable counts or claim a coverage union.
An unmatched seed establishes only failure of these selected event/window conditions.
A lexical match—even an unrelated word—is still a match, not evidence of relevance,
success, causality or independent confirmation.

V1 safety ceilings are 50,000 expanded input anchor occurrences across inputs, 5,000
seed events and 100,000 retained witness rows across all seed/condition pairs,
including nearest failed-condition witnesses. Serialized result rows and metadata
also have a cumulative 64 MiB ceiling (`MAX_RESULT_BYTES`). These are not user-tunable parameters.
Exceeding a ceiling raises a named-limit ValueError asking for narrower selectors;
the transaction rolls back all new results, cursors and recipes. Inputs are materialized
and saved JSON is loaded in full: these ceilings do not promise streaming input or
bounded JSON-parser memory.

## Read bounded content

```python
trace_read(workspace, targets=None, before=0, after=0, pair=True,
           line_context=10, max_chars=12000, output="inline", cursor=None,
           projection="content", inspect=False)
```

Targets can be whole refs (`"A/e92"`), event ranges (`{"session":"A","events":[90,108]}`), event fields (`{"event":"A/e92","field":"text","lines":[2,9]}`), object slices (`{"object":"A/o_buffer","lines":[18,37]}`), selected result rows (`{"result":"r17","rows":[0,3]}`), or map cards (`{"entry":"framing","part":"card"}`). Field-anchor lines are optional: `{"event":"A/e92","field":"text"}` reads the whole field, and `{"object":"A/o_buffer"}` reads the whole object. Out-of-range slices are rejected without clipping; the error reports the reference, field, requested bounds, available line count and a whole-field selector. For a map entry use `part="anchors"` to expand its selected source material instead of reading its interpretation. Ranges and line numbers are inclusive; result row IDs are zero-based.

Unknown event-field errors identify the offending source ref, requested field, sorted
available fields and a valid target example. All targets are validated before read
planning, inspection results or packets are written; retry with an actual field rather
than relying on a fallback. This also applies to invalid fields in retained/map anchors.

Search-result rows preserve their exact matched slices. Query rows need a navigable `anchor`, `ref` or `entry_id`; aggregate-only results cannot be read as source. A map-card result does not automatically become a source selection. Explicitly expand its entry anchors instead. The same conversion applies to scoped search and map edits.

`before`/`after` add neighboring events within the same session, `pair=True` adds the recorded call/result counterpart, and `line_context` expands line slices. Requested slices precede whole targets and added context. Overlapping windows merge. References are listed rather than recursively expanded. Read projection follows the search rules above.

Returns content `blocks`, shared source attribution in `sources`, delivered slices (normally derived from blocks in compact output), a bounded `remaining` preview and `remaining_count`, reference previews/counts, unavailable-target previews/counts, and `next_cursor`. A read page contains at most 64 blocks; previews contain at most eight entries. Longer reference lists supply `references_result` and a `references_cursor` pageable with `trace_query`. The complete pending read plan persists behind the read cursor. Continue with `workspace`, `cursor` and optional `max_chars`/`output` only. Omit `targets`, `before`, `after`, `pair`, `line_context` and `projection`; even explicit default values for those selection/expansion arguments are rejected. Omit or null `inspect` as well; explicit `inspect=false` is inert and accepted, but `inspect=true` is initial-only. Errors name the prohibited supplied arguments and allowed continuation arguments. Rejected calls leave the immutable read plan unchanged.

`max_chars` is a content-character budget. `text_chars` reports consumed content and `packet_chars` reports the compact inline response or exported text-packet size; metadata, escaping and CLI pretty printing can make the full response larger. Long lines continue using character offsets rather than being silently clipped. A large early target can still delay later targets. Delivery completeness concerns stored content, not whether an original terminal output was already truncated.

`output="buffer"` exports the bounded page. In the CLI/library, `buffer` and `buffer_path` identify the packet file containing the rendered text page. Known unavailable targets are reported separately from pending delivery. Reading has no interpretive side effect on the map.

### Recorded session navigation

`session_links` is derived from indexed source receipts, separate from ordinary
`links` and investigator-authored `map_links`. It is backfilled for existing indexes
and rebuilt after source indexing/refresh. `rlm_child` rows use the launch event as
`src`, child session ID as `dst`, and null `output_name`; `rlm_output` rows use the
observed session's output-object ref as `src` and its reserved semantic output name.
`anchor` is JSON launch evidence (`event`, `field="text"`, inclusive `lines`).

Only complete `kind='result', role='tool', tool='submit_rlm'` text receipts are parsed:
`Submitted RLM task rlm_job_<digits> with profile ... (...).`, then `Child live session:`
with a lowercase UUID or `rlm-<digits>`, optional `Resuming completed task: rlm_job_<digits>`,
optional task-name/working-directory lines, optional input-buffer section, optional
`Output buffers reserved:` entries (`- name -> buf_handle [reserved]`), and the exact
matching `Use rlm_status(...) ... cancel_rlm(...)` footer. Sections must occur in that
order. Narrative, call arguments, embedded examples, status blobs and malformed or
incomplete receipts do not derive links. Resume receipts add no inferred resume edge
or completion fact.

Ordinary `trace_read` pages expose `session_navigation` (at most eight rows) and
`session_navigation_count` for delivered sources, not pending targets. Rows contain
`src,dst,kind,job_id,output_name,anchor`, `observed_session`, `indexed`, nullable `title`
and `recorded_parent_id`, `parent_status` (`match`, `mismatch`, `unknown`), `association`
(`observed_submission` or `declared_output`), and `ambiguous`. A single recorded
`references` hop from a delivered event to an indexed output object adds `via` and
uses the delivered event as `src`; missing object bytes do not prevent navigation.
There is no recursive or automatic reverse-child walk. Query `sessions.parent_id`
and incoming `session_links` explicitly for parent investigation.

Overflow adds `session_navigation_result` containing all rows and
`session_navigation_cursor` starting after the preview; page it with **`trace_query`**.
Reading a navigation row resolves only its launch `anchor`. `dst` is a session
locator, never a source-text ref: query its events and read selected refs separately.
Children not indexed have `indexed=false`; no navigation operation loads them. Use
explicit authorized `trace_open` to attach a missing child. Inspection returns costs,
not this delivered-page navigation.

Copied receipts remain observed associations, not canonical ownership. Job identity
is scoped to `(observed_session,job_id)`; conflicts and distinct receipt anchors are
retained. `ambiguous` means a returned `src` has multiple distinct observed-session,
child and job associations. `parent_status` compares indexed parent metadata with the
receipt's observed session, not proven ancestry. Output reservations establish neither
completion nor correctness, even when object bytes exist. Author semantic relations
with `trace_edit`; recorded links do not supply those interpretations.

### Structured Python/CLI compact read layout

`output="compact"` opts into a display-only layout with `packet_format="compact_read_json_v1"`. It contains the same ordered source text and map-card bodies, anchors, source attribution (`kind`, `role`, `tool`, `system_generated`), content budget, pending previews/counts, unavailable notices, references and navigation as legacy `output="inline"`. It does not summarize or filter evidence, and does not move visible references behind extra calls.

Compact omits `delivered` when it is reconstructible from the blocks. If an old cursor's delivery metadata carries otherwise unavailable navigation (for example, an unavailable block without an anchor), `delivered` remains explicit. These redundant block fields are omitted:
- An omitted `requested` means `true`; explicit `false` remains.
- For blocks with a `text` field, omitted `char_offset` means `0`, omitted `complete` means `true`, and omitted `char_end` equals `char_offset + len(text)` in Unicode characters. Nonzero offsets, `complete=false`, and any non-derivable end remain explicit. These defaults do not apply to whole-card or unavailable blocks.
- Reconstruct `delivered` in block order: a whole `card` gives `{"entry": entry_id, "part": "card"}`; a text block gives `{"anchor": anchor}` (or `{"entry": entry_id, "part": "card"}` for `format="map_card_json"`) plus `char_offset` and `char_end`; an unavailable block gives `{"anchor": anchor, "available": false}`. Whole cards retain `entry_id` and `card`; split cards retain `entry_id`, `format` and exact JSON text slices. Unavailable blocks retain their anchor and reason and are not empty text bodies.

Inline remains the default, including continuation after a compact page. Choose `output="compact"` on each desired page; existing read cursors work unchanged and layouts can be mixed across pages. Neither cursors nor saved evidence store the display choice. Legacy inline uses `packet_format="compact_json"` (meaning minified JSON, not the compact read layout); buffer uses `packet_format="text"`. For inline and compact, `packet_chars` is the length of canonical JSON (`ensure_ascii=False`, sorted keys, separators `(',', ':')`), including its own count's digits. Transport whitespace or CLI pretty printing can add characters. `inspect=true` still requires inline output; compact inspection is rejected before any read-plan result is written.

### Inspect a read plan

`inspect=True` resolves the same targets, pairing, neighboring events, line context,
projection, ordering and overlap deduplication as an ordinary read, but returns costs
rather than source bodies. It defaults to false and is supported only on an initial
request with `output="inline"` in Python/CLI;
inspection with a read cursor or buffer output is rejected, as is structured compact
inspection. `max_chars` remains the simulated read-text budget, not the inspection
response budget. Inspection does not classify relevance, summarize evidence or
silently exclude source kinds.

Returns `plan_result`, `total_rows`, `known_text_chars`, `unavailable_rows`,
`partially_delivered_rows`, `fully_deferred_rows`, bounded `rows` and `plan_cursor`.
Each plan row contains `row`, exact `anchor`, `requested`, `kind`, `role`, `tool`,
`text_chars`, `would_deliver_chars` and `available`. Unknown unavailable content has
unknown size, not a zero-length body; it is counted separately from budget-deferred
content. Known size and simulated delivery account for expanded context, pairing
and serialized map cards as the ordinary reader does. Top-level target order need
not equal field/slice delivery order.

The initial preview contains at most 16 rows within a separate 4,000-character row
display cap. All rows are retained as an ordinary query result. When the preview is
incomplete, pass `plan_cursor` to **`trace_query`**, not `trace_read`, and use that
query's `next_cursor` for further pages. `max_chars` on the query controls its visible
row JSON, not the earlier simulated read. The plan's navigable anchors are also
available through `result_items`; cost fields are returned row metadata, not new
public SQL columns.

To read selected plan rows without re-expanding them:

```json
{"workspace":"W","targets":[{"result":"<plan_result>","rows":[0,4,9]}],"pair":false,"line_context":0,"max_chars":6000}
```

This is a normal read, not continuation of the inspection response. Inspection
retains the selection and estimate, not a guarantee that changed sources or map
cards still have the same contents or cost; inspect again after such changes.
On ordinary read-cursor continuation, omit `inspect`. Explicit `inspect=false` is
also accepted as inert; `inspect=true` is rejected.
The response's `predicted_text_chars` is the expected first-page text charge, excluding
metadata and the inspection preview itself.

## Edit the investigation map

```python
trace_edit(workspace, put=None, delete=None, link=None, unlink=None)
```

Each `put` entry has a caller-chosen `id`. Creation requires `kind` and `title`; `kind` is only `episode` or `note`. Episodes require nonempty source anchors. Notes may be unanchored and pinned for continuation. Other fields are `note` (text), `anchors`, `tags` (string list), `fields` (authored JSON object), `pinned` (boolean), and `details_path` (optional path to a separately edited report). This is data supplied by the investigator, not a request to synthesize a finding.

```json
{
  "put": [{
    "id": "input-scope", "kind": "episode", "title": "Partial captures added",
    "note": "The user changed the completeness assumption; test whether later handling reflects it.",
    "anchors": [{"result":"r1","rows":[0]}],
    "tags": ["requirements"],
    "fields": {"next_probe":"Find the later parser change and its tests."},
    "pinned": true
  }]
}
```

Use a result and row actually returned in your workspace. Result anchors are expanded to fixed slices at edit time, not stored as live queries. For existing entries, omitted fields remain unchanged; arrays replace prior values; `fields` merges by top-level key and null deletes that key. Null clears optional fields. `add_anchors` and `remove_anchors` update membership without retyping it; they cannot accompany a replacement `anchors` list. To shorten a range, remove its exact old anchor and add the new one in one call.

`link` takes `{id,from,to,label,note}` relations between authored entries, with note optional. IDs are caller-chosen; replacing an ID updates that relation. `unlink` and `delete` take ID lists. Deleting an entry rejects dangling relations unless they are removed in the same batch. The transaction returns `created`, `updated`, `deleted`, `linked`, `unlinked` and entry previews. Rejected edits leave the map unchanged.

Two episodes can overlap or contain disjoint source ranges. Splitting, merging, matching and labeling are explicit edits, not separate reasoning endpoints. An authored relation such as “same subproblem” is a hypothesis you can revise. Neither episode membership nor returned text implies that all contents were reviewed or that the claim is established.

## Errors and practical limits

The CLI returns JSON errors on stderr with exit status 2; the native adapter uses the harness's ordinary tool error path. Embedding mode is explicitly unsupported. Unknown fields/addresses and malformed selections fail rather than broadening silently. Missing referenced content is different from an invalid address. The bundled skill describes how to interpret bounded or incomplete evidence; native responses do not repeat general caveats on each call.

The index never executes saved tools or loads arbitrary pickled session objects. JSON-readable buffers and saved call/result text can be searched, but opaque legacy buffer managers, binary attachments and every historical buffer version are outside this implementation. Original sessions are not edited. Workspace indexes and optional CLI logs contain copied source text; treat them as sensitive and avoid publishing them with code.
