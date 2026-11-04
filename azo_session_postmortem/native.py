"""Native Agent Zoo tools; the engine remains independent of the host."""

import json
from pathlib import Path

from agent_utils import Feature, tool
from agent_utils.session_cwd import resolve_session_path

from . import engine


_CWD_READS = {"session_cwd", "terminal_cwd"}


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
    if name == "trace_read" and arguments.get("output") == "buffer":
        manager = getattr(state, "buffer_manager", None)
        # Preflight before dispatch: even cursors/packet files must not be written
        # when this host cannot deliver a real managed buffer.
        if manager is None or not callable(getattr(manager, "create_scratch_buffer", None)):
            raise ValueError("output=buffer requires the host BufferManager; use output=inline")
    result = engine.dispatch(name, arguments)
    if name == "trace_open":
        result = {**result, "capabilities": {**result["capabilities"],
                  "buffer_output": "managed buffer ID plus workspace packet file",
                  "query_schema": str(Path(__file__).resolve().parents[1] / "docs" / "api.md")}}
    if manager is not None:
        packet_path = result.get("buffer_path") or result["buffer"]
        # Preserve UTF-8 and newline bytes as characters, unlike universal-newline
        # file opening. A normal managed scratch buffer is readable by view/RLM.
        with Path(packet_path).open("r", encoding="utf-8", newline="") as packet:
            text = packet.read()
        buffer_id, _ = manager.create_scratch_buffer(
            text, path=str(packet_path), turn=getattr(state, "step", 0)
        )
        result = {**result, "buffer": buffer_id, "buffer_path": str(packet_path)}
    return json.dumps(result, ensure_ascii=False)


@tool(optional_reads=_CWD_READS)
def trace_open(
    workspace: str,
    sessions: list[str] | None = None,
    descendants: bool | None = None,
    refresh: bool | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
    state=None,
) -> str:
    """Open or resume an external investigation workspace and inspect its durable map.

    Omit sources to reuse the indexed view. No model calls or automatic summaries.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        sessions: Source session IDs, locators, or JSON paths; omit to resume.
        descendants: Include descendants when opening sources (default true).
        refresh: Explicitly refresh pinned sources (default false).
        page_size: Catalog page size (default 8).
        cursor: Continue an open page; use only workspace and page_size with it.
    """
    return _dispatch("trace_open", workspace, state, sessions=sessions,
                     descendants=descendants, refresh=refresh, page_size=page_size, cursor=cursor)


@tool(optional_reads=_CWD_READS)
def trace_query(
    workspace: str,
    sql: str | None = None,
    params: dict[str, object] | None = None,
    saved: str | None = None,
    sample: dict[str, object] | None = None,
    page_size: int | None = None,
    max_chars: int | None = None,
    cursor: str | None = None,
    save_as: str | None = None,
    state=None,
) -> str:
    """Select structure or map entries with read-only SQL; retain stable result selections.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        sql: SELECT over sessions, events, objects, links, map_entries, map_links, map_members, result_items, or saved_queries.
        params: Named SQL parameter values, e.g. {"role": "user"}.
        saved: Run a saved query instead of sql, params, or sample.
        sample: Deterministic sample object with n and optional by (column names) and seed.
        page_size: Maximum rows per page (default 20).
        max_chars: Display budget in characters (default 6000).
        cursor: Continue a query page; only page_size and max_chars may accompany it.
        save_as: Save this query recipe under a caller-chosen name.
    """
    return _dispatch("trace_query", workspace, state, sql=sql, params=params,
                     saved=saved, sample=sample, page_size=page_size,
                     max_chars=max_chars, cursor=cursor, save_as=save_as)


@tool(optional_reads=_CWD_READS)
def trace_search(
    workspace: str,
    scope: dict[str, object] | None = None,
    query: str | None = None,
    mode: str | None = None,
    on: str | None = None,
    collapse: str | None = None,
    top_k: int | None = None,
    page_size: int | None = None,
    max_chars: int | None = None,
    saved: str | None = None,
    cursor: str | None = None,
    save_as: str | None = None,
    projection: str | None = None,
    state=None,
) -> str:
    """Retrieve raw evidence and/or caller-authored map entries, keeping origins separate.

    Lexical retrieval only; the investigator supplies semantic interpretation and map edits.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        scope: One selector: {workspace:true}, {sessions:[IDs]}, {entries:[map IDs]}, {refs:[anchors]}, or {result:ID}.
        query: Search text or regex, according to mode.
        mode: terms (default), literal, or regex; similar/embeddings are unsupported.
        on: source (default), map, or both; raw and map hits remain separate.
        collapse: none (default) or exact to group identical occurrences.
        top_k: Reserved for unsupported similarity retrieval; lexical modes retain all matches.
        page_size: Maximum hits per page (default 12).
        max_chars: Display budget in characters (default 6000).
        saved: Run a saved search instead of supplying new search selectors.
        cursor: Continue results; only page_size and max_chars may accompany it.
        save_as: Save the search recipe under this name.
        projection: content (default) excludes transport metadata; all includes every field.
    """
    return _dispatch("trace_search", workspace, state, scope=scope, query=query,
                     mode=mode, on=on, collapse=collapse, top_k=top_k,
                     page_size=page_size, max_chars=max_chars, saved=saved,
                     cursor=cursor, save_as=save_as, projection=projection)


@tool(optional_reads=_CWD_READS | {"buffer_manager", "step"}, writes={"buffer_manager"})
def trace_read(
    workspace: str,
    targets: list[str | dict[str, object]] | None = None,
    before: int | None = None,
    after: int | None = None,
    pair: bool | None = None,
    line_context: int | None = None,
    max_chars: int | None = None,
    output: str | None = None,
    cursor: str | None = None,
    projection: str | None = None,
    state=None,
) -> str:
    """Read exact anchored evidence or map cards, with bounded context and continuation.

    Buffer output returns a real managed buffer ID plus the engine's packet path.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        targets: Whole refs, whole fields {event:ref,field:text}, whole objects {object:ref}, or slices with lines:[lo,hi]. Also {session:ID,events:[lo,hi]}, {entry:ID,part:card|anchors}, or {result:ID,rows:[0,1]}. Lines/events are inclusive 1-based; result rows are 0-based. Omit or use null with cursor.
        before: Preceding event count (default 0); omit or use null with cursor.
        after: Following event count (default 0); omit or use null with cursor.
        pair: Include paired tool calls/results (default true); omit or use null with cursor.
        line_context: Extra lines around line anchors (default 10); omit or use null with cursor.
        max_chars: Content-text character budget, excluding headers (default 12000).
        output: inline (default) or buffer (requires the host BufferManager).
        cursor: Continue an immutable read snapshot using workspace, cursor and optional max_chars/output only. All other arguments must be null or omitted, even default-valued expansion flags.
        projection: content (default) or all; explicit field anchors remain exact. Omit or use null with cursor.
    """
    return _dispatch("trace_read", workspace, state, targets=targets, before=before,
                     after=after, pair=pair, line_context=line_context,
                     max_chars=max_chars, output=output, cursor=cursor, projection=projection)


@tool(optional_reads=_CWD_READS)
def trace_edit(
    workspace: str,
    put: list[dict[str, object]] | None = None,
    delete: list[str] | None = None,
    link: list[dict[str, object]] | None = None,
    unlink: list[str] | None = None,
    state=None,
) -> str:
    """Atomically update the investigator-authored durable map, never the transcript.

    Maintain semantic episodes, evidence anchors, relations and pinned continuation notes.

    Args:
        workspace: Workspace directory, relative to the agent session cwd.
        put: Entry patches; new entries require id, kind (episode/note), title. Omitted fields stay unchanged; null clears note, tags, fields, pinned, details_path. Episodes require anchors. Use anchors OR add_anchors/remove_anchors, not both.
        delete: Map entry IDs to delete.
        link: Relation objects with id, from, to, label and optional note.
        unlink: Relation IDs to remove.
    """
    return _dispatch("trace_edit", workspace, state, put=put, delete=delete,
                     link=link, unlink=unlink)


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
_ADDRESS_VARIANTS = [
    _STRING,
    _object({"event": _STRING, "field": _STRING, "lines": _RANGE}, ["event"]),
    _object({"object": _STRING, "lines": _RANGE}, ["object"]),
    _object({"session": _STRING, "events": _RANGE}, ["session", "events"]),
    _object({"result": _STRING, "rows": _array({"type": "integer", "minimum": 0})},
            ["result"]),
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
    _object({"result": _STRING}, ["result"]),
]}

for _component, _name, _schema in (
    (trace_query, "params", _PARAMS),
    (trace_query, "sample", _SAMPLE),
    (trace_search, "scope", _SCOPE),
    (trace_edit, "put", _array(_PATCH)),
    (trace_edit, "link", _array(_RELATION)),
    (trace_read, "targets", _array(_READ_TARGET)),
):
    _structured_parameter(_component, _name, _nullable(_schema))
for _component, _name, _values in (
    (trace_search, "mode", ["terms", "literal", "regex"]),
    (trace_search, "on", ["source", "map", "both"]),
    (trace_search, "collapse", ["none", "exact"]),
    (trace_search, "projection", ["content", "all"]),
    (trace_read, "projection", ["content", "all"]),
    (trace_read, "output", ["inline", "buffer"]),
):
    _structured_parameter(_component, _name,
                          _nullable({"type": "string", "enum": _values}))

TOOLS = (trace_open, trace_query, trace_search, trace_read, trace_edit)


def register_features(builder, *, session, config):
    """Register the same five tools in both common-scope pipelines."""
    builder.add(Feature(name="session_postmortem", components=list(TOOLS)))
