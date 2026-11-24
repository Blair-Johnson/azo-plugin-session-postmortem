"""Native Agent Zoo tools; cached evidence remains independent of the host."""

from pathlib import Path

from agent_utils.core.events import Event, RenderRequested, TurnSettled
from agent_utils.core.pipeline import EmitFn, Feature
from agent_utils.core.state import State
from agent_utils.core.tool import Tool
from agent_utils.features.buffers.tools import resolve_session_path
from agent_utils.features.skills import SkillSource, parse_skill_text

from . import cache, cache_render

try:
    from agent_zoo.events import SessionReady as _SessionReady
    _SKILL_EVENTS: tuple[type[Event], ...] = (_SessionReady, RenderRequested, TurnSettled)
except ImportError:
    _SKILL_EVENTS = (RenderRequested, TurnSettled)


_CWD_READS = {"cwd", "session_cwd", "terminal_cwd"}
_RETRIEVAL_READS = _CWD_READS | {"buffer_manager", "step"}


def _resolve_path(raw: str, state: State | None) -> Path:
    return Path(resolve_session_path(raw, state)).expanduser().resolve()


def _dispatch(name, workspace, state, **arguments):
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("workspace must be a nonempty directory path")
    arguments = {key: value for key, value in arguments.items() if value is not None}
    arguments["workspace"] = str(_resolve_path(workspace, state))
    if name == "trace_open" and isinstance(arguments.get("sessions"), list):
        sources = []
        for source in arguments["sessions"]:
            if isinstance(source, str) and source:
                candidate = _resolve_path(source, state)
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


class _TraceRetrievalTool(Tool):
    optional_reads = _RETRIEVAL_READS
    writes = {"buffer_manager"}


class TraceOpen(_TraceRetrievalTool):
    name = "trace_open"

    def call(
        self,
        state: State,
        workspace: str,
        sessions: list[str] | None = None,
        descendants: bool | None = None,
        refresh: bool | None = None,
        preview_chars: int | None = None,
    ) -> str:
        """Open or resume a trace workspace; cache its catalog in a buffer.

        Args:
            workspace: Workspace directory.
            sessions: Session IDs/paths; omit to resume.
            descendants: Include children (default true).
            refresh: Reindex sources (default false).
            preview_chars: Preview chars (default 6000; 0 hides preview).
        """
        return _dispatch(
            "trace_open", workspace, state, sessions=sessions,
            descendants=descendants, refresh=refresh, preview_chars=preview_chars,
        )


class TraceQuery(_TraceRetrievalTool):
    name = "trace_query"

    def call(
        self,
        state: State,
        workspace: str,
        sql: str | None = None,
        params: dict[str, object] | None = None,
        sample: dict[str, object] | None = None,
        saved: str | None = None,
        save_as: str | None = None,
        preview_chars: int | None = None,
    ) -> str:
        """Run read-only SQL over a workspace; cache rows with item IDs.

        Args:
            workspace: Workspace directory.
            sql: Read-only SELECT query.
            params: Named SQL scalar bindings.
            sample: Stratified sample: {n, by?, seed?}.
            saved: Saved query name.
            save_as: Save recipe under name.
            preview_chars: Preview chars (default 6000).
        """
        return _dispatch(
            "trace_query", workspace, state, sql=sql, params=params,
            sample=sample, saved=saved, save_as=save_as, preview_chars=preview_chars,
        )


class TraceSearch(_TraceRetrievalTool):
    name = "trace_search"

    def call(
        self,
        state: State,
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
    ) -> str:
        """Search trace evidence or map entries; cache all hits.

        Args:
            workspace: Workspace directory.
            query: Search text or regex.
            scope: {workspace:true}, {sessions}, {entries}, {refs}, {items}, or {result, group?, rows?}.
            mode: terms (default), literal, or regex.
            context_lines: Context lines (default 2).
            on: source (default), map, or both.
            collapse: none (default) or exact.
            projection: content (default) or all.
            follow_object_links: Follow event-to-object links (default true).
            saved: Saved search name.
            save_as: Save recipe under name.
            preview_chars: Preview chars (default 6000).
        """
        return _dispatch(
            "trace_search", workspace, state, query=query, scope=scope,
            mode=mode, context_lines=context_lines, on=on, collapse=collapse,
            projection=projection, follow_object_links=follow_object_links,
            saved=saved, save_as=save_as, preview_chars=preview_chars,
        )


class TraceRead(_TraceRetrievalTool):
    name = "trace_read"

    def call(
        self,
        state: State,
        workspace: str,
        targets: list[str | dict[str, object]],
        before: int | None = None,
        after: int | None = None,
        pair: bool | None = None,
        line_context: int | None = None,
        projection: str | None = None,
        preview_chars: int | None = None,
    ) -> str:
        """Read trace evidence or map cards into an indexed buffer.

        Args:
            workspace: Workspace directory.
            targets: Refs, item IDs, or target objects ({event, field?, lines?}, {object, lines?}, {session, events}, {result, group?, rows?}, {entry, part?}).
            before: Events before (default 0).
            after: Events after (default 0).
            pair: Pair tool calls/results (default true).
            line_context: Context lines (default 10).
            projection: content (default) or all.
            preview_chars: Preview chars (default 12000).
        """
        return _dispatch(
            "trace_read", workspace, state, targets=targets, before=before,
            after=after, pair=pair, line_context=line_context,
            projection=projection, preview_chars=preview_chars,
        )


class TraceEdit(Tool):
    name = "trace_edit"
    optional_reads = _CWD_READS
    writes = set()

    def call(
        self,
        state: State,
        workspace: str,
        put: list[dict[str, object]] | None = None,
        delete: list[str] | None = None,
        link: list[dict[str, object]] | None = None,
        unlink: list[str] | None = None,
    ) -> str:
        """Atomically update the authored map; resolves item aliases to anchors.

        Args:
            workspace: Workspace directory.
            put: Entry patches ({id, kind?, title?, note?, anchors?, add_anchors?, remove_anchors?, tags?, fields?, pinned?, details_path?}).
            delete: Entry IDs to delete.
            link: Relations ({id, from, to, label, note?}).
            unlink: Relation IDs to remove.
        """
        return _dispatch(
            "trace_edit", workspace, state, put=put, delete=delete,
            link=link, unlink=unlink,
        )


class TraceCooccur(_TraceRetrievalTool):
    name = "trace_cooccur"

    def call(
        self,
        state: State,
        workspace: str,
        seed: dict[str, object] | None = None,
        against: list[dict[str, object]] | None = None,
        saved: str | None = None,
        save_as: str | None = None,
        preview_chars: int | None = None,
    ) -> str:
        """Join retained event selections within before/after windows.

        Args:
            workspace: Workspace directory.
            seed: {items} or {result, group?, rows?}.
            against: 1-3 conditions: [{name, selection, before, after}].
            saved: Saved cooccur recipe name.
            save_as: Save recipe under name.
            preview_chars: Preview chars (default 6000).
        """
        return _dispatch(
            "trace_cooccur", workspace, state, seed=seed, against=against,
            saved=saved, save_as=save_as, preview_chars=preview_chars,
        )


# Keep tool schemas flat and compact; full validation of nested shapes runs
# inside cache/engine dispatch and exact object formats are documented in SKILL.md.
def _structured_parameter(component, name, fragment):
    properties = component.schema["function"]["parameters"]["properties"]
    properties[name] = {"description": properties[name]["description"], **fragment}


_NULLABLE_OBJECT = {"type": ["object", "null"]}
_NULLABLE_OBJECT_ARRAY = {"type": ["array", "null"], "items": {"type": "object"}}
_TARGET_ARRAY = {"type": "array", "items": {"type": ["string", "object"]}}

for _component, _name, _schema in (
    (TraceQuery, "params", _NULLABLE_OBJECT),
    (TraceQuery, "sample", _NULLABLE_OBJECT),
    (TraceSearch, "scope", _NULLABLE_OBJECT),
    (TraceEdit, "put", _NULLABLE_OBJECT_ARRAY),
    (TraceEdit, "link", _NULLABLE_OBJECT_ARRAY),
    (TraceCooccur, "seed", _NULLABLE_OBJECT),
    (TraceCooccur, "against", _NULLABLE_OBJECT_ARRAY),
    (TraceRead, "targets", _TARGET_ARRAY),
):
    _structured_parameter(_component, _name, _schema)

trace_open = TraceOpen()
trace_query = TraceQuery()
trace_search = TraceSearch()
trace_read = TraceRead()
trace_edit = TraceEdit()
trace_cooccur = TraceCooccur()

TOOLS = (trace_open, trace_query, trace_search, trace_read, trace_edit, trace_cooccur)


class SessionPostmortemSkill:
    """Publish the bundled skill before the harness renders its skill catalog."""

    handles = _SKILL_EVENTS
    optional_reads = {"skill_registry"}
    writes = {"skill_registry"}

    def __init__(self) -> None:
        self._registry = None
        self._cached_source: SkillSource | None = None
        self._cached_entry = None

    def _load_bundled_skill(self):
        if self._cached_source is None or self._cached_entry is None:
            skill_root = Path(__file__).resolve().parent / "skills" / "session-postmortem"
            skill_file = skill_root / "SKILL.md"
            resources = tuple(
                sorted(
                    p.relative_to(skill_root).as_posix()
                    for p in skill_root.rglob("*")
                    if p.is_file() and p != skill_file
                )
            )
            source = SkillSource(
                path=str(skill_root),
                kind="azo-plugin-session-postmortem",
                scope="bundled",
                precedence=-5000,
                native=True,
                always_list=True,
            )
            entry = parse_skill_text(
                str(skill_file),
                skill_file.read_text(encoding="utf-8"),
                source=source,
                resources=resources,
            )
            self._cached_source = source
            self._cached_entry = entry
        return self._cached_source, self._cached_entry

    def handle(self, event: Event, state: State, emit: EmitFn) -> None:
        del event, emit
        registry = getattr(state, "skill_registry", None)
        if registry is not None and registry is not self._registry:
            source, entry = self._load_bundled_skill()
            registry.add_sources([source])
            registry.register_entry(entry)
            self._registry = registry


def register_features(builder, *, session, config):
    """Register six tools and their bundled skill in both common-scope pipelines."""
    del session, config
    from agent_zoo.features.system_prompt import SystemPromptSkillList

    builder.add(Feature(
        name="session_postmortem",
        components=[SessionPostmortemSkill(), *TOOLS],
        order=[SessionPostmortemSkill, SystemPromptSkillList],
    ))
