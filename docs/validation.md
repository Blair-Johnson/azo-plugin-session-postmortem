# Implementation validation

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
