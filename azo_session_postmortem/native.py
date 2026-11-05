"""Native Agent Zoo tools; cached evidence remains independent of the host."""

from pathlib import Path

from agent_utils import Feature, tool
from agent_utils.session_cwd import resolve_session_path

from . import cache, cache_render


_CWD_READS = {"session_cwd", "terminal_cwd"}
_RETRIEVAL_READS = _CWD_READS | {"buffer_manager", "step"}


def _dispatch(name, workspace, state, **arguments):
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a nonempty directory path")
    arguments = {key: value for key, value in arguments.items() if value is not None}
    arguments["workspace"] = str(resolve_session_path(workspace, state))
    if name == "trace_open" and isinstance(arguments.get("sessions"), list):
        sources = []
        for source in arguments["sessions"]:
            if isinstance(source, str) and source:
                candidate = resolve_session_path(source, state)
                if candidate.exists() or "/" in source or "\\" in source or source.endswith(".json"):
                    source = str(candidate)
            sources.append(source)
        arguments["sessions"] = sources
    manager = None
    if name != "trace_edit":
        manager = getattr(state, "buffer_manager", None)
        # Preflight every retrieval before cache dispatch can create a workspace,
        # result identity, or durable text file. Never fabricate buffer handles.
        if manager is None or not callable(getattr(manager, "create_scratch_buffer", None)):
            raise ValueError("retrieval requires the host BufferManager")
    result = cache.dispatch(name, arguments)
    buffer_id = ""
    if manager is not None:
        buffer_id, _ = manager.create_scratch_buffer(
            result["text"], path=result["path"], turn=getattr(state, "step", 0)
        )
    return cache_render.render_preview(result["document"], buffer_id, result["preview_chars"])


@tool(optional_reads=_RETRIEVAL_READS, writes={"buffer_manager"})
def trace_open(
    workspace: str,
    sessions: list[str] | None = None,
    descendants: bool | None = None,
    refresh: bool | None = None,
    preview_chars: int | None = None,
    state=None,
) -> str:
    """Open or resume an investigation; cache its complete catalog in a managed buffer.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        sessions: Source session IDs, locators, or JSON paths; omit to resume.
        descendants: Include descendants when opening sources (default true).
        refresh: Explicitly refresh pinned sources (default false).
        preview_chars: Inline preview budget (default 6000); zero suppresses the preview, not the full buffer.
    """
    return _dispatch("trace_open", workspace, state, sessions=sessions,
                     descendants=descendants, refresh=refresh, preview_chars=preview_chars)


@tool(optional_reads=_RETRIEVAL_READS, writes={"buffer_manager"})
def trace_query(
    workspace: str,
    sql: str | None = None,
    params: dict[str, object] | None = None,
    sample: dict[str, object] | None = None,
    saved: str | None = None,
    save_as: str | None = None,
    preview_chars: int | None = None,
    state=None,
) -> str:
    """Run read-only SQL and cache complete rows with stable item selectors.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        sql: SELECT over sessions, events, objects, links, session_links, map_entries, map_links, map_members, result_items, or saved_queries.
        params: Named SQL scalar parameter values, e.g. {"role": "user"}.
        sample: Deterministic sample object with n and optional by (column names) and seed.
        saved: Run a saved query instead of sql, params, or sample.
        save_as: Save this query recipe under a caller-chosen name.
        preview_chars: Inline preview budget (default 6000); complete values remain in the managed buffer.
    """
    return _dispatch("trace_query", workspace, state, sql=sql, params=params,
                     sample=sample, saved=saved, save_as=save_as, preview_chars=preview_chars)


@tool(optional_reads=_RETRIEVAL_READS, writes={"buffer_manager"})
def trace_search(
    workspace: str,
    query: str | None = None,
    scope: dict[str, object] | None = None,
    mode: str | None = None,
    context_lines: int | None = None,
    on: str | None = None,
    collapse: str | None = None,
    projection: str | None = None,
    follow_object_links: bool | None = None,
    saved: str | None = None,
    save_as: str | None = None,
    preview_chars: int | None = None,
    state=None,
) -> str:
    """Search exact evidence or authored map entries; cache every lexical hit.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        query: Search text or regex, according to mode.
        scope: One selector: workspace, sessions, entries, refs, items (immutable item IDs), or result with optional cooccur group/rows.
        mode: terms (default), literal, or regex; embeddings are unsupported.
        context_lines: Source lines around each hit in the full buffer (default 2); aliases still select exact matching lines.
        on: source (default), map, or both; raw and authored hits remain separate.
        collapse: none (default) or exact to group identical occurrences.
        projection: content (default) excludes transport metadata; all includes every field.
        follow_object_links: Follow implicit event-to-object references (default true). False keeps event-only selections within their fields; explicit objects remain. Omit with saved.
        saved: Run a saved search instead of supplying new search selectors.
        save_as: Save the search recipe under this name.
        preview_chars: Inline preview budget (default 6000); all hits remain in the managed buffer.
    """
    return _dispatch("trace_search", workspace, state, query=query, scope=scope,
                     mode=mode, context_lines=context_lines, on=on, collapse=collapse,
                     projection=projection, follow_object_links=follow_object_links,
                     saved=saved, save_as=save_as, preview_chars=preview_chars)


@tool(optional_reads=_RETRIEVAL_READS, writes={"buffer_manager"})
def trace_read(
    workspace: str,
    targets: list[str | dict[str, object]],
    before: int | None = None,
    after: int | None = None,
    pair: bool | None = None,
    line_context: int | None = None,
    projection: str | None = None,
    preview_chars: int | None = None,
    state=None,
) -> str:
    """Read exact evidence or map cards into a complete indexed managed buffer.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        targets: Source refs or immutable item IDs; whole event fields, objects, line slices, session event ranges, map cards/anchors, or result selections with optional group/rows. Lines/events are inclusive 1-based; rows are 0-based.
        before: Preceding event count (default 0).
        after: Following event count (default 0).
        pair: Include paired tool calls/results (default true).
        line_context: Extra lines around line anchors (default 10).
        projection: content (default) or all; explicit field anchors remain exact.
        preview_chars: Inline preview budget (default 12000); zero suppresses the preview, never the complete buffer.
    """
    return _dispatch("trace_read", workspace, state, targets=targets, before=before,
                     after=after, pair=pair, line_context=line_context,
                     projection=projection, preview_chars=preview_chars)


@tool(optional_reads=_CWD_READS)
def trace_edit(
    workspace: str,
    put: list[dict[str, object]] | None = None,
    delete: list[str] | None = None,
    link: list[dict[str, object]] | None = None,
    unlink: list[str] | None = None,
    state=None,
) -> str:
    """Atomically update the authored map, resolving item aliases to exact source anchors.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        put: Entry patches; new entries require id, kind (episode/note), title. Omitted fields stay unchanged; null clears note, tags, fields, pinned, details_path. Episodes require anchors (source addresses or item IDs). Use anchors OR add_anchors/remove_anchors, not both.
        delete: Map entry IDs to delete.
        link: Relation objects with id, from, to, label and optional note.
        unlink: Relation IDs to remove.
    """
    return _dispatch("trace_edit", workspace, state, put=put, delete=delete,
                     link=link, unlink=unlink)


@tool(optional_reads=_RETRIEVAL_READS, writes={"buffer_manager"})
def trace_cooccur(
    workspace: str,
    seed: dict[str, object] | None = None,
    against: list[dict[str, object]] | None = None,
    saved: str | None = None,
    save_as: str | None = None,
    preview_chars: int | None = None,
    state=None,
) -> str:
    """Join retained event selections; cache all matched/unmatched seeds and witnesses.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        seed: {items:[IDs]} or {result:ID} with optional group (matched/unmatched/witnesses) and rows. Omitted rows select all; [] selects none.
        against: One to three named conditions with selection (same shape as seed), nonnegative before/after event windows; nested nulls invalid.
        saved: Rerun a saved cooccur recipe against its fixed input selections; cannot accompany seed/against.
        save_as: Save selectors and windows as a reusable recipe.
        preview_chars: Inline preview budget (default 6000); full relations and witness links remain in the managed buffer.
    """
    return _dispatch("trace_cooccur", workspace, state, seed=seed, against=against,
                     saved=saved, save_as=save_as, preview_chars=preview_chars)


# The host decorator cannot infer dictionaries or mixed unions. Bare objects also
# leave providers with no model-visible keys. Keep explicit schemas here, without
# changing the engine or coercing JSON strings into structured arguments.
def _structured_parameter(component, name, fragment):
    properties = component.schema["function"]["parameters"]["properties"]
    properties[name] = {"description": properties[name]["description"], **fragment}


def _object(properties, required):
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


def _array(items, **constraints):
    return {"type": "array", "items": items, **constraints}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_STRING = {"type": "string"}
_STRINGS = _array(_STRING)
_RANGE = _array({"type": "integer", "minimum": 1}, minItems=2, maxItems=2)
# Closed, fully required alternatives survive strict provider conversion.
_ROWS = _array({"type": "integer", "minimum": 0})
_RESULT_ID = {"type": "string", "minLength": 1}
_GROUP = {"type": "string", "enum": ["matched", "unmatched", "witnesses"]}
_RESULT_VARIANTS = [
    _object({"result": _RESULT_ID}, ["result"]),
    _object({"result": _RESULT_ID, "rows": _ROWS}, ["result", "rows"]),
    _object({"result": _RESULT_ID, "group": _GROUP}, ["result", "group"]),
    _object({"result": _RESULT_ID, "group": _GROUP, "rows": _ROWS},
            ["result", "group", "rows"]),
]
_ITEMS = _object({"items": _array(_RESULT_ID)}, ["items"])
_RESULT_SELECTION = {"anyOf": [*_RESULT_VARIANTS, _ITEMS]}
_ADDRESS_VARIANTS = [
    _STRING,
    _object({"event": _STRING, "field": _STRING, "lines": _RANGE}, ["event"]),
    _object({"object": _STRING, "lines": _RANGE}, ["object"]),
    _object({"session": _STRING, "events": _RANGE}, ["session", "events"]),
    *_RESULT_VARIANTS,
    # Separate whole-content branches survive providers requiring every property.
    # Keep the richer variants above for slices and existing optional defaults.
    _object({"event": _STRING, "field": _STRING}, ["event", "field"]),
    _object({"object": _STRING}, ["object"]),
]
_SOURCE_ANCHOR = {"anyOf": [*_ADDRESS_VARIANTS,
    _object({"entry": _STRING, "part": {"type": "string", "enum": ["anchors"]}},
            ["entry", "part"]),
]}
_READ_TARGET = {"anyOf": [*_ADDRESS_VARIANTS,
    _object({"entry": _STRING, "part": {"type": "string", "enum": ["card", "anchors"]}},
            ["entry"]),
]}
_ANCHORS = _array(_SOURCE_ANCHOR)

# These two dictionaries intentionally have caller-chosen keys. Do not close them
# with additionalProperties=False: that advertises only {} and loses valid data.
# SQL bindings are SQLite scalar values; authored fields allow arbitrary JSON.
_PARAMS = {"type": "object", "additionalProperties": {
    "type": ["string", "number", "boolean", "null"],
}}
_FIELDS = {"type": "object", "additionalProperties": True}
_PATCH = _object({
    "id": {"type": "string", "minLength": 1},
    "kind": {"type": "string", "enum": ["episode", "note"]},
    "title": _STRING,
    "note": _nullable(_STRING),
    "anchors": _ANCHORS,
    "tags": _nullable(_STRINGS),
    "fields": {**_nullable(_FIELDS), "description":
               "Authored JSON object with arbitrary keys; merges by key. Null values "
               "delete keys; null clears all fields. Omit to preserve existing fields."},
    "pinned": _nullable({"type": "boolean"}),
    "details_path": _nullable(_STRING),
    "add_anchors": _ANCHORS,
    "remove_anchors": _ANCHORS,
}, ["id"])
_RELATION = _object({
    "id": {"type": "string", "minLength": 1},
    "from": {"type": "string", "minLength": 1},
    "to": {"type": "string", "minLength": 1},
    "label": {"type": "string", "minLength": 1},
    "note": _nullable(_STRING),
}, ["id", "from", "to", "label"])
_SAMPLE = _object({
    "n": {"type": "integer", "minimum": 0},
    "by": _STRINGS,
    "seed": {"type": ["number", "string", "boolean", "null"]},
}, ["n"])
_SCOPE = {"anyOf": [
    _object({"workspace": {"type": "boolean", "enum": [True]}}, ["workspace"]),
    _object({"sessions": _STRINGS}, ["sessions"]),
    _object({"entries": _STRINGS}, ["entries"]),
    _object({"refs": _ANCHORS}, ["refs"]),
    *_RESULT_VARIANTS,
    _ITEMS,
]}


_COOCCUR_CONDITION = _object({
    "name": {"type": "string", "minLength": 1,
             "description": "Unique condition name; seed is reserved."},
    "selection": _RESULT_SELECTION,
    "before": {"type": "integer", "minimum": 0},
    "after": {"type": "integer", "minimum": 0},
}, ["name", "selection", "before", "after"])

for _component, _name, _schema in (
    (trace_cooccur, "seed", _RESULT_SELECTION),
    (trace_cooccur, "against", _array(_COOCCUR_CONDITION, minItems=1, maxItems=3)),
    (trace_query, "params", _PARAMS),
    (trace_query, "sample", _SAMPLE),
    (trace_search, "scope", _SCOPE),
    (trace_edit, "put", _array(_PATCH)),
    (trace_edit, "link", _array(_RELATION)),
):
    _structured_parameter(_component, _name, _nullable(_schema))
for _component, _name, _values in (
    (trace_search, "mode", ["terms", "literal", "regex"]),
    (trace_search, "on", ["source", "map", "both"]),
    (trace_search, "collapse", ["none", "exact"]),
    (trace_search, "projection", ["content", "all"]),
    (trace_read, "projection", ["content", "all"]),
):
    _structured_parameter(_component, _name,
                          _nullable({"type": "string", "enum": _values}))

_structured_parameter(trace_read, "targets", _array(_READ_TARGET))
for _component in (trace_open, trace_query, trace_search, trace_read, trace_cooccur):
    _structured_parameter(_component, "preview_chars",
                          _nullable({"type": "integer", "minimum": 0}))
_structured_parameter(trace_search, "context_lines",
                      _nullable({"type": "integer", "minimum": 0}))

TOOLS = (trace_open, trace_query, trace_search, trace_read, trace_edit, trace_cooccur)


class SessionPostmortemSkill:
    """Publish the bundled skill before the harness renders its skill catalog."""

    optional_reads = {"skill_registry"}
    writes = {"skill_registry"}

    def __init__(self):
        self._registry = None

    def __call__(self, state):
        from agent_utils.skills import SkillSource

        registry = getattr(state, "skill_registry", None)
        if registry is not None and registry is not self._registry:
            registry.add_sources(SkillSource(
                Path(__file__).resolve().parent / "skills/session-postmortem/SKILL.md",
                kind="azo-plugin-session-postmortem", scope="bundled",
                precedence=-5000, native=True, always_list=True,
            ))
            self._registry = registry
        return state


def register_features(builder, *, session, config):
    """Register six tools and their bundled skill in both common-scope pipelines."""
    from agents.system_prompt import SystemPromptSkillList

    builder.add(Feature(
        name="session_postmortem", components=[SessionPostmortemSkill(), *TOOLS],
        order=[SessionPostmortemSkill, SystemPromptSkillList],
    ))
