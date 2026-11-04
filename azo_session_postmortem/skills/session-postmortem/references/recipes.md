# Investigation recipes

These ten recipes are independent starting points. All example references, IDs,
anchors and sequence/line numbers are hypothetical. `W` stands for an existing
workspace; `S` and `T` stand for attached session IDs. Responses are abridged,
representative shapes, not new trial results; substitute IDs, anchors and bounds
actually returned by your workspace. Each JSON request names its tool in the
preceding sentence.

## Contents

1. [Outline and exact fields](#1-outline-a-range-then-read-the-important-exact-fields)
2. [Receipt-field searches](#2-select-receipt-fields-before-searching-for-a-failure)
3. [Priority comparisons](#3-put-both-comparison-receipts-before-the-long-chronology)
4. [Output recovery](#4-recover-truncated-output-without-inventing-historical-buffer-contents)
5. [Duplicate occurrences](#5-inspect-equal-text-occurrences-then-revise-the-hypothesis)
6. [Read inspection](#6-inspect-delivery-costs-then-choose-the-evidence-yourself)
7. [Supersession](#7-check-whether-a-later-change-supersedes-an-early-failure)
8. [Priority and handoff](#8-enforce-read-priority-then-hand-off-individual-claims)
9. [Bounded co-occurrence](#9-join-selected-candidates-to-nearby-receipts-then-inspect-both-sides)
10. [Interval scope and child navigation](#10-search-an-interval-without-object-expansion-then-follow-a-child-handoff)

## 1. Outline a range, then read the important exact fields

**Question:** What did the user change, and did the later completion answer it?

Call `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,seq,kind,role,system_generated,tool,preview FROM events WHERE session=:s AND seq BETWEEN :lo AND :hi ORDER BY seq","params":{"s":"S","lo":2103,"hi":2113},"page_size":16,"max_chars":4000}
```

Representative rows expose an initial request at `S/e2103`, a long `skill` result
at `S/e2106`, and a short clarification at `S/e2112`. A request can be deferred
behind the skill body in a whole-range read even with pairing disabled.

Next call `trace_read`:

```json
{"workspace":"W","targets":[{"event":"S/e2103","field":"text"},{"event":"S/e2112","field":"text"}],"pair":false,"line_context":0,"max_chars":4000}
```

If looking across a family for requests, use a separate `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,session,seq,preview,json_object('event',ref,'field','text') AS anchor FROM events WHERE role='user' AND system_generated=0 ORDER BY session,seq","page_size":12,"max_chars":4000}
```

Representative response: `result="r1"`, 24 retained rows, 12 displayed, a
`next_cursor`. Next read a chosen row with
`{"workspace":"W","targets":[{"result":"r1","rows":[3]}],"pair":false,"line_context":0}`.
Inspect `system_generated=1` messages separately for completions and `IS NULL`
for unknown origin; do not silently classify either as human. This filter excludes
potentially relevant unknowns, so retain that route if authorship matters.

## 2. Select receipt fields before searching for a failure

**Question:** Which run actually reproduced the error, rather than merely declaring
an exception in source code?

Call `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,seq,json_object('event',ref,'field','text') AS anchor FROM events WHERE session=:s AND seq BETWEEN :lo AND :hi AND kind='result' AND tool IN ('terminal_run','terminal_view','terminal_status') ORDER BY seq","params":{"s":"S","lo":4803,"hi":4915},"page_size":8,"max_chars":3000}
```

Representative response: `result="r2"` with retained terminal-result field anchors.
There is no need to read that whole result merely to scope the search.

Next call `trace_search`:

```json
{"workspace":"W","scope":{"result":"r2"},"query":"(?i)(traceback|attributeerror|typeerror|valueerror|r19[0-9])","mode":"regex","on":"source","collapse":"exact","page_size":8,"max_chars":5000}
```

Representative hit: `result="r3"`, row 0 anchored to `S/e4855` text lines,
excerpt `r196 ... ValueError ...`; a missing-fixture run appears separately.
Next call `trace_read`:

```json
{"workspace":"W","targets":[{"result":"r3","rows":[0]}],"pair":true,"line_context":3,"max_chars":5000}
```

The paired call establishes what ran. This selection excludes asynchronous
completion messages and worker reports; inspect them separately if the terminal
receipt is incomplete. A generated message can be a report rather than a run.
For code/edit intent, make a separate query selecting the appropriate `view`
results or `kind='call' AND tool='str_replace'` with
`json_object('event',ref,'field','arguments') AS anchor`, then search that result.
Do not combine those scopes and count every matching error name as execution.

## 3. Put both comparison receipts before the long chronology

**Question:** What changed between the first attempt and the later run?

Once candidate receipts are located, call `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,seq,preview,json_object('event',ref,'field','text') AS anchor FROM events WHERE session=:s AND (seq BETWEEN 4824 AND 4865 OR seq BETWEEN 4880 AND 4915) ORDER BY CASE WHEN seq IN (4855,4890) THEN 0 ELSE 1 END,seq","params":{"s":"S"},"page_size":8,"max_chars":4000}
```

Representative response: `result="r4"`; first two rows are the candidate receipts
at `S/e4855` and `S/e4890`; the remaining chronology is retained, not discarded.
Next call `trace_read`:

```json
{"workspace":"W","targets":[{"result":"r4","rows":[0,1]}],"pair":false,"line_context":0,"max_chars":5000}
```

Use the actual returned row IDs. For unfamiliar regions, first outline both rather
than inventing receipt addresses. Compare inputs, execution status, failure and
publication separately; a failed collection is not a reproduced test failure.
If these two exact reads fit, do not add inspection merely for ceremony. Record
unvisited side branches rather than declaring the entire chronology reviewed.

## 4. Recover truncated output without inventing historical buffer contents

**Question:** What failed before the repair, and what did the repair actually change?

Call `trace_read` on the receipt:

```json
{"workspace":"W","targets":[{"event":"S/e4855","field":"text"}],"pair":false,"line_context":0,"max_chars":3000}
```

Representative response: a truncated terminal capture and a reference with
`src="S/e4855"`, `dst="S/o_buf_saved"`. Next call `trace_read`:

```json
{"workspace":"W","targets":[{"object":"S/o_buf_saved"}],"pair":false,"line_context":0,"max_chars":5000}
```

If unavailable, the next recovery call is `trace_query`:

```json
{"workspace":"W","sql":"SELECT src AS ref,dst,kind FROM links WHERE dst=:object ORDER BY src","params":{"object":"S/o_buf_saved"},"page_size":12,"max_chars":3000}
```

Representative response: `result="r5"` with referrers containing captured tails or
excerpts. Next read selected referrers, for example:

```json
{"workspace":"W","targets":[{"result":"r5","rows":[0,2]}],"pair":true,"line_context":0,"max_chars":5000}
```

Long reference lists supply `references_result` and `references_cursor`; page the
latter with `trace_query`. A recovered excerpt is not the missing full object.

For the earlier edit, read the actual edit arguments instead of assuming the saved
object represents that time:

```json
{"workspace":"W","targets":[{"event":"S/e4860","field":"arguments"}],"pair":true,"line_context":0,"max_chars":5000}
```

Then locate its save and run receipts. A final buffer may contain the later repair;
a failed-run tail may omit the full exception. Preserve both limits in the claim.

## 5. Inspect equal-text occurrences, then revise the hypothesis

**Question:** Are two reviewers corroborating a failure or repeating a handoff?

Call `trace_search` with a distinctive phrase from the review:

```json
{"workspace":"W","scope":{"workspace":true},"query":"baseline fails before training","mode":"literal","on":"source","collapse":"exact","page_size":8,"max_chars":3000}
```

Representative hit: `occurrence_count=2`, `occurrences_result="r6"`.
Next call `trace_query`:

```json
{"workspace":"W","sql":"SELECT row,ref,anchor FROM result_items WHERE result_id=:r ORDER BY row","params":{"r":"r6"},"page_size":12,"max_chars":3000}
```

Representative response: one occurrence in `S`, another in `T`. Next read those
occurrences and their context:

```json
{"workspace":"W","targets":[{"result":"r6","rows":[0,1]}],"before":1,"after":1,"pair":false,"line_context":0,"max_chars":6000}
```

If these reveal only copied prose, inspect launch and execution receipts before
claiming independent confirmation. After finding fresh-child launch evidence, an
existing episode can be revised with `trace_edit`:

```json
{"workspace":"W","put":[{"id":"repair-origin","note":"Revised: the repairer was a fresh child receiving a copied review, not a resumed review execution. The second occurrence is not independent confirmation.","add_anchors":[{"event":"S/e310","field":"arguments"}],"fields":{"counterevidence":"Fresh-child launch contradicts the earlier resumed-review hypothesis.","unvisited_routes":["Third reviewer session not yet read"],"next_query":"Locate that reviewer's own run receipts before counting confirmations."}}]}
```

This updates an existing entry; to create an episode, also supply `kind`, `title`
and nonempty `anchors`. Notes require `kind` and `title`, but anchors are optional.
Representative response: `updated=["repair-origin"]`.
Next read the revised card using
`{"workspace":"W","targets":[{"entry":"repair-origin","part":"card"}]}`.
Keep the report in an ordinary file, not a giant map note. Retain supporting and
contrary anchors even when revising the explanation.

## 6. Inspect delivery costs, then choose the evidence yourself

**Question:** Will a mixed-length range reach the short clarification and second session?

Call `trace_read` with the same expansion and budget you are considering:

```json
{"workspace":"W","targets":[{"session":"S","events":[2103,2113]},{"event":"T/e23","field":"text"}],"pair":false,"line_context":0,"max_chars":15500,"output":"inline","inspect":true}
```

Representative response fields:

```json
{"plan_result":"r7","total_rows":20,"known_text_chars":21000,"unavailable_rows":1,"partially_delivered_rows":1,"fully_deferred_rows":3,"rows":[{"row":0,"anchor":{"event":"S/e2103","field":"text"},"requested":true,"kind":"message","role":"user","tool":null,"text_chars":204,"would_deliver_chars":204,"available":true}],"plan_cursor":"c7"}
```

This is an abridged preview, not source content. Up to 16 rows fit within a separate
4,000-character display cap. `max_chars` is the simulated read-text allowance;
unavailable text has unknown size, not zero. Added paired/context fields, map cards,
ordering and overlap deduplication follow the ordinary reader. Explicit line slices
precede whole targets even when listed last; target-list order is not priority
(see recipe 8).

Next page the plan with **`trace_query`**:

```json
{"workspace":"W","cursor":"c7","page_size":16,"max_chars":4000}
```

The returned `next_cursor`, if any, continues that query. All plan rows remain in
`r7`; choose desired row IDs, then call a normal `trace_read`:

```json
{"workspace":"W","targets":[{"result":"r7","rows":[0,4,9]}],"pair":false,"line_context":0,"max_chars":6000}
```

`pair=false,line_context=0` avoids expanding the chosen plan again. Inspection does
not automatically select relevance or exclude skills. Cost fields are returned
row metadata, **not columns in `result_items`**. You can join its `ref` to `events`
to filter source kinds, but cannot query `result_items.text_chars`.

`inspect` defaults false, accepts only initial inline requests, and emits no source
bodies. For an ordinary read's `next_cursor`, omit or null `inspect` along with all
selection/expansion arguments; continue only with `workspace`, `cursor` and optional
`max_chars`/`output`. Reinspect if sources or map cards change. Use this extra call
only when its cost information improves the selection.

## 7. Check whether a later change supersedes an early failure

**Question:** Does an early non-primary-checkout rejection still describe the latest
state in the selected history? All references below are hypothetical.

Call `trace_query` to select later message and result **body anchors**, without a
keyword filter on the mechanically clipped `preview` column:

```json
{"workspace":"W","sql":"SELECT ref,seq,json_object('event',ref,'field','text') AS anchor FROM events WHERE session=:s AND seq BETWEEN :lo AND :hi AND kind IN ('message','result') ORDER BY seq","params":{"s":"S","lo":5847,"hi":7383},"page_size":8,"max_chars":3000}
```

Suppose this returns `result="r8"`. Call `trace_search` over that retained region,
including alternate vocabulary rather than only the original `in_place` spelling:

```json
{"workspace":"W","scope":{"result":"r8"},"query":"(?i)(linked.worktree|home.checkout|primary.checkout|in.place)","mode":"regex","on":"source","page_size":8,"max_chars":4000}
```

Suppose a candidate support receipt is row 0 of `r9`. Call `trace_read`:

```json
{"workspace":"W","targets":[{"result":"r9","rows":[0]}],"pair":true,"line_context":3,"max_chars":5000}
```

Follow the candidate's code, test and commit receipts before revising the limitation;
a claim that support landed does not prove a complete round trip. For negative
evidence, page relevant hits and check unavailable notices, then report the interval,
terms and excluded sources (here, other sessions and call arguments), not “no fix
exists.” A no-hit preview SQL filter would not have searched those full bodies.

## 8. Enforce read priority, then hand off individual claims

**Question:** Which comparison claims did this investigator actually check?
All references and outcomes below are hypothetical.

Suppose two whole configuration fields matter before a known long log slice. Call
`trace_read` for the configurations first:

```json
{"workspace":"W","targets":[{"event":"S/e700","field":"text"},{"event":"S/e710","field":"text"}],"pair":false,"line_context":0,"max_chars":5000}
```

After checking delivery, call `trace_read` for the slice:

```json
{"workspace":"W","targets":[{"event":"S/e720","field":"text","lines":[155,182]}],"pair":false,"line_context":0,"max_chars":5000}
```

If combined in one request, the slice precedes both whole fields **even if listed
last**. List order is not priority; separate calls enforce it without inspection.
Use returned line bounds, not guessed original-file lengths.

Suppose the configs show different concurrency, while a seed card's remote-success
claim remains unchecked. Call `trace_edit` to retain claim-specific state:

```json
{"workspace":"W","put":[{"id":"comparison-check","kind":"episode","title":"Comparison: configuration checked, remote outcome still inherited","anchors":[{"event":"S/e700","field":"text"},{"event":"S/e710","field":"text"},{"entry":"seed-comparison","part":"anchors"}],"fields":{"claims":[{"claim":"Concurrency differs: baseline 12, treatment 5.","status":"checked","anchors":["S/e700:text","S/e710:text"],"limit":"Does not isolate a prompt effect.","next_unknown":"Which other configuration values differ?"},{"claim":"Remote rerun succeeded.","status":"inherited","anchors":[{"entry":"seed-comparison","part":"anchors"}],"limit":"Seed claim; remote receipt not rechecked.","next_unknown":"Follow the seed's remote run anchor and inspect its completion receipt."},{"claim":"The comparison held configuration constant.","status":"contradicted","anchors":["S/e700:text","S/e710:text"],"limit":"Contradicts constant configuration, not every possible prompt contribution.","next_unknown":"Find a matched-configuration comparison before causal attribution."}]}}]}
```

These `fields` keys are investigator-authored annotations, not tool verification.
A checked claim does not verify the episode; reopening its card confirms persistence,
not independent corroboration. Keep inherited anchors and unresolved routes visible.


## 9. Join selected candidates to nearby receipts, then inspect both sides

**Question:** Which selected candidate events have a selected receipt within four
later events in the same session? Suppose prior query/search calls returned `r20`
(candidate anchors) and `r21` (receipt anchors). Use actual returned IDs. Call
`trace_cooccur`:

```json
{"workspace":"W","seed":{"result":"r20"},"against":[{"name":"receipt","selection":{"result":"r21"},"before":0,"after":4}],"page_size":2,"max_chars":6000,"save_as":"nearby-receipts"}
```

The returned `result` holds matched seed rows, `unmatched_result` holds failed seeds,
and each condition has its own `witnesses_result`. Read a matched seed with
`trace_read`, substituting the actual returned result ID:

```json
{"workspace":"W","targets":[{"result":"<result>","rows":[0]}],"pair":false,"line_context":0}
```

If `unmatched_cursor` is non-null, call **`trace_query`** to inspect the failed side:

```json
{"workspace":"W","cursor":"<unmatched_cursor>","page_size":10,"max_chars":6000}
```

Read a chosen condition's **witness evidence**, not its seed row, with `trace_read`:

```json
{"workspace":"W","targets":[{"result":"<witnesses_result>"}],"pair":false,"line_context":0,"max_chars":6000}
```

A positive condition retains all witnesses, even beyond its two previews; a failed
condition can retain one nearest out-of-window witness. Inspect signed `delta_seq`
and status before interpreting it. Page the matched `next_cursor` with
`trace_cooccur`, not `trace_query`; page the unmatched query's next cursor with
`trace_query`. To enumerate a witness result first, query `result_items` by its
`result_id` as in recipe 5. Saved `nearby-receipts` reruns these fixed result selectors,
not their original searches. An unmatched seed is only a bounded positional finding;
check coverage and exclusions, then author any substantive claim yourself.

## 10. Search an interval without object expansion, then follow a child handoff

**Question:** What did this interval claim, and what evidence did the named child produce?
Call `trace_query` to retain event refs (not a keyword filter on previews):

```json
{"workspace":"W","sql":"SELECT ref,seq,kind,role,tool,preview FROM events WHERE session=:s AND seq BETWEEN :lo AND :hi ORDER BY seq","params":{"s":"S","lo":700,"hi":760},"page_size":8}
```

Suppose it returns `r30`. Call `trace_search`:

```json
{"workspace":"W","scope":{"result":"r30"},"query":"handoff|completed|failed","mode":"regex","include_references":false,"page_size":8}
```

This searches selected event fields without implicitly following referenced objects.
Explicit objects and workspace/session-scope objects are not removed by the flag.
Check `scope_summary` and unavailable notices: sequence bounds are envelopes that can
contain gaps, not proof of contiguous review or semantic completeness. Co-occurrence
result scopes never implicitly expand under either setting.

Suppose a handoff hit is row 0 in `r31`. Call `trace_read`:

```json
{"workspace":"W","targets":[{"result":"r31","rows":[0]}],"pair":false,"line_context":0,"output":"compact"}
```

If `session_navigation` displays child `dst="T"`, inspect its supporting `anchor`
with `trace_read` (substitute the actual anchor; these bounds are hypothetical):

```json
{"workspace":"W","targets":[{"event":"S/e705","field":"text","lines":[1,6]}],"pair":false,"line_context":0}
```

For overflow, page `session_navigation_cursor` with `trace_query`; selecting rows of
`session_navigation_result` reads those launch anchors, **not the child**. A declared
output reservation is not completion or correctness. Copied receipts are observed
associations; preserve conflicting rows and check `observed_session`, `parent_status`
and `ambiguous`. Only recognized whole `submit_rlm` result receipts derive these links.

To follow the displayed child, call `trace_query`:

```json
{"workspace":"W","sql":"SELECT id,parent_id,title FROM sessions WHERE id=:child","params":{"child":"T"}}
```

If the child is not indexed, and accessing it is authorized, explicitly call
`trace_open`; navigation itself never loads it:

```json
{"workspace":"W","sessions":["T"],"descendants":false}
```

Then select child events with `trace_query`:

```json
{"workspace":"W","sql":"SELECT ref,seq,kind,role,tool,preview FROM events WHERE session=:child ORDER BY seq","params":{"child":"T"},"page_size":8}
```

Use the returned result and chosen row IDs in `trace_read` (here, `r32` and row 0):

```json
{"workspace":"W","targets":[{"result":"r32","rows":[0]}],"pair":false,"line_context":0,"max_chars":5000}
```

For the reverse question, `sessions.parent_id` supplies recorded parent metadata;
query incoming launch associations separately with `trace_query`:

```json
{"workspace":"W","sql":"SELECT src,dst,kind,job_id,output_name,anchor FROM session_links WHERE dst=:child ORDER BY src,anchor","params":{"child":"T"}}
```

Reading these query rows follows `anchor` to launch evidence, not `dst` to child text.
There is no automatic reverse traversal or canonical-parent repair. Compare the
observed receipt session with recorded parent metadata; author any interpretation
with `trace_edit` map relations, which are separate from recorded `session_links`.
