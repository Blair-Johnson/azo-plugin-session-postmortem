# Implementation validation

## Search scope and recorded session navigation

`trace_search(include_references=False)` disables implicit whole-event expansion into referenced objects. Explicit object selections and objects already included by workspace/session scopes remain searchable. Results preserve a bounded `scope_summary` with complete aggregate counts; sequence bounds are envelopes, not a claim that every intervening event was searched. Invalid read-field errors now identify the offending event and available fields without partially delivering a mixed request.

`trace_read` adds bounded `session_navigation` rows and pageable overflow. These derive only from recognized recorded `submit_rlm` result receipts and one event-to-output-reference hop. The public `session_links` table supports querying the same associations. Links retain an exact launch witness, distinguish missing indexed children, and flag ambiguous associations or parent-metadata mismatches. They describe observed submissions and declared output reservations, not verified completion, correctness, or canonical ancestry. Opening an existing workspace backfills derived links; no destination is loaded automatically.

The integrated managed-host suite passed all 176 tests without skips in `rx r49` (`c1`). Focused worker runs `r46` and `r47` passed 80 engine/regression checks and 18 parser/navigation checks respectively. A parent-only native fixture initially failed in `r48` because its explicit object naming did not model a saved buffer; it was corrected to use the actual saved-buffer representation before the full suite. An independent static review found no material correctness issues. `r51` built and initialized both isolated host pipelines with six tools, the model-visible search control and no warnings.

Known-case replay `r50` (`c50`) copied the 58-session round4 G workspace through a read-only connection. Reading parent event `0ca0982e-7080-472a-8c28-67f3ad2c16f0/e2712` exposed output `buf_f58d78`'s recorded child `ef6adac4-4da5-4517-a171-9403cec73d5e` and the supporting launch at parent event 2648. The replay followed that witness and a child receipt, and checked the previously unhelpful invalid-field case. Source-table hashes and authored maps were unchanged in the copy. This demonstrates recovery of the known relationship, not a measured reduction in fresh-investigator time or missed evidence.

Bundled guidance validation `r52` parsed all 43 JSON examples and passed the skill-format check. Dependency-free wheel smoke `r53` (`c9`) verified the packaged navigation module and skill, then exercised scoped search and missing-child navigation through isolated CLI processes, alongside the existing inspection and co-occurrence checks.
## Co-occurrence retrieval

`trace_cooccur` is a sixth native and CLI operation. It joins retained search/query selections by inclusive, same-session event distance. Matched seeds, unmatched seeds and exact witness anchors remain independently navigable; failed conditions retain a nearest same-session witness when one exists. It does not infer semantic relationships. Guidance ships in the bundled skill; graph-neighbor retrieval remains a separate proposal.

The final managed-host suite passed all 135 tests without skips in `rx r39` (`c1`). `r36` built both pipelines with six tools; `r37` exercised the dependency-free wheel's CLI, navigation and bundled skill. The skill-format validator passed. Independent review found that inherited whole-event search behavior followed referenced objects from joined results. The fix passed 45 focused checks in `r38`, including seven new regressions, and is included in the final full suite. Joined-result searches stay within selected event fields; ordinary selections, stored anchors, read expansion and map behavior remain unchanged.

Replay `r34` (`c34`) used a disposable copy of the seven-session, 5,842-event study index. Five cases reproduced the recorded memberships, with disjoint/exhaustive matched and unmatched sets and unchanged maps. Runtime matching at radius five retained 1,084 of 1,523 seeds. Workdir matching retained 13 of 535, but none of five nominated positive targets. Prompt matching retained 0, 15 and 189 of 319 seeds at radii zero, five and 100; the radius-five unmatched result exposed a readable outcome witness 98 events after its seed. Radius 100 retained all three nominated prompt targets, versus two at radius five. These are known-case regressions, not complete relevance labels or evidence of improved investigator speed or answer quality. The replay enumerates all results for verification; its character totals are not recommended interactive budgets.

A final replay retry (`r40`) was lost after an `rx` SQLite I/O error, with about 134 MiB free on the filesystem. It produced no verified replay result; the chained wheel retry did not run. Earlier `r34` replay and `r37` wheel checks remain valid for their snapshots; the post-fix `r39` full suite passed. Only 240 KiB of disposable failed-run scratch was removed. Session data and unrelated files were untouched. Free disk space before further large CTF workspaces; fresh native comparisons require installation and user reload.

## Compact reads and investigation guidance

`trace_read(output="compact")` is an opt-in rendering change; the default inline format and buffer exports remain unchanged. It removes only reconstructible bookkeeping, retaining otherwise irrecoverable legacy delivery records. The bundled skill now covers claim-specific handoffs, bounded later-evidence searches, mixed-selector ordering and compact output. It does not automatically verify claims or infer semantic relationships.

The final host suite passed all 90 tests in `rx r21` (`c1`), including 14 compact-rendering cases and four replay-checker cases. `r22` built both host pipelines; `r23` validated wheel packaging, the bundled skill and isolated CLI operation. The skill-format validator also passed. A static independent review found no material renderer bug; its two replay-validation findings were fixed before the final replay: deliveries are reconstructed and checked, and reference-overflow payloads are compared rather than only their IDs.

On disposable copies of existing CTF workspaces, strengthened replays `r24`–`r26` compared inline, compact and alternating output pages. The long mixed read returned 37,225 content characters over two pages, with native response size falling from 50,248 to 45,791 characters. The short read returned 2,805 content characters on one page, falling from 5,976 to 4,690. A handoff card returned 1,681 content characters over three pages, falling from 3,468 to 3,066. Source/card text, delivery state, reference navigation and persisted maps matched. Counts are actual native JSON response characters, not tokens; no investigator speed or quality benefit is established. Initial replays `r16`–`r18` had weaker validation and are superseded by these checks. All successful recorded runs used disk-safe execution, retaining source snapshots and logs without execution worktrees.

The accompanying advanced-retrieval studies are separate retrospective investigations, not shipped APIs. Their private-source case reports and exact query recipes live outside this repository under `postmortem-trials/retrieval-study`; they are not bundled in the plugin.

## Read inspection and bundled skill refinement

The pre-refinement baseline is commit `90a32021f28d73a1c4e660a71877100ee836cb25`. The final host suite passed all 72 tests with no skips in `rx r7` (`c1`), source snapshot `f05a97a04fd4178f8707ac2d39778b48fdb8d5bf`. Ordinary reads retain their behavior; optional `inspect=true` exposes metadata-only, pageable read plans. Investigation guidance is shipped as the `session-postmortem` skill, not a guide-path capability.

`r8` dry-built both default and RLM pipelines, checking the five tools, model-visible inspection argument and skill registration before the skill catalog. `r9` built the dependency-free wheel, verified the bundled skill, and exercised CLI inspection in isolation. The skill-format validator passed separately.

`r10` tested a disposable copy of the saved mixed-length CTF read. Predicted and actual per-field delivery matched: 15 plan rows, 15,500 text characters, one partial row and four fully deferred rows. `r11` reopened a copied 58-session investigation, preserved its ten map entries and nine relations, exported a native buffer and resumed an old read cursor. Original workspaces were read-only. These checks do not establish improved investigator performance; fresh pre/post CTF comparisons wait for the user's reload.

All recorded checks used disk-safe execution: logs and source snapshots remain, successful execution worktrees were removed. The following sections describe the initial packaging validation historically, before later installs and native trials.

## Initial packaging validation (historical)

This repository was validated locally without installing or reloading the live plugin. The source-plugin installer dry run recognized the manifest and proposed the common-scope `session_postmortem.py` entry. No new remote repository or commit was created.

## Completed checks

The standalone Pixi environment used Python 3.15.2 and SQLite 3.54.1. `pixi run test`'s underlying unittest command discovered 38 cases: 25 passed and 13 explicitly skipped because host libraries were absent. No core test failed.

The managed Agent Zoo runtime environment used Python 3.12.15 and SQLite 3.54.1. Running the complete unittest suite there passed all 38 cases with no skips. The 26 backend/source cases include the original prototype regressions plus stale-anchor rejection, late-in-line search excerpts, complete lexical counts, bounded query previews, refresh rollback/state preservation and state-home precedence. The 12 native cases exercise ordinary Tool/State dispatch, published schemas, relative workspace/source paths, saved selections/cursors, map edits, exact managed-buffer contents, missing-manager preflight and symlinked loading from an unrelated cwd.

`scripts/validate_host.py` built and initialized both real default and RLM pipelines through the host's plugin inspector using a temporary plugin root. Each pipeline exposed exactly one copy of all five tools, with `workspace` in the schema and no model-visible `state` parameter. Both reported no warnings. Static inspection of the native module also reported no warnings.

`scripts/check_distribution.py` built `azo_plugin_session_postmortem-0.1.0-py3-none-any.whl`, verified its console entry point and package contents, and opened the synthetic session through the wheel's CLI from an isolated process/directory without installing the package. This checks distribution mechanics, not a live TUI reload.

A local compatibility check used a read-only connection to an existing prototype workspace and a temporary SQLite backup. Through native Tool/State dispatch, the copy reopened one session containing 2,723 events, 379 objects, four map entries, two authored links and four saved queries. It exported a real managed buffer and resumed an old read cursor. All stored map-entry, membership and authored-link rows remained equal. The original workspace was never opened for writing, and neither its source text nor its database is distributed in this repository.

## Reproduce

From this checkout:

```sh
pixi run test
pixi run python scripts/check_distribution.py
```

In an existing Agent Zoo runtime Python environment, from this checkout:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/validate_host.py --agent-zoo /path/to/agent-zoo
python -m scripts.check_workspace /path/to/existing/investigation
```

The last command copies the supplied workspace into temporary storage and prints only counts/check outcomes. It requires a nonempty indexed session; the source workspace is read-only. It does not rerun saved tools or historical experiments.

The repository was newly initialized without a first commit. `rx` refused to snapshot an unborn HEAD, so these checks ran directly and their outcomes are recorded here; there is no corresponding successful rx run ID. After creating the first commit, future changes can use ordinary rx snapshots.

## What remains unvalidated

The live harness has not loaded this plugin through `/reload`; perform that step after an explicit installation. Tests cover macOS locally, not every platform in the Pixi lockfile. Earlier prototype investigations motivated the interfaces, but this packaging pass did not repeat the semantic CTF grading, measure analysis speed, or establish causal A/B effectiveness. Opaque legacy buffers, binary attachments, sequence-anchor meaning after source reordering, large materialized searches and expensive regular expressions retain the limits documented in the README and API.
