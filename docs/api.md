# Tool and Python API

All operations require a `workspace` directory. CLI arguments are JSON objects; Python uses `dispatch("trace_read", {...})`; native tools expose the same named arguments. Native relative paths follow the active harness workspace. CLI/Python relative paths follow their process cwd. Native `state` is injected by the harness and never a model argument.

No operation interprets the session for you. The map stores only semantic text and relationships supplied in edit requests. Query and search results are durable selections, not live queries; saved recipes are rerun explicitly. Source content and authored annotations are kept separate.

## Open and resume

```python
trace_open(workspace, sessions=None, descendants=True, refresh=False,
           page_size=8, cursor=None)
```

`sessions` contains saved session IDs/prefixes or explicit source paths. Managed IDs use Agent Zoo's reader and state home. A source path can point to a standalone or pinned JSON document. `descendants` includes discoverable child sources when attaching; unavailable children are warnings, not invented content. Reopening without `sessions` restores the existing catalog and map without re-analysis. `refresh=True` requests source reindexing; it does not rewrite authored notes or result selections. Prefer pinned sources when you need repeatable exploration.

Returns `workspace`, source descriptions/warnings, `capabilities`, `counts`, pinned/recent entry previews, saved query recipes and `next_cursor`. Page the catalog with the cursor and optional `page_size`, without source-changing arguments. Previews are short; read complete cards when resuming. Capabilities advertise modes, map kinds, projections and packet behavior.

Refresh preserves stored map/result records but does not rebase sequence-based anchors or remove previously indexed descendants. If source ordering changes, a still-valid sequence address can change meaning. Use a pinned source for a fixed investigation; reread evidence after refresh. Invalid stale anchors fail explicitly, while the authored card remains readable so it can be repaired.
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
             saved=None, cursor=None, save_as=None, projection="content")
```

A scope has exactly one key: `{"workspace":true}`, `{"sessions":["A","B"]}`, `{"refs":[...]}`, `{"result":"r17"}`, or `{"entries":["episode-id"]}`. Omitted scope means workspace. Entry scopes expand source anchors, not note prose. Whole-event scopes include directly referenced available objects; explicit slices remain within their field and lines. Session scopes do not silently expand to other sessions.

`mode` is `literal`, `regex` or `terms`. Terms use simple per-line token-frequency ranking. `similar` is unsupported and never silently falls back to lexical matches. `top_k` is reserved for the unsupported similarity path; current modes retain the complete matching selection. `on` is `source`, `map` or `both`; `both` returns separate hit lists, never a shared relevance ranking. Source search does not search your interpretation in place of raw evidence.

`projection="content"` selects text, arguments/raw arguments, structured blocks and recognized readable-reasoning fields for whole events. `all` includes saved metadata and provider replay. An explicit event-field anchor overrides projection. These are mechanical field rules, not semantic filtering. Nested content blocks can still contain non-prose structure.

Returns `result`, `hits={"source":[...],"map":[...]}`, `matched_total`, `selected_count` and `next_cursor`. Counts are separate for source/map. Hits have row IDs, matched field, excerpt, origin and retrieval mode. Source hits retain `ref` plus exact `anchor`; map hits identify `entry_id`. A returned excerpt is a preview, not the complete evidence.

`collapse="exact"` groups identical source passage text for display and supplies `occurrence_count` and `occurrences_result`. Selecting the grouped row selects all occurrences; select individual rows of its occurrence result when needed. Repeated actions are not deleted. Cursor calls page the existing selection; `saved` reruns a named recipe. Old saved recipes retain their original projection.

## Read bounded content

```python
trace_read(workspace, targets=None, before=0, after=0, pair=True,
           line_context=10, max_chars=12000, output="inline", cursor=None,
           projection="content")
```

Targets can be whole refs (`"A/e92"`), event ranges (`{"session":"A","events":[90,108]}`), event fields (`{"event":"A/e92","field":"text","lines":[2,9]}`), object slices (`{"object":"A/o_buffer","lines":[18,37]}`), selected result rows (`{"result":"r17","rows":[0,3]}`), or map cards (`{"entry":"framing","part":"card"}`). Field-anchor lines are optional; omitting them reads the whole field. For a map entry use `part="anchors"` to expand its selected source material instead of reading its interpretation. Ranges and line numbers are inclusive; result row IDs are zero-based.

Search-result rows preserve their exact matched slices. Query rows need a navigable `anchor`, `ref` or `entry_id`; aggregate-only results cannot be read as source. A map-card result does not automatically become a source selection. Explicitly expand its entry anchors instead. The same conversion applies to scoped search and map edits.

`before`/`after` add neighboring events within the same session, `pair=True` adds the recorded call/result counterpart, and `line_context` expands line slices. Requested slices precede whole targets and added context. Overlapping windows merge. References are listed rather than recursively expanded. Read projection follows the search rules above.

Returns content `blocks`, shared source attribution in `sources`, delivered slices, a bounded `remaining` preview and `remaining_count`, reference previews/counts, unavailable-target previews/counts, and `next_cursor`. A read page contains at most 64 blocks; previews contain at most eight entries. Longer reference lists supply `references_result` and a `references_cursor` pageable with `trace_query`. The complete pending read plan persists behind the read cursor. Continue with `cursor` and optional `max_chars`/`output`, without selection or expansion arguments.

`max_chars` is a content-character budget. `text_chars` reports consumed content and `packet_chars` reports the compact inline response or exported text-packet size; metadata, escaping and CLI pretty printing can make the full response larger. Long lines continue using character offsets rather than being silently clipped. A large early target can still delay later targets. Delivery completeness concerns stored content, not whether an original terminal output was already truncated.

`output="buffer"` exports the bounded page. In the CLI/library the `buffer` and `buffer_path` values identify the packet file. In the harness, `buffer` is a real managed scratch-buffer ID and `buffer_path` identifies the exported packet file. The scratch buffer contains a copy; editing it does not rewrite the packet file. Use ordinary buffer tools to inspect or delegate it. Native export requires the host BufferManager; it never disguises a filename as a managed ID. Known unavailable targets are reported separately from pending delivery. Reading has no interpretive side effect on the map.

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

The CLI returns JSON errors on stderr with exit status 2; the native adapter uses the harness's ordinary tool error path. Embedding mode is explicitly unsupported. Unknown fields/addresses and malformed selections fail rather than broadening silently. Missing referenced content is different from an invalid address. Source warnings and unavailable reasons must be checked when interpreting negative search results.

The index never executes saved tools or loads arbitrary pickled session objects. JSON-readable buffers and saved call/result text can be searched, but opaque legacy buffer managers, binary attachments and every historical buffer version are outside this implementation. Original sessions are not edited. Workspace indexes and optional CLI logs contain copied source text; treat them as sensitive and avoid publishing them with code.
