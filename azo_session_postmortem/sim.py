"""Deterministic Simulation Testing (DST) hooks for ``azo-plugin-session-postmortem``.

Provides a stateful, strictly deterministic in-memory simulator for
``PostmortemDispatchRequest`` alongside ``fuzz_tool_args`` and
``check_invariants``. Performs zero filesystem, network, clock, or OS I/O so
``--replay`` matches bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import posixpath
import random
import re
from typing import Any

from agent_utils.core.session import Session
from agent_utils.core.state import State
from agent_utils.runtime.simulation import SimOutcome, SimulationRuntime

from . import cache_render
from .native import PostmortemDispatchRequest

_DEFAULT_WORKSPACE = "/workspace/postmortem"
_ALT_WORKSPACES = ("/workspace/postmortem", "/workspace/pm_alt")
_RETRIEVAL_TOOLS = frozenset({
    "trace_open",
    "trace_query",
    "trace_search",
    "trace_read",
    "trace_cooccur",
})
_ALL_TRACE_TOOLS = _RETRIEVAL_TOOLS | {"trace_edit"}
_PREFIX = {
    "trace_search": "S",
    "trace_query": "Q",
    "trace_read": "R",
    "trace_cooccur": "C",
    "trace_open": "O",
}
_ALLOWED_ARGS: dict[str, set[str]] = {
    "trace_open": {"sessions", "descendants", "refresh"},
    "trace_query": {"sql", "params", "sample", "saved", "save_as"},
    "trace_search": {
        "query",
        "scope",
        "mode",
        "context_lines",
        "on",
        "collapse",
        "projection",
        "follow_object_links",
        "saved",
        "save_as",
    },
    "trace_read": {"targets", "before", "after", "pair", "line_context", "projection"},
    "trace_cooccur": {"seed", "against", "saved", "save_as"},
    "trace_edit": {"put", "delete", "link", "unlink"},
}
_RESULT_PATH_RE = re.compile(r"/results/([SQRCO]\d+)\.txt\Z")
_INDEX_ITEM_RE = re.compile(r"^- ([SQRCO]\d+-[A-Z]\d+) \| L\d+-L\d+ \| ")
_ALIAS_RE = re.compile(r"[SQRCO]\d+(?:-[A-Z]\d+)?\Z")
_WRITE_SQL_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b",
    re.IGNORECASE,
)


def _norm_ws(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace is required and must be a directory path")
    norm = posixpath.normpath(raw.strip())
    if not norm.startswith("/"):
        norm = posixpath.normpath(posixpath.join("/workspace", norm))
    return norm


def _validate_nonneg_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _labeled(values: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in values.items())


def _card_text(entry: dict[str, Any]) -> str:
    parts: list[str] = []
    if entry.get("note"):
        parts.append(str(entry["note"]))
    meta = [f"Kind: {entry.get('kind', 'note')}"]
    if entry.get("tags"):
        meta.append("Tags: " + ", ".join(str(t) for t in entry["tags"]))
    if entry.get("anchors"):
        meta.append("Anchors: " + ", ".join(str(a) for a in entry["anchors"]))
    if entry.get("pinned"):
        meta.append("Pinned: true")
    parts.append("\n".join(meta))
    return "\n\n".join(parts)


@dataclass
class _SimEvent:
    ref: str
    session: str
    seq: int
    role: str
    tool: str
    kind: str
    text: str


@dataclass
class _SimWorkspace:
    path: str
    generation: int = 1
    doc_counter: int = 0
    result_counter: int = 0
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[_SimEvent] = field(default_factory=list)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    map_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    map_links: dict[str, dict[str, Any]] = field(default_factory=dict)
    saved_queries: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Maps alias ID (e.g. "O1", "S2-H001", "r1") -> list of event/entry refs.
    alias_refs: dict[str, list[str]] = field(default_factory=dict)
    # Maps doc ID -> group_name -> list of refs.
    doc_groups: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def seed_default_corpus(self) -> None:
        if self.sessions:
            return
        self._add_session("sess-root", "Debug timeout regression", parent_id="")
        self._add_session("sess-child", "Inspect worker retry loop", parent_id="sess-root")
        self.map_entries["finding-1"] = {
            "id": "finding-1",
            "kind": "finding",
            "title": "Retry backoff spike",
            "note": "Observed retry burst before timeout on worker checkpoint.",
            "anchors": ["e:sess-root:3"],
            "tags": ["timeout", "retry"],
            "fields": {"severity": "high"},
            "pinned": True,
            "revision": 1,
        }
        # Pre-seed an initial open alias O1 so tools referencing O1 before
        # explicit trace_open (e.g. right after restore) resolve cleanly.
        self.doc_counter = 1
        all_refs = [ev.ref for ev in self.events]
        self.documents["O1"] = {
            "id": "O1",
            "operation": "trace_open",
            "workspace": self.path,
            "path": "results/O1.txt",
            "generation": self.generation,
        }
        self.alias_refs["O1"] = list(all_refs)
        self.alias_refs["O1-P001"] = ["entry:finding-1"]
        self.alias_refs["O1-S001"] = [ev.ref for ev in self.events if ev.session == "sess-root"]
        self.alias_refs["O1-S002"] = [ev.ref for ev in self.events if ev.session == "sess-child"]

    def _add_session(self, sid: str, title: str, *, parent_id: str = "") -> None:
        if sid in self.sessions:
            return
        self.sessions[sid] = {
            "id": sid,
            "title": title,
            "source": f"sim://{sid}.json",
            "parent_id": parent_id,
            "warnings": [],
        }
        templates = (
            ("user", "", "message", f"[{sid}] Investigate timeout error during checkpoint save."),
            ("assistant", "grep", "tool_call", f"[{sid}] Searching buffer logs for retry backoff."),
            ("tool", "grep", "tool_result", f"[{sid}] ERROR timeout after retry backoff in sqlite cache."),
            ("assistant", "view", "tool_call", f"[{sid}] Inspecting buffer state after checkpoint error."),
            ("assistant", "", "message", f"[{sid}] Confirmed root cause: retry loop held lock on buffer."),
        )
        for seq, (role, tool, kind, text) in enumerate(templates, start=1):
            self.events.append(
                _SimEvent(
                    ref=f"e:{sid}:{seq}",
                    session=sid,
                    seq=seq,
                    role=role,
                    tool=tool,
                    kind=kind,
                    text=text,
                )
            )
        obj_ref = f"o:{sid}:1"
        self.objects[obj_ref] = {
            "ref": obj_ref,
            "session": sid,
            "kind": "artifact",
            "text": f"Captured diagnostics artifact for {sid}: timeout=30s retries=3.",
        }

    def event_by_ref(self, ref: str) -> _SimEvent | None:
        for ev in self.events:
            if ev.ref == ref:
                return ev
        return None

    def next_doc_id(self, operation: str) -> str:
        self.doc_counter += 1
        return f"{_PREFIX[operation]}{self.doc_counter}"

    def next_result_id(self) -> str:
        self.result_counter += 1
        return f"r{self.result_counter}"


class _SimPostmortemStore:
    """Stateful, deterministic in-memory postmortem store for DST simulation."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._workspaces: dict[str, _SimWorkspace] = {}
        default_ws = _SimWorkspace(path=_DEFAULT_WORKSPACE)
        default_ws.seed_default_corpus()
        self._workspaces[_DEFAULT_WORKSPACE] = default_ws

    def hydrate_from_state(self, state: State | None) -> None:
        """Restore workspace aliases from already-open scratch buffers (e.g. after crash/resume)."""
        if state is None:
            return
        bm = getattr(state, "buffer_manager", None)
        if bm is None or not callable(getattr(bm, "buffers", None)):
            return
        for bid in bm.buffers():
            buf = bm.get(bid)
            if buf is None:
                continue
            path = str(getattr(buf, "path", "") or "")
            m = _RESULT_PATH_RE.search(path)
            if not m:
                continue
            doc_id = m.group(1)
            ws_path = _norm_ws(path[: m.start()])
            ws = self._get_workspace(ws_path, create=True)
            try:
                num = int(doc_id[1:])
                if num > ws.doc_counter:
                    ws.doc_counter = num
            except ValueError:
                pass
            all_refs = [ev.ref for ev in ws.events]
            ws.alias_refs.setdefault(doc_id, list(all_refs))
            ws.doc_groups.setdefault(
                doc_id,
                {"matched": list(all_refs[:2]), "unmatched": list(all_refs[2:4]), "witnesses": list(all_refs[:1])},
            )
            ws.documents.setdefault(
                doc_id,
                {
                    "id": doc_id,
                    "operation": "trace_open",
                    "workspace": ws.path,
                    "path": f"results/{doc_id}.txt",
                    "generation": ws.generation,
                },
            )
            for line in str(getattr(buf, "text", "") or "").splitlines():
                im = _INDEX_ITEM_RE.match(line)
                if im:
                    ws.alias_refs.setdefault(im.group(1), [all_refs[0]] if all_refs else [])

    def _get_workspace(self, path: str, *, create: bool = False) -> _SimWorkspace:
        ws = self._workspaces.get(path)
        if ws is None:
            if not create:
                raise ValueError("workspace is not initialized; call trace_open first")
            ws = _SimWorkspace(path=path)
            ws.seed_default_corpus()
            self._workspaces[path] = ws
        return ws

    def _resolve_refs(self, ws: _SimWorkspace, selector: Any) -> list[str]:
        """Resolve any target/alias/selector into concrete event/object/entry refs."""
        if selector is None:
            return [ev.ref for ev in ws.events]
        if isinstance(selector, str):
            if selector in ws.alias_refs:
                return list(ws.alias_refs[selector])
            if ws.event_by_ref(selector) is not None or selector in ws.objects:
                return [selector]
            if selector in ws.map_entries:
                return [f"entry:{selector}"]
            if _ALIAS_RE.fullmatch(selector):
                raise ValueError(f"unknown cache alias in this workspace: {selector}")
            raise ValueError(f"unknown trace target: {selector}")
        if isinstance(selector, list):
            out: list[str] = []
            for item in selector:
                out.extend(self._resolve_refs(ws, item))
            return list(dict.fromkeys(out))
        if isinstance(selector, dict):
            if "items" in selector:
                items = selector["items"]
                if not isinstance(items, list):
                    raise ValueError("items must be a list")
                return self._resolve_refs(ws, items)
            if "result" in selector:
                res_id = str(selector["result"])
                group = selector.get("group")
                if group is not None:
                    groups = ws.doc_groups.get(res_id)
                    if not groups or group not in groups:
                        raise ValueError(f"unknown result group: {group}")
                    refs = list(groups[group])
                elif res_id in ws.alias_refs:
                    refs = list(ws.alias_refs[res_id])
                else:
                    raise ValueError(f"unknown cache alias in this workspace: {res_id}")
                rows = selector.get("rows")
                if rows is not None:
                    if not isinstance(rows, list):
                        raise ValueError("rows must be a list")
                    picked: list[str] = []
                    for idx in rows:
                        _validate_nonneg_int(idx, "row")
                        if idx >= len(refs):
                            raise ValueError("result row out of range")
                        picked.append(refs[idx])
                    return picked
                return refs
            if "event" in selector:
                return self._resolve_refs(ws, str(selector["event"]))
            if "object" in selector:
                return self._resolve_refs(ws, str(selector["object"]))
            if "entry" in selector:
                entry_id = str(selector["entry"])
                if entry_id not in ws.map_entries:
                    raise ValueError(f"unknown map entry: {entry_id}")
                return [f"entry:{entry_id}"]
            if "session" in selector:
                sid = str(selector["session"])
                ev_nums = selector.get("events")
                sess_events = [ev for ev in ws.events if ev.session == sid]
                if not sess_events:
                    raise ValueError(f"unknown session: {sid}")
                if isinstance(ev_nums, list) and ev_nums:
                    wanted = {int(n) for n in ev_nums if isinstance(n, int) and not isinstance(n, bool)}
                    matched = [ev.ref for ev in sess_events if ev.seq in wanted]
                    return matched or [sess_events[0].ref]
                return [ev.ref for ev in sess_events]
            if "sessions" in selector:
                sids = set(selector["sessions"] or ())
                return [ev.ref for ev in ws.events if ev.session in sids]
            if "entries" in selector:
                return [f"entry:{eid}" for eid in selector["entries"] if eid in ws.map_entries]
            if "refs" in selector:
                return self._resolve_refs(ws, list(selector["refs"] or ()))
            if selector.get("workspace") is True:
                return [ev.ref for ev in ws.events]
        raise ValueError(f"invalid trace selector: {selector!r}")

    def dispatch(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in _ALLOWED_ARGS:
            raise ValueError("unknown trace tool: " + str(tool))
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        args = dict(arguments)
        preview = args.pop("preview_chars", None)
        preview = (12000 if tool == "trace_read" else 6000) if preview is None else preview
        _validate_nonneg_int(preview, "preview_chars")
        unknown = set(args) - _ALLOWED_ARGS[tool] - {"workspace"}
        if unknown:
            raise ValueError("unknown arguments: " + ", ".join(sorted(unknown)))
        ws_path = _norm_ws(args.pop("workspace", None))
        args = {k: v for k, v in args.items() if v is not None}
        if "context_lines" in args:
            _validate_nonneg_int(args["context_lines"], "context_lines")

        ws = self._get_workspace(ws_path, create=(tool == "trace_open"))

        if tool == "trace_edit":
            ack = self._handle_edit(ws, args)
            return {"document": {"operation": "trace_edit", "ack": ack}, "preview_chars": preview}

        if tool == "trace_open":
            doc = self._handle_open(ws, args)
        elif tool == "trace_query":
            doc = self._handle_query(ws, args)
        elif tool == "trace_search":
            doc = self._handle_search(ws, args)
        elif tool == "trace_read":
            doc = self._handle_read(ws, args)
        elif tool == "trace_cooccur":
            doc = self._handle_cooccur(ws, args)
        else:  # pragma: no cover
            raise ValueError("unknown trace tool: " + str(tool))

        text = cache_render.render_full(doc)
        rel_path = f"results/{doc['id']}.txt"
        ws.documents[doc["id"]] = {
            "id": doc["id"],
            "operation": tool,
            "workspace": ws.path,
            "path": rel_path,
            "generation": ws.generation,
        }
        return {
            "document": doc,
            "text": text,
            "path": posixpath.join(ws.path, rel_path),
            "preview_chars": preview,
        }

    def _add_block(
        self,
        ws: _SimWorkspace,
        doc: dict[str, Any],
        counters: dict[str, int],
        code: str,
        title: str,
        text: str,
        group: str,
        refs: list[str],
        *,
        requested: bool = True,
        **metadata: Any,
    ) -> dict[str, Any]:
        counters[code] = counters.get(code, 0) + 1
        item_id = f"{doc['id']}-{code}{counters[code]:03d}"
        block = {
            "id": item_id,
            "title": title,
            "text": text,
            "group": group,
            "requested": requested,
            **metadata,
        }
        doc["blocks"].append(block)
        ws.alias_refs[item_id] = list(refs)
        return block

    def _handle_open(self, ws: _SimWorkspace, args: dict[str, Any]) -> dict[str, Any]:
        sessions_arg = args.get("sessions")
        if sessions_arg is not None:
            if not isinstance(sessions_arg, list):
                raise ValueError("sessions must be a list of session IDs or paths")
            for raw_sid in sessions_arg:
                if not isinstance(raw_sid, str) or not raw_sid.strip():
                    raise ValueError("session entry must be a nonempty string")
                sid = posixpath.basename(raw_sid.strip()).removesuffix(".json")
                ws._add_session(sid, f"Simulated session {sid}")
        if args.get("refresh"):
            ws.generation += 1

        doc_id = ws.next_doc_id("trace_open")
        doc: dict[str, Any] = {
            "id": doc_id,
            "operation": "trace_open",
            "workspace": ws.path,
            "title": f"Workspace: {len(ws.sessions)} sessions",
            "counts": {
                "sessions": len(ws.sessions),
                "events": len(ws.events),
                "objects": len(ws.objects),
                "map_entries": len(ws.map_entries),
            },
            "blocks": [],
        }
        counters: dict[str, int] = {}
        doc_refs: list[str] = []

        sorted_cards = sorted(
            ws.map_entries.values(),
            key=lambda c: (not bool(c.get("pinned")), -int(c.get("revision", 1)), str(c["id"])),
        )
        for card in sorted_cards:
            ref = f"entry:{card['id']}"
            doc_refs.append(ref)
            self._add_block(
                ws,
                doc,
                counters,
                "P" if card.get("pinned") else "M",
                f"Map {card['id']} · {card['title']}",
                _card_text(card),
                "pinned" if card.get("pinned") else "map",
                [ref],
            )
        for sid, info in sorted(ws.sessions.items()):
            ev_refs = [ev.ref for ev in ws.events if ev.session == sid]
            obj_refs = [ref for ref, obj in ws.objects.items() if obj["session"] == sid]
            refs = ev_refs + obj_refs
            doc_refs.extend(refs)
            details: dict[str, Any] = {
                "Session": sid,
                "Title": info["title"],
                "Source": info["source"],
                "Events": len(ev_refs),
                "Objects": len(obj_refs),
            }
            if info["parent_id"]:
                details["Parent"] = info["parent_id"]
            self._add_block(
                ws,
                doc,
                counters,
                "S",
                f"Session {sid} · {info['title']}",
                _labeled(details),
                "session",
                refs,
            )
        for rel_id, rel in sorted(ws.map_links.items()):
            self._add_block(
                ws,
                doc,
                counters,
                "M",
                f"Map relation {rel_id}",
                f"{rel['from']} --[{rel['label']}]--> {rel['to']}",
                "map",
                [f"entry:{rel['from']}", f"entry:{rel['to']}"],
            )
        for rname, recipe in sorted(ws.saved_queries.items()):
            self._add_block(
                ws,
                doc,
                counters,
                "Q",
                f"Recipe {rname}",
                _labeled({"Operation": recipe["tool"], "Query": recipe.get("summary", "")}),
                "recipe",
                list(recipe.get("last_refs", [])),
            )
        ws.alias_refs[doc_id] = list(dict.fromkeys(doc_refs))
        return doc

    def _handle_query(self, ws: _SimWorkspace, args: dict[str, Any]) -> dict[str, Any]:
        saved = args.get("saved")
        if saved is not None:
            recipe = ws.saved_queries.get(str(saved))
            if recipe is None or recipe.get("tool") != "trace_query":
                raise ValueError(f"unknown saved query: {saved}")
            sql = recipe["sql"]
        else:
            sql = args.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql is required")
        if _WRITE_SQL_RE.search(sql):
            raise ValueError("trace_query only permits read-only SELECT statements")

        sql_lower = sql.lower()
        rows: list[tuple[dict[str, Any], list[str]]] = []
        if "from sessions" in sql_lower:
            columns = ["id", "title", "parent_id"]
            for sid, info in sorted(ws.sessions.items()):
                rows.append(
                    (
                        {"id": sid, "title": info["title"], "parent_id": info["parent_id"]},
                        [ev.ref for ev in ws.events if ev.session == sid],
                    )
                )
        elif "from map_entries" in sql_lower:
            columns = ["id", "kind", "title", "pinned"]
            for eid, card in sorted(ws.map_entries.items()):
                rows.append(
                    (
                        {
                            "id": eid,
                            "kind": card["kind"],
                            "title": card["title"],
                            "pinned": bool(card.get("pinned")),
                        },
                        [f"entry:{eid}"],
                    )
                )
        else:
            columns = ["ref", "session", "seq", "role", "tool", "kind"]
            for ev in ws.events:
                if "role = 'tool'" in sql_lower and ev.role != "tool":
                    continue
                if "role = 'assistant'" in sql_lower and ev.role != "assistant":
                    continue
                if "role = 'user'" in sql_lower and ev.role != "user":
                    continue
                rows.append(
                    (
                        {
                            "ref": ev.ref,
                            "session": ev.session,
                            "seq": ev.seq,
                            "role": ev.role,
                            "tool": ev.tool,
                            "kind": ev.kind,
                        },
                        [ev.ref],
                    )
                )

        sample = args.get("sample")
        if isinstance(sample, dict) and "n" in sample:
            n = _validate_nonneg_int(sample["n"], "sample.n")
            if n > 0 and len(rows) > n:
                sample_seed = sample.get("seed", 0)
                local_rng = random.Random(int(sample_seed) if isinstance(sample_seed, int) else 0)
                indices = sorted(local_rng.sample(range(len(rows)), n))
                rows = [rows[i] for i in indices]

        doc_id = ws.next_doc_id("trace_query")
        sql_res_id = ws.next_result_id()
        doc: dict[str, Any] = {
            "id": doc_id,
            "operation": "trace_query",
            "workspace": ws.path,
            "title": f"Query: {len(rows)} rows",
            "counts": {"rows": len(rows)},
            "description": "Columns: " + ", ".join(columns),
            "sql_result": sql_res_id,
            "blocks": [],
        }
        counters: dict[str, int] = {}
        all_refs: list[str] = []
        for idx, (row_dict, refs) in enumerate(rows):
            all_refs.extend(refs)
            self._add_block(
                ws,
                doc,
                counters,
                "R",
                f"Row {idx}",
                _labeled(row_dict),
                "row",
                refs,
            )
        dedup_refs = list(dict.fromkeys(all_refs))
        ws.alias_refs[doc_id] = dedup_refs
        ws.alias_refs[sql_res_id] = dedup_refs
        save_as = args.get("save_as")
        if isinstance(save_as, str) and save_as.strip():
            ws.saved_queries[save_as.strip()] = {
                "tool": "trace_query",
                "sql": sql,
                "summary": sql[:80],
                "last_refs": dedup_refs,
            }
        return doc

    def _handle_search(self, ws: _SimWorkspace, args: dict[str, Any]) -> dict[str, Any]:
        saved = args.get("saved")
        if saved is not None:
            recipe = ws.saved_queries.get(str(saved))
            if recipe is None or recipe.get("tool") != "trace_search":
                raise ValueError(f"unknown saved search: {saved}")
            query = recipe["query"]
            mode = recipe.get("mode", "terms")
            on = recipe.get("on", "source")
        else:
            query = args.get("query")
            mode = str(args.get("mode", "terms"))
            on = str(args.get("on", "source"))
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query is required")
        if mode not in {"terms", "literal", "regex"}:
            raise ValueError(f"invalid search mode: {mode}")
        if on not in {"source", "map", "both"}:
            raise ValueError(f"invalid search target: {on}")
        context = _validate_nonneg_int(args.get("context_lines", 2), "context_lines")

        scope = args.get("scope")
        scoped_refs: set[str] | None = None
        if scope is not None:
            scoped_refs = set(self._resolve_refs(ws, scope))

        def _matches(text: str) -> bool:
            if mode == "literal":
                return query.casefold() in text.casefold()
            if mode == "regex":
                try:
                    return re.search(query, text, re.IGNORECASE) is not None
                except re.error as exc:
                    raise ValueError(f"invalid regex: {exc}") from exc
            terms = query.casefold().split()
            lowered = text.casefold()
            return all(term in lowered for term in terms)

        doc_id = ws.next_doc_id("trace_search")
        sql_res_id = ws.next_result_id()
        doc: dict[str, Any] = {
            "id": doc_id,
            "operation": "trace_search",
            "workspace": ws.path,
            "title": "Search: 0 hits",
            "description": f"Mode: {mode} · context: {context} lines",
            "sql_result": sql_res_id,
            "blocks": [],
        }
        counters: dict[str, int] = {}
        hit_refs: list[str] = []

        if on in {"source", "both"}:
            for ev in ws.events:
                if scoped_refs is not None and ev.ref not in scoped_refs:
                    continue
                if _matches(ev.text):
                    hit_refs.append(ev.ref)
                    labels = " / ".join(p for p in (ev.role, ev.tool, ev.kind) if p)
                    self._add_block(
                        ws,
                        doc,
                        counters,
                        "H",
                        f"{ev.ref} · {labels} · text · lines [1, 1]",
                        ev.text,
                        "hit",
                        [ev.ref],
                        anchor={"event": ev.ref, "field": "text", "lines": [1, 1]},
                        match_lines=[1, 1],
                        context_lines=context,
                        excerpt=ev.text[:200],
                    )
        if on in {"map", "both"}:
            for eid, card in sorted(ws.map_entries.items()):
                entry_ref = f"entry:{eid}"
                if scoped_refs is not None and entry_ref not in scoped_refs:
                    continue
                haystack = f"{card['title']}\n{card.get('note', '')}\n{' '.join(card.get('tags', []))}"
                if _matches(haystack):
                    hit_refs.append(entry_ref)
                    self._add_block(
                        ws,
                        doc,
                        counters,
                        "M",
                        f"Map {eid} · note",
                        _card_text(card),
                        "map",
                        [entry_ref],
                    )

        doc["title"] = f"Search: {len(doc['blocks'])} hits"
        doc["counts"] = {"hits": len(doc["blocks"])}
        dedup_refs = list(dict.fromkeys(hit_refs))
        ws.alias_refs[doc_id] = dedup_refs
        ws.alias_refs[sql_res_id] = dedup_refs
        save_as = args.get("save_as")
        if isinstance(save_as, str) and save_as.strip():
            ws.saved_queries[save_as.strip()] = {
                "tool": "trace_search",
                "query": query,
                "mode": mode,
                "on": on,
                "summary": query[:80],
                "last_refs": dedup_refs,
            }
        return doc

    def _handle_read(self, ws: _SimWorkspace, args: dict[str, Any]) -> dict[str, Any]:
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError("targets must be a nonempty list")
        before = _validate_nonneg_int(args.get("before", 0), "before")
        after = _validate_nonneg_int(args.get("after", 0), "after")
        _validate_nonneg_int(args.get("line_context", 10), "line_context")
        pair = args.get("pair", True)
        if not isinstance(pair, bool):
            raise ValueError("pair must be a boolean")

        doc_id = ws.next_doc_id("trace_read")
        doc: dict[str, Any] = {
            "id": doc_id,
            "operation": "trace_read",
            "workspace": ws.path,
            "title": "Read: 0 passages",
            "blocks": [],
        }
        counters: dict[str, int] = {}
        delivered_refs: list[str] = []
        seen_primary: set[str] = set()

        for group_idx, target in enumerate(targets):
            refs = self._resolve_refs(ws, target)
            for ref in refs:
                if ref in seen_primary:
                    continue
                seen_primary.add(ref)
                delivered_refs.append(ref)
                if ref.startswith("entry:"):
                    eid = ref.removeprefix("entry:")
                    card = ws.map_entries.get(eid)
                    if card is None:
                        continue
                    self._add_block(
                        ws,
                        doc,
                        counters,
                        "B",
                        f"Map {eid} · {card['title']}",
                        _card_text(card),
                        "evidence",
                        [ref],
                        requested=True,
                        request_group=group_idx,
                    )
                elif ref in ws.objects:
                    obj = ws.objects[ref]
                    self._add_block(
                        ws,
                        doc,
                        counters,
                        "B",
                        f"{ref} · {obj['kind']}",
                        obj["text"],
                        "evidence",
                        [ref],
                        requested=True,
                        request_group=group_idx,
                    )
                else:
                    ev = ws.event_by_ref(ref)
                    if ev is None:
                        continue
                    labels = " / ".join(p for p in (ev.role, ev.tool, ev.kind) if p)
                    self._add_block(
                        ws,
                        doc,
                        counters,
                        "B",
                        f"{ev.ref} · {labels} · text",
                        ev.text,
                        "evidence",
                        [ev.ref],
                        requested=True,
                        request_group=group_idx,
                    )
                    if before or after:
                        for neighbor in ws.events:
                            if neighbor.session != ev.session or neighbor.ref == ev.ref:
                                continue
                            if ev.seq - before <= neighbor.seq <= ev.seq + after:
                                if neighbor.ref not in seen_primary:
                                    seen_primary.add(neighbor.ref)
                                    self._add_block(
                                        ws,
                                        doc,
                                        counters,
                                        "B",
                                        f"{neighbor.ref} · context",
                                        neighbor.text,
                                        "context",
                                        [neighbor.ref],
                                        requested=False,
                                    )

        doc["title"] = f"Read: {len(doc['blocks'])} passages"
        doc["counts"] = {"passages": len(doc["blocks"]), "relations": 0, "navigation": 0}
        ws.alias_refs[doc_id] = list(dict.fromkeys(delivered_refs))
        return doc

    def _handle_edit(self, ws: _SimWorkspace, args: dict[str, Any]) -> dict[str, list[str]]:
        ack: dict[str, list[str]] = {
            "created": [],
            "updated": [],
            "deleted": [],
            "linked": [],
            "unlinked": [],
        }
        put = args.get("put") or []
        if not isinstance(put, list):
            raise ValueError("put must be a list")
        for patch in put:
            if not isinstance(patch, dict) or not isinstance(patch.get("id"), str) or not patch["id"].strip():
                raise ValueError("each put entry requires a nonempty string id")
            eid = patch["id"].strip()
            existing = ws.map_entries.get(eid)
            anchors = list(existing["anchors"]) if existing else []
            if "anchors" in patch:
                anchors = self._resolve_refs(ws, patch["anchors"])
            if "add_anchors" in patch:
                anchors = list(dict.fromkeys(anchors + self._resolve_refs(ws, patch["add_anchors"])))
            if "remove_anchors" in patch:
                to_drop = set(self._resolve_refs(ws, patch["remove_anchors"]))
                anchors = [a for a in anchors if a not in to_drop]
            entry = {
                "id": eid,
                "kind": str(patch.get("kind", existing["kind"] if existing else "note")),
                "title": str(patch.get("title", existing["title"] if existing else eid)),
                "note": str(patch.get("note", existing["note"] if existing else "")),
                "anchors": anchors,
                "tags": list(patch.get("tags", existing["tags"] if existing else [])),
                "fields": dict(patch.get("fields", existing["fields"] if existing else {})),
                "pinned": bool(patch.get("pinned", existing["pinned"] if existing else False)),
                "revision": (int(existing["revision"]) + 1) if existing else 1,
            }
            ws.map_entries[eid] = entry
            (ack["updated"] if existing else ack["created"]).append(eid)

        delete = args.get("delete") or []
        if not isinstance(delete, list):
            raise ValueError("delete must be a list")
        for eid in delete:
            if not isinstance(eid, str):
                raise ValueError("delete IDs must be strings")
            if eid in ws.map_entries:
                del ws.map_entries[eid]
                ack["deleted"].append(eid)
                for rel_id, rel in list(ws.map_links.items()):
                    if rel["from"] == eid or rel["to"] == eid:
                        del ws.map_links[rel_id]

        links = args.get("link") or []
        if not isinstance(links, list):
            raise ValueError("link must be a list")
        for rel in links:
            if not isinstance(rel, dict):
                raise ValueError("link item must be an object")
            for req_key in ("id", "from", "to", "label"):
                if not isinstance(rel.get(req_key), str) or not rel[req_key].strip():
                    raise ValueError(f"link requires nonempty string {req_key}")
            if rel["from"] not in ws.map_entries or rel["to"] not in ws.map_entries:
                raise ValueError("link endpoints must reference existing map entries")
            ws.map_links[rel["id"]] = {
                "id": rel["id"],
                "from": rel["from"],
                "to": rel["to"],
                "label": rel["label"],
                "note": str(rel.get("note") or ""),
            }
            ack["linked"].append(rel["id"])

        unlink = args.get("unlink") or []
        if not isinstance(unlink, list):
            raise ValueError("unlink must be a list")
        for rel_id in unlink:
            if not isinstance(rel_id, str):
                raise ValueError("unlink IDs must be strings")
            if rel_id in ws.map_links:
                del ws.map_links[rel_id]
                ack["unlinked"].append(rel_id)

        return ack

    def _handle_cooccur(self, ws: _SimWorkspace, args: dict[str, Any]) -> dict[str, Any]:
        saved = args.get("saved")
        if saved is not None:
            recipe = ws.saved_queries.get(str(saved))
            if recipe is None or recipe.get("tool") != "trace_cooccur":
                raise ValueError(f"unknown saved cooccur recipe: {saved}")
            seed_sel = recipe["seed"]
            against = recipe["against"]
        else:
            seed_sel = args.get("seed")
            against = args.get("against")
        if not isinstance(seed_sel, dict):
            raise ValueError("seed selection is required")
        if not isinstance(against, list) or not 1 <= len(against) <= 3:
            raise ValueError("against must be a list of 1-3 conditions")

        seed_refs = self._resolve_refs(ws, seed_sel)
        seed_events = [ev for ref in seed_refs if (ev := ws.event_by_ref(ref)) is not None]
        cond_specs: list[tuple[str, int, int, list[_SimEvent]]] = []
        for cond in against:
            if not isinstance(cond, dict):
                raise ValueError("each against condition must be an object")
            cname = str(cond.get("name") or "").strip()
            if not cname:
                raise ValueError("against condition requires name")
            before = _validate_nonneg_int(cond.get("before", 0), "before")
            after = _validate_nonneg_int(cond.get("after", 0), "after")
            sel_refs = self._resolve_refs(ws, cond.get("selection"))
            sel_events = [ev for ref in sel_refs if (ev := ws.event_by_ref(ref)) is not None]
            cond_specs.append((cname, before, after, sel_events))

        doc_id = ws.next_doc_id("trace_cooccur")
        sql_res_id = ws.next_result_id()
        doc: dict[str, Any] = {
            "id": doc_id,
            "operation": "trace_cooccur",
            "workspace": ws.path,
            "title": "Cooccurrence: 0 matched, 0 unmatched",
            "sql_result": sql_res_id,
            "blocks": [],
        }
        counters: dict[str, int] = {}
        matched_refs: list[str] = []
        unmatched_refs: list[str] = []
        witness_refs: list[str] = []
        witness_blocks: list[dict[str, Any]] = []

        for ev in seed_events:
            all_ok = True
            links: list[dict[str, Any]] = []
            for cname, before, after, targets in cond_specs:
                in_win = [
                    t
                    for t in targets
                    if t.session == ev.session and -before <= (t.seq - ev.seq) <= after
                ]
                if not in_win:
                    all_ok = False
                for w in in_win[:2]:
                    delta = w.seq - ev.seq
                    label = f"{cname} · {delta:+d} · within window [-{before}, +{after}]"
                    w_block = self._add_block(
                        ws,
                        doc,
                        counters,
                        "W",
                        f"{w.ref} · {label}",
                        w.text[:200],
                        "witness",
                        [w.ref],
                        requested=False,
                    )
                    witness_refs.append(w.ref)
                    witness_blocks.append(w_block)
                    links.append({"id": w_block["id"], "label": label, "text": w.text[:160]})
            group = "matched" if all_ok else "unmatched"
            code = "M" if all_ok else "U"
            (matched_refs if all_ok else unmatched_refs).append(ev.ref)
            blk = self._add_block(
                ws,
                doc,
                counters,
                code,
                f"{ev.ref} · seq {ev.seq} · {group}",
                ev.text[:200],
                group,
                [ev.ref],
            )
            blk["links"] = links

        # Keep requested seeds ahead of witness expansions (matches cache._cooccur).
        doc["blocks"] = [b for b in doc["blocks"] if b["group"] != "witness"] + witness_blocks
        doc["title"] = f"Cooccurrence: {len(matched_refs)} matched, {len(unmatched_refs)} unmatched"
        doc["counts"] = {"matched": len(matched_refs), "unmatched": len(unmatched_refs)}
        ws.alias_refs[doc_id] = list(dict.fromkeys(matched_refs or seed_refs))
        ws.alias_refs[sql_res_id] = list(ws.alias_refs[doc_id])
        ws.doc_groups[doc_id] = {
            "matched": list(dict.fromkeys(matched_refs)),
            "unmatched": list(dict.fromkeys(unmatched_refs)),
            "witnesses": list(dict.fromkeys(witness_refs)),
        }
        save_as = args.get("save_as")
        if isinstance(save_as, str) and save_as.strip():
            ws.saved_queries[save_as.strip()] = {
                "tool": "trace_cooccur",
                "seed": seed_sel,
                "against": against,
                "summary": f"{len(cond_specs)} condition(s)",
                "last_refs": list(ws.alias_refs[doc_id]),
            }
        return doc


def _discover_buffer_aliases(state: State) -> tuple[list[str], list[str]]:
    """Inspect live scratch buffers (read-only) to find valid result & item aliases."""
    bm = getattr(state, "buffer_manager", None)
    if bm is None or not callable(getattr(bm, "buffers", None)):
        return [], []
    doc_ids: list[str] = []
    item_ids: list[str] = []
    for bid in bm.buffers():
        buf = bm.get(bid)
        if buf is None:
            continue
        m = _RESULT_PATH_RE.search(str(getattr(buf, "path", "") or ""))
        if not m:
            continue
        doc_id = m.group(1)
        if doc_id not in doc_ids:
            doc_ids.append(doc_id)
        for line in str(getattr(buf, "text", "") or "").splitlines():
            im = _INDEX_ITEM_RE.match(line)
            if im:
                item_ids.append(im.group(1))
    return doc_ids, item_ids


def register_sim_handlers(sim: SimulationRuntime, rng: random.Random) -> None:
    """Register a deterministic in-memory handler for ``PostmortemDispatchRequest``."""
    store = _SimPostmortemStore(rng)
    sim_sess = getattr(sim, "session", None)
    store.hydrate_from_state(getattr(sim_sess, "state", None))

    def handle_postmortem(
        _op_id: Any,
        req: PostmortemDispatchRequest,
        _sim: SimulationRuntime,
    ) -> SimOutcome:
        delay = rng.uniform(0.005, 0.03)
        try:
            result = store.dispatch(req.tool_name, dict(req.arguments))
        except Exception as exc:
            return SimOutcome(error=exc, delay=delay)
        return SimOutcome(value=result, delay=delay)

    sim.register_handler(PostmortemDispatchRequest, handle_postmortem)


def fuzz_tool_args(
    tool_name: str,
    rng: random.Random,
    state: State,
) -> dict[str, Any] | None:
    """Generate realistic, schema-valid arguments for ``trace_*`` tools during DST fuzzing."""
    if tool_name not in _ALL_TRACE_TOOLS:
        return None

    doc_ids, item_ids = _discover_buffer_aliases(state)
    query_or_search_docs = [d for d in doc_ids if d[0] in ("O", "Q", "S", "C")]

    if tool_name == "trace_open":
        ws = rng.choice(_ALT_WORKSPACES)
        args: dict[str, Any] = {"workspace": ws}
        if rng.random() < 0.5:
            args["sessions"] = rng.choice([["sess-root"], ["sess-root", "sess-child"], ["sess-extra"]])
        if rng.random() < 0.4:
            args["descendants"] = rng.choice([True, False])
        if rng.random() < 0.2:
            args["refresh"] = True
        if rng.random() < 0.3:
            args["preview_chars"] = rng.choice([0, 400, 6000])
        return args

    if tool_name == "trace_query":
        sql = rng.choice([
            "SELECT ref, session, seq, role, tool FROM events ORDER BY seq",
            "SELECT ref, session, seq, kind FROM events WHERE role = 'tool'",
            "SELECT ref, session, seq, role FROM events WHERE role = 'assistant'",
            "SELECT id, title, parent_id FROM sessions ORDER BY id",
            "SELECT id, kind, title, pinned FROM map_entries ORDER BY id",
        ])
        args = {"workspace": _DEFAULT_WORKSPACE, "sql": sql}
        if rng.random() < 0.35:
            args["sample"] = {"n": rng.randint(1, 3), "seed": rng.randint(1, 50)}
        if rng.random() < 0.25:
            args["save_as"] = rng.choice(["q_events", "q_tools"])
        if rng.random() < 0.3:
            args["preview_chars"] = rng.choice([500, 6000])
        return args

    if tool_name == "trace_search":
        args = {
            "workspace": _DEFAULT_WORKSPACE,
            "query": rng.choice(["timeout", "retry", "error", "buffer", "checkpoint", "backoff"]),
            "mode": rng.choice(["terms", "literal", "regex"]),
            "on": rng.choice(["source", "source", "map", "both"]),
            "context_lines": rng.choice([0, 1, 2]),
        }
        if rng.random() < 0.35:
            if query_or_search_docs and rng.random() < 0.5:
                args["scope"] = {"result": rng.choice(query_or_search_docs)}
            else:
                args["scope"] = rng.choice([{"workspace": True}, {"sessions": ["sess-root"]}])
        if rng.random() < 0.25:
            args["save_as"] = rng.choice(["s_timeout", "s_retry"])
        if rng.random() < 0.3:
            args["preview_chars"] = rng.choice([600, 6000])
        return args

    if tool_name == "trace_read":
        target_pool: list[str | dict[str, object]] = [
            "e:sess-root:1",
            "e:sess-root:3",
            "e:sess-child:2",
            "o:sess-root:1",
            {"event": "e:sess-root:2", "field": "text"},
            {"entry": "finding-1", "part": "card"},
            {"session": "sess-root", "events": [1, 3]},
        ]
        if item_ids:
            target_pool.extend(item_ids[:4])
        count = rng.randint(1, 2)
        chosen = [rng.choice(target_pool) for _ in range(count)]
        args = {
            "workspace": _DEFAULT_WORKSPACE,
            "targets": chosen,
            "before": rng.choice([0, 1]),
            "after": rng.choice([0, 1]),
            "pair": rng.choice([True, False]),
        }
        if rng.random() < 0.3:
            args["preview_chars"] = rng.choice([800, 12000])
        return args

    if tool_name == "trace_edit":
        entry_id = rng.choice(["note-1", "note-2", "finding-2"])
        args = {
            "workspace": _DEFAULT_WORKSPACE,
            "put": [
                {
                    "id": entry_id,
                    "kind": rng.choice(["finding", "hypothesis", "note"]),
                    "title": f"Simulated {entry_id}",
                    "note": "Recorded deterministic evidence during DST fuzzing.",
                    "anchors": [rng.choice(["e:sess-root:2", "e:sess-root:3", "e:sess-child:3"])],
                    "tags": ["dst", "sim"],
                    "pinned": rng.choice([True, False]),
                }
            ],
        }
        if rng.random() < 0.4:
            args["link"] = [
                {
                    "id": f"rel-{entry_id}",
                    "from": "finding-1",
                    "to": entry_id,
                    "label": rng.choice(["supports", "caused_by", "related"]),
                }
            ]
        return args

    if tool_name == "trace_cooccur":
        seed_result = rng.choice(query_or_search_docs) if query_or_search_docs else "O1"
        against_result = rng.choice(query_or_search_docs) if query_or_search_docs else "O1"
        args = {
            "workspace": _DEFAULT_WORKSPACE,
            "seed": {"result": seed_result},
            "against": [
                {
                    "name": rng.choice(["near_error", "near_retry"]),
                    "selection": {"result": against_result},
                    "before": rng.randint(1, 3),
                    "after": rng.randint(1, 3),
                }
            ],
        }
        if rng.random() < 0.25:
            args["save_as"] = "c_window"
        if rng.random() < 0.3:
            args["preview_chars"] = rng.choice([800, 6000])
        return args

    return None


def check_invariants(session: Session) -> None:
    """Assert session-postmortem invariants after every DST step."""
    state = session.state
    bm = getattr(state, "buffer_manager", None)
    if bm is not None and callable(getattr(bm, "buffers", None)):
        for bid in bm.buffers():
            buf = bm.get(bid)
            if buf is None:
                continue
            path = str(getattr(buf, "path", "") or "")
            if not _RESULT_PATH_RE.search(path):
                continue
            assert getattr(buf, "disk_bound", False) is False, (
                f"postmortem scratch buffer {bid!r} ({path}) must not be disk-bound"
            )
            # Only verify rendered structure when the scratch buffer has not been
            # subsequently edited by built-in tools like str_replace / append_lines.
            text = str(getattr(buf, "text", "") or "")
            if text.startswith("# ") and "\n## INDEX\n" in text:
                seen_ids: set[str] = set()
                for line in text.splitlines():
                    im = _INDEX_ITEM_RE.match(line)
                    if im:
                        item_id = im.group(1)
                        assert item_id not in seen_ids, (
                            f"duplicate postmortem block ID {item_id!r} in buffer {bid!r}"
                        )
                        seen_ids.add(item_id)

    for tc in getattr(state, "pending_tool_calls", None) or ():
        name = getattr(tc, "name", None)
        if getattr(tc, "finished_at", None) is None or getattr(tc, "error", False):
            continue
        res = getattr(tc, "result", None)
        if name in _RETRIEVAL_TOOLS:
            assert isinstance(res, str) and res.startswith("Result ") and " · buffer " in res, (
                f"successful {name} preview must include Result and buffer pointer, got: {res!r:.120}"
            )
        elif name == "trace_edit":
            assert isinstance(res, str) and bool(res.strip()), (
                f"successful trace_edit must return non-empty ack, got: {res!r}"
            )
