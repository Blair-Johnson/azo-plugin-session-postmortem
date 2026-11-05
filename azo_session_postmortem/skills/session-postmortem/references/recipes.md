# Investigation recipes

All sessions, IDs, anchors, line ranges, excerpts and outcomes below are hypothetical,
not measured results. `W` is an investigation workspace; `S` and `T` are attached
session IDs. Substitute returned IDs and observed bounds. JSON blocks are native
requests for the named tool; response excerpts are illustrative plain text, not exact
format guarantees. Each retrieval also creates a complete indexed managed buffer.
These recipes are independent routes, not a required workflow.

## 1. Search a requirement, then read its exact evidence

**Question:** Did a later clarification change what the parser had to accept?
Call `trace_search`:

```json
{"workspace":"W","scope":{"sessions":["S"]},"query":"partial captures","mode":"literal","context_lines":2,"preview_chars":2500}
```

An illustrative preview might show:

```text
S17 — 2 hits
S17-H001 | S/e2112 text lines 3–4
Accept partial captures too; the final record can be incomplete.
S17-H002 | S/e2170 text line 6
Updated the parser for partial captures.
Full indexed buffer: buf_ab12cd
```

The preview already exposes the distinction between a request and a claimed change.
Read the first hit with surrounding lines, or both if comparing them. `trace_read`:

```json
{"workspace":"W","targets":["S17-H001","S17-H002"],"pair":false,"line_context":3,"preview_chars":5000}
```

Suppose the read returns `R18`, with blocks `R18-B001` and `R18-B002` and a generated
buffer pointer. The full buffer contains all selected passages and expansion; the
preview may fit both short passages. `S17-H001` still selects the exact original
match, not all context printed around it. To check execution rather than a claim,
select the relevant run receipt and paired call next—not every message containing
“parser.” A known canonical field can be read directly without a search:

```json
{"workspace":"W","targets":[{"event":"S/e2175","field":"text"}],"pair":true,"line_context":0}
```

## 2. Skip to the useful buffer level

**Question:** Which of many matches needs a closer read?
Suppose `S24` returns `buf_cd34ef`; only a few hits fit inline. Use the generated
buffer directly. Ordinary `view`:

```json
{"buffer":"buf_cd34ef","line_ranges":"1:35"}
```

The `INDEX` gives item headings and exact buffer line ranges. Jump to a listed range
with `view`, or find a distinctive phrase with ordinary `grep`:

```json
{"buffer":"buf_cd34ef","pattern":"partial|S24-H037","context_lines":3}
```

Read returned item `S24-H037` with `trace_read`, without converting buffer line
numbers into source line numbers:

```json
{"workspace":"W","targets":["S24-H037"],"pair":false,"line_context":5}
```

Alternatively, search just selected items with `trace_search`:

```json
{"workspace":"W","scope":{"items":["S24-H037","S24-H041"]},"query":"incomplete","mode":"literal"}
```

For a large result that needs parallel review, ordinary `chunk_sources` can operate
on that same buffer:

```json
{"sources":["buf_cd34ef"],"target_chars":18000,"overlap_chars":0,"max_buffers":20,"suggest_groups":true}
```

Retain original item/source IDs in delegated findings. Chunks are copies, not new
trace evidence identities. Skip the index when an inline hit or known ID is enough;
skip inline browsing when buffer search or chunking is the useful starting point.

A mixed-length `trace_read` can request two short fields around a large field:

```json
{"workspace":"W","targets":[{"event":"S/e700","field":"text"},{"event":"S/e705","field":"text"},{"event":"T/e23","field":"text"}],"pair":false,"line_context":0,"preview_chars":3500}
```

The preview allocates space across requested targets before extra context, completing
short targets where possible and using head/tail excerpts for a long whole field.
The complete middle field remains in the indexed buffer. Reducing `preview_chars`
changes inline display, not which targets or bytes enter that buffer.

## 3. Select body scope before checking a failure or supersession

**Question:** Which selected run actually produced the error?
Call `trace_query` for exact receipt fields rather than keyword-filtered previews:

```json
{"workspace":"W","sql":"SELECT ref,seq,json_object('event',ref,'field','text') AS anchor FROM events WHERE session=:s AND seq BETWEEN :lo AND :hi AND kind='result' AND tool IN ('terminal_run','terminal_view','terminal_status') ORDER BY seq","params":{"s":"S","lo":4803,"hi":4915}}
```

Suppose this returns `Q30`. Use its whole retained selection in `trace_search`:

```json
{"workspace":"W","scope":{"result":"Q30"},"query":"(?i)(traceback|typeerror|valueerror)","mode":"regex","collapse":"exact"}
```

Suppose `S31-H001` contains a failure. Read it with `pair=true` to inspect what ran.
Edit arguments containing an error name are not execution; asynchronous completions
and worker reports are separate scopes. Collapsed equal passages retain their
occurrences; inspect the occurrence items/links in the buffer when provenance matters.
Equal text alone does not prove independent runs.

To check whether later work superseded that failure, select a bounded later interval
of event refs with `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,seq,kind,role,system_generated,tool,preview FROM events WHERE session=:s AND seq BETWEEN :lo AND :hi ORDER BY seq","params":{"s":"S","lo":4916,"hi":5100}}
```

Suppose the result is `Q32`. Search event bodies without implicitly following their
referenced objects, using `trace_search`:

```json
{"workspace":"W","scope":{"result":"Q32"},"query":"(?i)(partial.capture|incomplete.record|parser.fix)","mode":"regex","follow_object_links":false,"save_as":"later-parser-evidence"}
```

This flag suppresses only implicit whole-event expansion. Explicit objects and
objects inherently included by workspace/session scopes remain searchable; explicit
field/line scopes remain exact. Rerun with `saved="later-parser-evidence"` and omit
the policy flag to preserve it. Follow candidate changes to run/publication receipts.
State the interval and exclusions if reporting no later evidence, not “no fix exists.”
For human requests, use `role='user' AND system_generated=0`; null origin is unknown,
not human. Do not apply that filter to a search for execution results.

## 4. Read both sides of a positional join

**Question:** Which selected candidates have a selected receipt within four later
events in the same session? Suppose searches returned `S40` and `S41`.
Call `trace_cooccur`:

```json
{"workspace":"W","seed":{"result":"S40"},"against":[{"name":"receipt","selection":{"result":"S41"},"before":0,"after":4}],"save_as":"nearby-receipts","preview_chars":5000}
```

An illustrative result makes both sides and witness links visible:

```text
C42 — 1 matched, 1 unmatched
C42-M001 | S/e4855 | receipt within window
  C42-W001 | delta +2 | "run completed: exit 0"
C42-U001 | S/e4890 | receipt outside window
  C42-W002 | nearest outside, delta +7 | "run completed: exit 1"
Full indexed buffer: buf_ef56ab
```

Read a matched seed and its exact witness using `trace_read`:

```json
{"workspace":"W","targets":["C42-M001","C42-W001"],"pair":false,"line_context":0}
```

Read the failed side separately:

```json
{"workspace":"W","targets":["C42-U001","C42-W002"],"pair":false,"line_context":0}
```

Explicit groups select all retained members, not only inline examples. For example,
`trace_read` can select the unmatched group:

```json
{"workspace":"W","targets":[{"result":"C42","group":"unmatched"}],"pair":false,"line_context":0}
```

Search the witnesses group using `trace_search`:

```json
{"workspace":"W","scope":{"result":"C42","group":"witnesses"},"query":"exit 1","mode":"literal"}
```

The other group name is `matched`; optional `rows` selects zero-based retained
positions. Seed groups contain only seed anchors, never their witnesses. All groups
and their item aliases preserve the no-implicit-object-expansion rule. Signed deltas
are witness sequence minus seed sequence; the `+7` witness is not a qualifying match.
A failed condition can retain its nearest same-session witness, or none when that
session has no witness. The full buffer contains all relations and short excerpts;
source bodies come from exact reads. Saved `nearby-receipts` reuses these fixed
selections, not fresh searches. To join just chosen items next, either input selector
can instead be `{"items":["S40-H001","S40-H003"]}`.

## 5. Recover output and follow a recorded child

**Question:** What evidence lies behind a handoff claim?
Read a known receipt with `trace_read`:

```json
{"workspace":"W","targets":[{"event":"S/e705","field":"text"}],"pair":false,"line_context":0}
```

Suppose the response points to object `S/o_buf_saved` and a recorded child association
for `T`. Follow the explicit object with `targets=[{"object":"S/o_buf_saved"}]`.
If its bytes are unavailable, use `trace_query` to locate referrers:

```json
{"workspace":"W","sql":"SELECT src AS ref,dst,kind FROM links WHERE dst=:object ORDER BY src","params":{"object":"S/o_buf_saved"}}
```

Read selected query item IDs for captured excerpts; they are not the missing full
object. For historical edits, read the edit's `arguments` and paired receipt, then
locate saves and tests rather than assuming a final buffer is the earlier state.

Read the launch anchor shown in the navigation block. Navigation items resolve launch
evidence, not the child session's contents. Query child evidence with `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,seq,kind,role,tool,preview FROM events WHERE session=:child ORDER BY seq","params":{"child":"T"}}
```

If `T` is not indexed and access is authorized, first call `trace_open`:

```json
{"workspace":"W","sessions":["T"],"descendants":false}
```

A declared output reservation proves neither completion nor correctness. For parent investigation,
query `sessions.parent_id` and incoming `session_links` separately; compare conflicting
observed receipts before attributing work. Recorded links are not authored map relations.

## 6. Compact context without losing claim-specific evidence

**Question:** What should the next investigator trust or check next?
Suppose the requirement was read, but the later success claim remains unchecked.
Call `trace_edit` using actual returned item IDs:

```json
{"workspace":"W","put":[{"id":"parser-scope","kind":"episode","title":"Partial captures required; execution still unchecked","anchors":["S17-H001","S17-H002"],"fields":{"checked":"Clarification explicitly includes incomplete final records.","inherited":"Later message claims the parser was updated; run receipt not checked.","next_read":"Inspect S/e2175 text with its paired call.","unvisited_routes":["Child T's test run"]}},{"id":"resume-parser","kind":"note","title":"Continue parser investigation","note":"Workspace W. Read parser-scope, then S/e2175 text and its paired call. Search/read buffers are copies of selected evidence, not proof of a passing run.","pinned":true}]}
```

The episode's aliases resolve to canonical anchors at edit time; `fields` are authored
annotations, not tool verification. If contrary evidence appears, revise the claim and
retain both supporting and contrary anchors. Keep a report in an ordinary file if
needed, with `details_path` pointing to it rather than duplicating it in the note.

After context compaction, call `trace_open`:

```json
{"workspace":"W"}
```

Its preview prioritizes the pinned note; the full buffer includes map cards and prior
result paths. Jump directly to the card with `trace_read`:

```json
{"workspace":"W","targets":[{"entry":"parser-scope","part":"card"}]}
```

Then read its evidence with `part="anchors"`, use a surviving exact item ID, or read
`S/e2175` directly. There is no need to rerun every search or reread every buffer.
Carry the workspace database when copying the investigation so item aliases remain
resolvable. A buffer export or chunk alone preserves text, not the alias registry;
keep exact source IDs with quotations. Reopening a card confirms persistence, not
independent verification of its claims.
