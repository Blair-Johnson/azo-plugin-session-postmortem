"""Read-only saved-session adapter for the trace CLI.

Managed sources use canonical JSON documents, never Session restore or pickle.
Only the selected saved state is inspected; forgotten/compressed entries remain
included. Object text is saved text, never a read from a historical pathname.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from pathlib import Path
import re
from typing import Iterable


class SourceError(ValueError):
    """A source cannot be resolved or safely interpreted."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _plain(value):
    """Unwrap JSON envelopes only; do not import classes or decode pickles."""
    while isinstance(value, dict):
        if "__component__" in value:
            value = value.get("__state__", {})
        elif "__dataclass__" in value:
            value = value.get("__fields__", {})
        else:
            break
    return value


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _text(value["content"])
    return _json(value)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"Cannot read JSON source {path}: {exc}") from exc


def _root():
    # Import lazily: portable fixtures work with stdlib alone.
    try:
        from agent_zoo.storage.projects import resolve_state_root
        return resolve_state_root(create=False) / "projects"
    except ImportError:
        home = os.environ.get("AGENT_ZOO_HOME") or os.environ.get("AGENT_ZOO_STATE_ROOT")
        return Path(home or "~/.local/share/agent-zoo").expanduser().resolve() / "projects"


def _catalog(sessions_dir):
    path = sessions_dir / "index.json"
    if not path.is_file():
        return []
    rows = _read_json(path)
    if not isinstance(rows, list):
        raise SourceError(f"Invalid session catalog: {path}")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("session_id"), str)]


def _resolve(source):
    path = Path(source).expanduser().absolute()
    if path.is_dir():
        return path / "session.json"
    if path.is_file() or (path.name == "session.json" and path.parent.is_dir()):
        return path
    if "/" in source or "\\" in source or source.endswith(".json"):
        raise SourceError(f"Source unavailable: {source}")
    matches = set()
    root = _root()
    # Directory names plus catalog entries find hidden and local-only sessions.
    for sessions in sorted(root.glob("*/sessions")):
        for candidate in sessions.iterdir():
            if candidate.is_dir() and candidate.name.startswith(source):
                matches.add(candidate / "session.json")
        for row in _catalog(sessions):
            if row["session_id"].startswith(source):
                matches.add(sessions / row["session_id"] / "session.json")
    if not matches:
        raise SourceError(f"Session ID prefix unavailable: {source}")
    if len(matches) != 1:
        raise SourceError(f"Ambiguous session ID prefix {source!r}: " + ", ".join(map(str, sorted(matches))))
    return matches.pop()


def _managed(path):
    directory = path.parent
    # A managed loose session.json is never authoritative, even if it exists.
    return path.name == "session.json" and (
        directory.parent.name == "sessions" and directory.parent.parent.parent == _root()
        or any((directory / name).exists() for name in ("journals", "revisions", "selection.json", "preferred.json"))
        or not path.is_file()
    )


def _load(path):
    if not _managed(path):
        doc = _read_json(path)
        if not isinstance(doc, dict):
            raise SourceError(f"Session JSON must be an object: {path}")
        return doc, [], {}
    try:
        from agent_zoo.storage.load import load_session
        loaded = load_session(path, session_id=path.parent.name, reconcile_shared=True)
        state = loaded.state
        provenance = dict(commit_id=state.commit_id, instance_id=state.instance_id,
                          source_format=state.source_format, freshness=loaded.freshness)
        return state.document, [loaded.warning] if loaded.warning else [], provenance
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise SourceError(f"Cannot load managed session {path}: {exc}") from exc


def _identity(doc, path):
    features = doc.get("features") if isinstance(doc.get("features"), dict) else {}
    fsess = features.get("session") if isinstance(features.get("session"), dict) else {}
    if "__durable_v__" in fsess and isinstance(fsess.get("__data__"), dict):
        fsess = fsess["__data__"]
    attrs = doc.get("attrs") or {}
    live = attrs.get("_live_metadata") or {}
    meta = doc.get("meta") or {}
    sid = (doc.get("id") or doc.get("session_id") or meta.get("session_id") or
           fsess.get("session_id") or attrs.get("_session_id"))
    if not isinstance(sid, str) or not sid:
        raise SourceError(f"Source has no session identity: {path}")
    parent = (doc.get("parent_id") or doc.get("parent_session_id") or
              fsess.get("parent_session_id") or
              attrs.get("_parent_session_id") or attrs.get("_rlm_parent_session_id") or
              live.get("parent_session_id") or meta.get("parent_session_id"))
    title = (doc.get("title") or fsess.get("title") or
             attrs.get("_session_title") or live.get("title") or meta.get("title") or sid)
    return sid, parent or None, str(title)


def _extract_rlm_jobs(doc):
    """Extract normalized RLM job metadata from ``attrs['rlm_jobs']`` without unpickling."""
    attrs = doc.get("attrs") if isinstance(doc.get("attrs"), dict) else {}
    raw_jobs = _plain(attrs.get("rlm_jobs"))
    if not isinstance(raw_jobs, dict):
        return {}
    extracted = {}
    for key, raw in raw_jobs.items():
        if not isinstance(key, str) or not key:
            continue
        item = _plain(raw)
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id") or key).strip()
        if not job_id:
            continue
        meta = _plain(item.get("metadata"))
        meta = meta if isinstance(meta, dict) else {}
        child = str(item.get("child_session_id") or meta.get("child_session_id") or "").strip()
        raw_outputs = _plain(item.get("output_map"))
        if not isinstance(raw_outputs, dict):
            raw_outputs = _plain(meta.get("parent_output_map"))
        output_map = {}
        if isinstance(raw_outputs, dict):
            for out_name, handle in raw_outputs.items():
                if isinstance(out_name, str) and out_name and isinstance(handle, str) and handle:
                    output_map[out_name] = handle
        if child or output_map:
            extracted[job_id] = {"child_session_id": child, "output_map": output_map}
    return extracted


def _children(doc, path, sid):
    """Discover declared children and catalog descendants, not all documents."""
    found = set()
    if _managed(path):
        for row in _catalog(path.parent.parent):
            if row.get("parent_session_id") == sid:
                found.add(str(path.parent.parent / row["session_id"] / "session.json"))
    for value in doc.get("children", doc.get("child_sessions", [])) or []:
        if isinstance(value, dict):
            value = value.get("source") or value.get("path") or value.get("session_id") or value.get("id")
        if isinstance(value, str) and value:
            found.add(value)
    # Saved RLM records often retain child identity even after catalog removal.
    def walk(value):
        if isinstance(value, dict):
            child = value.get("child_session_id")
            if isinstance(child, str) and child and child != sid:
                found.add(child)
            for k, v in value.items():
                if not k.startswith("__pickle"):
                    walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk((doc.get("attrs") or {}).get("rlm_jobs", {}))
    for link in doc.get("trace_links", []):
        if link.get("kind") == "child" and link.get("src", "").split("/")[0] == sid:
            found.add(link["dst"].split("/")[0])
    result = []
    for child in sorted(found):
        candidate = path.parent / child
        if candidate.exists():
            result.append(str(candidate))
        elif (path.parent / (child + ".json")).is_file():
            result.append(str(path.parent / (child + ".json")))
        elif (path.parent.parent / child).is_dir():
            result.append(str(path.parent.parent / child))
        else:
            result.append(child)
    return result


def _arguments(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            pass
    return None


def _events(doc, sid):
    events, links = [], []
    pending = {}

    def emit(kind, role, text, fields, clock=None, tool=None, arguments=None):
        seq = len(events) + 1
        ns = clock.get("occurred_at_ns") if isinstance(clock, dict) else None
        event = dict(ref=f"{sid}/e{seq}", session=sid, seq=seq, elapsed_s=None,
                     kind=kind, role=role, tool=tool, arguments=arguments,
                     text=_text(text), fields={k: v if isinstance(v, str) else _json(v)
                                              for k, v in fields.items()}, _ns=ns)
        events.append(event)
        return event

    # Portable already-normalized events are useful for tiny contract fixtures.
    if "events" in doc and "entries" not in doc:
        for raw in doc["events"]:
            event = emit(raw.get("kind", "message"), raw.get("role", "assistant"),
                         raw.get("text", ""), raw.get("fields", {}),
                         tool=raw.get("tool"), arguments=raw.get("arguments"))
            event["elapsed_s"] = raw.get("elapsed_s")
            event.pop("_ns")
        return events, links

    for position, entry in enumerate(doc.get("entries", [])):
        entry = _plain(entry)
        if not isinstance(entry, dict):
            continue
        clock = entry.get("event_time")
        fields = {"source_pointer": f"/entries/{position}",
                  "entry_index": str(entry.get("index", position)),
                  "step": str(entry.get("step", ""))}
        for key in ("forgotten", "compressed", "system_generated", "metadata"):
            if key in entry:
                fields[key] = _json(entry[key])
        if clock:
            fields["timestamp_origin"] = "entry (inherited)"
        structured = [_plain(c) for c in entry.get("tool_calls", []) or []]
        structured = [c for c in structured if isinstance(c, dict)]
        by_id = {c["id"]: c for c in structured if c.get("id")}
        local = {}

        def call(raw, key=None):
            raw = _plain(raw)
            cid = raw.get("id") or raw.get("call_id") or key or f"anonymous-{len(local)}"
            if cid in local:
                return local[cid]
            canonical = by_id.get(cid, raw)
            function = canonical.get("function") or {}
            name = canonical.get("name") or function.get("name") or raw.get("name")
            arg = canonical.get("parsed_args")
            if arg is None:
                arg = canonical.get("raw_args", canonical.get("arguments", canonical.get("input", function.get("arguments"))))
            extra = dict(fields, call_id=str(cid))
            if isinstance(arg, str):
                extra["raw_args"] = arg
            at = canonical.get("started_at") or canonical.get("event_time")
            if at:
                extra["timestamp_origin"] = "tool"
            event = emit("call", "assistant", name or "", extra, at or clock,
                         tool=name, arguments=_arguments(arg))
            record = dict(event=event, raw=canonical, result=None)
            local[cid] = pending[cid] = record
            return record

        def result(cid, value, raw=None):
            record = local.get(cid) or pending.get(cid)
            if record is None and cid in by_id:
                record = call(by_id[cid])
            if record and record["result"] is not None:
                return
            canonical = record["raw"] if record else (raw or {})
            if canonical.get("result") is not None:
                value = canonical["result"]
            extra = dict(fields, call_id=str(cid))
            if "error" in canonical:
                extra["error"] = _json(canonical["error"])
            at = canonical.get("finished_at")
            if at:
                extra["timestamp_origin"] = "tool"
            event = emit("result", "tool", value, extra, at or clock,
                         tool=record["event"]["tool"] if record else (raw or {}).get("name"))
            if record:
                record["result"] = event
                links.append(dict(src=record["event"]["ref"], dst=event["ref"], kind="call_result"))
            else:
                event["fields"]["unpaired_result"] = "true"

        for message in entry.get("messages", []):
            if not isinstance(message, dict):
                continue
            role = message.get("role", "assistant")
            if role in ("tool", "function") or message.get("type") == "function_call_output":
                cid = message.get("tool_call_id") or message.get("call_id") or message.get("name", "unknown")
                result(cid, message.get("content", message.get("output", "")), message)
                continue
            content = message.get("content", "")
            parts = content if isinstance(content, list) else [content]
            prose = []
            extra = dict(fields)
            for key, value in message.items():
                if key not in ("role", "content", "tool_calls", "function_call"):
                    extra[key] = value if isinstance(value, str) else _json(value)

            def flush():
                if prose:
                    text = "\n".join(prose)
                    if text or any(k in extra for k in ("reasoning_content", "reasoning")):
                        emit("context" if role == "system" else "message", role, text, extra, clock)
                    prose.clear()

            for part in parts:
                if isinstance(part, dict) and part.get("type") in ("tool_use", "function_call"):
                    flush()
                    call(part)
                elif isinstance(part, dict) and part.get("type") in ("tool_result", "function_call_output"):
                    flush()
                    result(part.get("tool_use_id") or part.get("call_id"), part.get("content", part.get("output", "")), part)
                else:
                    prose.append(_text(part))
            flush()
            for raw in message.get("tool_calls", []) or []:
                call(raw)
            if message.get("function_call"):
                call(message["function_call"])
        for index, raw in enumerate(structured):
            record = call(raw, key=f"structured-{index}")
            if raw.get("result") is not None and record["result"] is None:
                result(raw.get("id") or f"structured-{index}", raw["result"])
        addendum = entry.get("mode_user_message_addendum") or entry.get("mode_user_message_prefix")
        if addendum:
            emit("context", entry.get("mode_user_message_addendum_role", "system"), addendum,
                 dict(fields, context_kind="mode_addendum"), clock)
        # Structured blocks can contain useful output absent from result text.
        for cid, blocks in (entry.get("blocks") or {}).items():
            record = local.get(cid) or pending.get(cid)
            if record:
                target = record["result"] or record["event"]
                target["fields"]["blocks"] = _text(blocks)
    known_times = [e["_ns"] for e in events if isinstance(e["_ns"], (int, float))]
    origin = min(known_times) if known_times else None
    for event in events:
        ns = event.pop("_ns")
        if origin is not None and isinstance(ns, (int, float)):
            event["elapsed_s"] = (ns - origin) / 1_000_000_000
    return events, links


_HANDLE = re.compile(r"\b(?:buf|span|ses|att)_[A-Za-z0-9_]+\b")


def _objects(doc, sid, events, links):
    objects, aliases = {}, {}

    def add(handle, kind, text=None, reason=None, ref=None):
        ref = ref or f"{sid}/o_{handle}"
        item = dict(ref=ref, session=sid, kind=kind, text=text,
                    available=isinstance(text, str))
        if text is None:
            item["reason"] = reason or "Content not present in selected saved document"
        objects[ref] = item
        aliases[str(handle)] = ref
        return ref

    for raw in doc.get("trace_objects", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("ref"), str):
            raise SourceError("Each trace_objects item needs a string ref")
        ref = raw["ref"]
        text = raw.get("text") if raw.get("available", raw.get("text") is not None) else None
        if text is not None and not isinstance(text, str):
            raise SourceError(f"Object text must be a string or null: {ref}")
        add(ref.rsplit("/", 1)[-1], raw.get("kind", "artifact"), text,
            raw.get("reason"), ref=ref)

    components = doc.get("components") or {}
    attrs = doc.get("attrs") or {}
    manager = _plain(components.get("buffer_manager", attrs.get("buffer_manager", {})))
    opaque_buffers = isinstance(manager, dict) and any(k in manager for k in ("__pickle__", "__pickle_inline__"))
    if isinstance(manager, dict):
        if opaque_buffers:
            add("buffer_manager", "buffer_store", reason="Opaque serialized buffer manager; not unpickled")
        for handle, encoded in (manager.get("buffers") or {}).items():
            value = _plain(encoded)
            if not isinstance(value, dict):
                continue
            text = value.get("text") if value.get("allocated", True) else None
            text = text if isinstance(text, str) else None
            ref = add(handle, "buffer", text, "Buffer unallocated or opaque in saved state; not unpickled")
            if isinstance(value.get("initial_text"), str) and value["initial_text"] != text:
                initial = add(handle + "_initial", "buffer_snapshot", value["initial_text"])
                links.append(dict(src=ref, dst=initial, kind="references"))
            for label, snapshot in (value.get("checkpoints") or {}).items():
                snapshot = _plain(snapshot)
                if isinstance(snapshot, dict) and isinstance(snapshot.get("text"), str):
                    suffix = hashlib.sha256(str(label).encode()).hexdigest()[:12]
                    checkpoint = add(handle + "_checkpoint_" + suffix, "buffer_snapshot", snapshot["text"])
                    links.append(dict(src=ref, dst=checkpoint, kind="references"))

    # JSON-stored text artifacts are safe; binary attachment payloads remain opaque.
    for container in (doc, attrs, components):
        artifacts = _plain(container.get("artifacts", {}))
        pairs = artifacts.items() if isinstance(artifacts, dict) else enumerate(artifacts or [])
        for name, raw in pairs:
            value = _plain(raw)
            handle = str(value.get("ref") or value.get("id") or name) if isinstance(value, dict) else str(name)
            text = value.get("text", value.get("content")) if isinstance(value, dict) else value
            add("artifact_" + handle, "artifact", text if isinstance(text, str) else None)
    attachments = _plain(components.get("attachment_store", attrs.get("attachment_store", {})))
    if isinstance(attachments, dict):
        for handle, raw in (attachments.get("blobs") or {}).items():
            value = _plain(raw)
            text = value.get("text") if isinstance(value, dict) else None
            add(handle, "attachment", text if isinstance(text, str) else None,
                "Binary/opaque attachment has no safely stored text; not unpickled")
        for event in events:
            index = event["fields"].get("entry_index")
            cid = event["fields"].get("call_id")
            attached = ((attachments.get("tool_links") or {}).get(f"{index}:{cid}", []) if cid else
                        (attachments.get("user_links") or {}).get(str(index), []))
            for raw in attached:
                for handle in _HANDLE.findall(_json(raw)):
                    ref = aliases.get(handle) or add(handle, "attachment")
                    links.append(dict(src=event["ref"], dst=ref, kind="references"))

    for event in events:
        searchable = "\n".join([event["text"], _json(event["arguments"]), _json(event["fields"])])
        for handle in sorted(set(_HANDLE.findall(searchable))):
            reason = ("Unresolved buffer: saved buffer manager is opaque and unsupported; see docs/api.md"
                      if opaque_buffers and handle.startswith("buf_") else None)
            ref = aliases.get(handle) or add(handle, "buffer" if handle.startswith("buf_") else "reference", reason=reason)
            links.append(dict(src=event["ref"], dst=ref, kind="references"))
        args = event.get("arguments") or {}
        if event["kind"] == "call" and isinstance(args.get("path"), str) and args["path"]:
            path = args["path"]
            # A write call contains historical proposed content, not proof of a successful write.
            if (event.get("tool") or "").rsplit(".", 1)[-1] == "write_file" and isinstance(args.get("content"), str):
                ref = add("write_" + str(event["seq"]), "write_input", args["content"])
            else:
                suffix = hashlib.sha256(path.encode()).hexdigest()[:16]
                ref = aliases.get("file_" + suffix) or add("file_" + suffix, "file", reason=
                    f"Historical file content unavailable: {path}; current filesystem was not read")
            links.append(dict(src=event["ref"], dst=ref, kind="references"))
    for item in list(objects.values()):
        for handle in sorted(set(_HANDLE.findall(item["text"] or ""))):
            ref = aliases.get(handle) or add(handle, "reference")
            if ref != item["ref"]:
                links.append(dict(src=item["ref"], dst=ref, kind="references"))
    return list(objects.values())


def _normalize(doc, path, warnings, provenance):
    for key in ("meta", "attrs", "components", "features"):
        if doc.get(key) is not None and not isinstance(doc[key], dict):
            raise SourceError(f"Session {key} must be an object: {path}")
    for key in ("entries", "events", "trace_objects", "trace_links"):
        if key in doc and not isinstance(doc[key], list):
            raise SourceError(f"Session {key} must be a list: {path}")
    if doc.get("format_version", 2) not in (1, 2):
        raise SourceError(f"Unsupported session document format: {doc.get('format_version')}")
    sid, parent, title = _identity(doc, path)
    if _managed(path):
        row = next((r for r in _catalog(path.parent.parent) if r["session_id"] == sid), {})
        parent = parent or row.get("parent_session_id") or None
        if title == sid:
            title = row.get("title") or sid
    events, links = _events(doc, sid)
    for link in doc.get("trace_links", []):
        if not isinstance(link, dict) or not all(isinstance(link.get(k), str) for k in ("src", "dst", "kind")):
            raise SourceError("trace_links items require string src, dst, kind")
        if link["kind"] not in ("call_result", "references", "child"):
            raise SourceError(f"Unsupported link kind: {link['kind']}")
        links.append({k: link[k] for k in ("src", "dst", "kind")})
    objects = _objects(doc, sid, events, links)
    known = {item["ref"] for item in events + objects}
    for link in links:
        for ref in (link["src"], link["dst"]):
            if ref.startswith(sid + "/") and ref not in known:
                objects.append(dict(ref=ref, session=sid, kind="reference", text=None,
                                    available=False, reason="Referenced endpoint absent from selected saved document"))
                known.add(ref)
    if parent:
        links.append(dict(src=parent, dst=sid, kind="child"))
    result = dict(id=sid, parent_id=parent, title=title, source=str(path),
                  events=events, objects=objects, links=links, warnings=list(warnings))
    rlm_jobs = _extract_rlm_jobs(doc)
    if rlm_jobs:
        result["rlm_jobs"] = rlm_jobs
    if provenance:
        result["provenance"] = provenance
    return result


def load_sources(sources: list[str], descendants: bool = True) -> Iterable[dict]:
    """Yield normalized saved sessions; explicit failures raise SourceError.

    Missing discovered children instead produce warnings and unavailable objects.
    Descendant discovery follows catalog relationships and saved child references;
    it does not scan every historical document or external coordination store.
    """
    if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or not s.strip() for s in sources):
        raise SourceError("sources must be a nonempty list of paths or session ID prefixes")
    if type(descendants) is not bool:
        raise SourceError("descendants must be a boolean")
    queue = deque((s, None) for s in sources)
    seen_paths, sessions = {}, {}
    while queue:
        source, discovered_by = queue.popleft()
        try:
            path = _resolve(source)
            if path in seen_paths:
                if discovered_by:
                    sessions[discovered_by]["links"].append(dict(src=discovered_by, dst=seen_paths[path], kind="child"))
                continue
            doc, warnings, provenance = _load(path)
            session = _normalize(doc, path, warnings, provenance)
            seen_paths[path] = session["id"]
        except (SourceError, OSError, TypeError, KeyError) as exc:
            if discovered_by is None:
                raise SourceError(f"Cannot open source {source!r}: {exc}") from exc
            owner = sessions[discovered_by]
            owner["warnings"].append(f"Child source unavailable: {source}: {exc}")
            gap = Path(source).parent.name if source.endswith("session.json") else Path(source).stem
            owner["objects"].append(dict(ref=gap, session=discovered_by, kind="session", text=None,
                                          available=False, reason=str(exc)))
            owner["links"].append(dict(src=discovered_by, dst=gap, kind="child"))
            continue
        sid = session["id"]
        if sid in sessions:
            previous = sessions[sid]
            if previous["events"] != session["events"] or previous["objects"] != session["objects"]:
                raise SourceError(f"Conflicting sources for session {sid}; choose one explicit source")
            continue
        sessions[sid] = session
        if discovered_by:
            sessions[discovered_by]["links"].append(dict(src=discovered_by, dst=sid, kind="child"))
        if descendants:
            queue.extend((child, sid) for child in _children(doc, path, sid))
    for session in sessions.values():
        triples = {(link["src"], link["dst"], link["kind"]) for link in session["links"]}
        session["links"] = [dict(src=s, dst=d, kind=k) for s, d, k in sorted(triples)]
        yield session
