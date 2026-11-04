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
        targets: References or anchors: {event:ref,field:text,lines:[lo,hi]}, {object:ref}, {session:ID,events:[lo,hi]}, {entry:ID,part:card|anchors}, or {result:ID,rows:[0,1]}. Lines/events are inclusive 1-based; result rows are 0-based.
        before: Preceding event count (default 0).
        after: Following event count (default 0).
        pair: Include paired tool calls/results (default true).
        line_context: Extra lines around line anchors (default 10).
        max_chars: Content-text character budget, excluding headers (default 12000).
        output: inline (default) or buffer (requires the host BufferManager).
        cursor: Continue a read snapshot; only max_chars and output may accompany it.
        projection: content (default) or all; explicit field anchors remain exact.
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
        put: Entry patches; new entries require id, kind (episode/note), title. Optional note, anchors, tags, fields, pinned, details_path, add_anchors, remove_anchors. Episodes require anchors.
        delete: Map entry IDs to delete.
        link: Relation objects with id, from, to, label and optional note.
        unlink: Relation IDs to remove.
    """
    return _dispatch("trace_edit", workspace, state, put=put, delete=delete,
                     link=link, unlink=unlink)


# The host decorator currently handles scalars/lists but treats dict and multi-type
# unions as strings. Supply accurate JSON fragments on OUR tool schemas, without
# changing the host generator or replacing the explicit native signatures.
def _structured_parameter(component, name, fragment):
    properties = component.schema["function"]["parameters"]["properties"]
    properties[name] = {"description": properties[name]["description"], **fragment}


for _component, _name in ((trace_query, "params"), (trace_query, "sample"),
                          (trace_search, "scope")):
    _structured_parameter(_component, _name, {"type": ["object", "null"]})
for _name in ("put", "link"):
    _structured_parameter(trace_edit, _name,
                          {"type": ["array", "null"], "items": {"type": "object"}})
_structured_parameter(trace_read, "targets", {
    "type": ["array", "null"],
    "items": {"anyOf": [{"type": "string"}, {"type": "object"}]},
})

TOOLS = (trace_open, trace_query, trace_search, trace_read, trace_edit)


def register_features(builder, *, session, config):
    """Register the same five tools in both common-scope pipelines."""
    builder.add(Feature(name="session_postmortem", components=list(TOOLS)))
