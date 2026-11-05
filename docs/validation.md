# Validation

## Current interface

Native retrieval returns useful inline text and a complete indexed managed buffer. Result and item IDs work in later reads, searches, joins and map annotations. Native tools have no pagination, inspection or output-format arguments. The separate structured Python/CLI engine retains those older controls for compatibility.

The final managed-host suite (`rx r117`) passed 333 tests and 355 subtests without skips. It covers real managed buffers, complete indexed source delivery, durable selectors, frozen source membership after map edits, search scope, missing-source reporting, co-occurrence navigation, and structured engine compatibility. Single-occurrence search hits do not repeat their excerpts inline; occurrence pointers and full-buffer entries remain available.

The installed tools were checked after user reload. A search returned 95 collapsed hits with useful inline excerpts; ordinary buffer grep located the last hit, and an item ID retrieved its source directly. Repeating the search after the singleton fix confirmed the duplicate excerpts were gone.

## Additional checks

Synthetic native workflow `r111` retained all 80 search hits and a 40,000-line log, kept short requested targets visible, exposed both co-occurrence branches, and reused aliases after replacing the transient BufferManager. Copied-workspace check `r112` reopened a 58-session, 15,688-event investigation and followed a query item without changing its ten map entries or nine authored relations. Original investigations were read-only.

At the buffer-native implementation snapshot, portable run `r113` passed 274 tests and 258 subtests, with 54 explicit host-only skips. Run `r114` initialized both isolated default and RLM pipelines with six native tools. Wheel check `r115` verified packaged modules and bundled skill, isolated legacy CLI compatibility, and item selection across fresh processes. These precede the small singleton-rendering fix, which is covered by the later full host suite.

Run IDs refer to local rx records, not artifacts distributed with the package. Historical implementation notes and earlier interface validation are available in Git history.

## Reproduce

From this checkout:

```sh
pixi run test
pixi run python scripts/check_distribution.py
```

Using an existing Agent Zoo runtime Python environment, from this checkout:

```sh
python -m pytest tests -q
python scripts/validate_host.py --agent-zoo /path/to/agent-zoo
python scripts/check_cache_workflows.py
python -m scripts.check_workspace /path/to/existing/investigation
```

The host validator builds isolated pipelines without changing the live installation. The workspace check uses a temporary copy; it does not rerun historical tools or experiments. The bundled skill contains investigation guidance; `docs/api.md` documents the interfaces.

## Limits

These checks establish interface behavior, not a causal improvement in investigator speed or semantic answer quality. Fresh comparative CTFs have not yet evaluated the buffer-native interface. Platform testing was local to macOS. Missing source bodies remain missing; complete retrieval is not complete review. Reloading an existing source conservatively invalidates cache aliases, while adding disjoint sessions preserves them. Large materialized searches and expensive regular expressions retain the limits documented in the API.
