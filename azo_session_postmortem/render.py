"""Concise native text views of engine results, without I/O or host dependencies.

Source bodies and hit excerpts are opaque strings: never wrap, normalize, escape,
or shorten them. Handles are result IDs plus zero-based positional row numbers;
a SQL column named ``row`` is payload, not that position.
"""


def _handle(result, row):
    return f"{result} row {row}"


def _anchor(value, short=False):
    if isinstance(value, str):
        if short and "/" in value:
            session, suffix = value.split("/", 1)
            return session[:8] + ("…" if len(session) > 8 else "") + "/" + suffix
        return value
    if not isinstance(value, dict):
        return _value(value)
    if "anchor" in value:
        return _anchor(value["anchor"], short)
    if "event" in value or "object" in value:
        ref = value.get("event", value.get("object"))
        label = _anchor(ref, short)
        if value.get("field") not in (None, "text"):
            label += "/" + str(value["field"])
        if value.get("lines"):
            label += " L" + "–".join(map(str, value["lines"]))
        return label
    if "entry" in value:
        return "map " + str(value["entry"]) + (" anchors" if value.get("part") == "anchors" else "")
    if "session" in value:
        label = "session " + str(value["session"])
        if value.get("events"):
            label += " events " + "–".join(map(str, value["events"]))
        return label
    if "result" in value:
        label = str(value["result"])
        if "rows" in value:
            label += " rows " + _value(value["rows"])
        return label
    return _value(value)


def _value(value):
    """Readable nested query/authored values, preserving explicit nulls and empties."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value if value else '""'
    if isinstance(value, dict):
        anchor_keys = {"event", "object", "entry", "session", "result", "field", "lines", "events", "rows", "part"}
        if value and set(value) <= anchor_keys and set(value) & {"event", "object", "entry", "session", "result"}:
            return _anchor(value)
        return "(" + "; ".join(f"{key}={_value(item)}" for key, item in value.items()) + ")"
    if isinstance(value, (list, tuple)):
        return "[" + "; ".join(_value(item) for item in value) + "]"
    return str(value)


def _cursor(result, key, operation, label="Next"):
    token = result.get(key)
    return f"{label}: {operation} cursor={token}" if token else ""


def _join(parts):
    # Do not strip: the final part may be exact source text with significant space.
    return "\n\n".join(part for part in parts if part != "")


def _notice(item):
    if not isinstance(item, dict):
        return str(item)
    return _anchor(item.get("anchor", item.get("ref", "content"))) + ": " + str(item.get("reason") or "content unavailable")


def _clipped(row):
    return "(preview clipped)" if row.get("_display_truncated") else ""


def _source_warnings(source):
    notes = []
    for warning in source.get("warnings") or []:
        warning = str(warning)
        if warning.startswith("Child source unavailable: "):
            notes.append("missing child " + warning.removeprefix("Child source unavailable: ").split(": ", 1)[0])
        elif warning.lower().startswith("missing child "):
            notes.append(warning)
    return " (" + "; ".join(dict.fromkeys(notes)) + ")" if notes else ""


def _open(result):
    parts = ["Workspace " + str(result["workspace"])] if result.get("workspace") else []
    sources = result.get("sources") or []
    if sources:
        parts.append("Sessions\n" + "\n".join(
            str(source["id"]) + (" — " + str(source["title"]) if source.get("title") else "")
            + _source_warnings(source)
            for source in sources))
    seen = set()
    for key, label in (("pinned_entries", "Pinned"), ("recent_entries", "Recent")):
        cards = []
        for card in result.get(key) or []:
            if card["id"] in seen:
                continue
            seen.add(card["id"])
            cards.append(str(card["id"]) + " — " + str(card.get("title", ""))
                         + ("\n" + card["note"] if card.get("note") else ""))
        if cards:
            parts.append(label + "\n" + "\n".join(cards))
    recipes = result.get("saved_queries") or []
    if recipes:
        parts.append("Saved: " + "; ".join(str(row["name"]) + (" → " + str(row["last_result"]) if row.get("last_result") else "") for row in recipes))
    if not sources and not seen and not recipes:
        parts.append("No catalog entries on this page.")
    parts.append(_cursor(result, "next_cursor", "trace_open"))
    return _join(parts)


def _search(result):
    rid = result.get("result", "result")
    totals = result.get("matched_total")
    hits = result.get("hits") or {}
    rows = list(hits.get("source") or []) + list(hits.get("map") or [])
    if isinstance(totals, dict):
        count = sum(value for value in totals.values() if isinstance(value, int))
    else:
        count = totals
    heading = f"{rid} — {count} raw matches" if count is not None else f"{rid} — raw match count unknown"
    heading += f"; {len(rows)} displayed " + ("groups" if any(row.get("occurrence_count", 1) > 1 for row in rows) else "hits")
    parts = [heading]
    if not rows:
        parts.append("No hits on this page.")
    for position, row in enumerate(rows, result.get("page_offset", 0)):
        handle = _handle(rid, row.get("row", position))
        location = _anchor(row["anchor"], True) if row.get("anchor") else "map " + str(row.get("entry_id", ""))
        header = f"[{handle}] {location}"
        if row.get("occurrence_count", 0) > 1:
            header += f"; {row['occurrence_count']} occurrences"
            if row.get("occurrences_result"):
                header += f" → {row['occurrences_result']}"
        parts.append(header + "\n" + row.get("excerpt", ""))
        parts.append(_clipped(row))
    parts.append(_cursor(result, "next_cursor", "trace_search"))
    return _join(parts)


def _condition(condition, complete=False):
    if complete:
        # A query explicitly selects these nested values: retain even nulls,
        # empty witness lists, attribution and unfamiliar future columns.
        return _value(condition)
    name = str(condition.get("name", "condition"))
    status = str(condition.get("status", "unknown")).replace("_", " ")
    text = name + ": " + status
    if "before" in condition and "after" in condition:
        text += f" (window −{condition['before']}/+{condition['after']})"
    count = condition.get("witness_count")
    rid = condition.get("witnesses_result")
    if count is not None:
        text += f"; {count} witnesses"
    if rid:
        text += " → " + str(rid)
    for position, witness in enumerate(condition.get("witnesses") or []):
        label = _handle(rid, witness.get("row", position)) if rid else "witness"
        text += "\n    [" + label + "] " + _anchor(witness.get("anchor", witness.get("ref")))
        if witness.get("delta_seq") is not None:
            text += f"; distance {witness['delta_seq']:+d}"
    return text


def _query(result):
    rid = result.get("result", "result")
    rows = result.get("rows") or []
    parts = [f"{rid} — {result.get('row_count', len(rows))} rows; {len(rows)} displayed"]
    columns = result.get("columns")
    for position, row in enumerate(rows, result.get("page_offset", 0)):
        handle = _handle(rid, position)
        if result.get("presentation") == "cooccur":
            header = f"[{handle}] " + _anchor(row.get("anchor", row.get("ref")))
            if "conditions" not in row:
                if row.get("delta_seq") is not None:
                    header += f"; distance {row['delta_seq']:+d}"
                labels = [str(row[key]) for key in ("role", "kind", "tool") if row.get(key)
                          and not (key == "kind" and row[key] == "message" and row.get("role"))]
                if labels:
                    header += " · " + " · ".join(dict.fromkeys(labels))
            parts.append(header)
            parts.extend(_condition(condition) for condition in row.get("conditions") or [])
            parts.append(_clipped(row))
            continue
        keys = columns if columns is not None else [key for key in row if key != "_display_truncated"]
        cells = []
        for key in keys:
            value = row.get(key)
            if key == "conditions" and isinstance(value, list):
                cells.append("conditions: " + ("\n  " + "\n  ".join(_condition(item, complete=True) for item in value) if value else "[]"))
            elif key == "delta_seq" and isinstance(value, int):
                cells.append(f"{key}={value:+d}")
            else:
                cells.append(str(key) + "=" + _value(value))
        parts.append(f"[{handle}] " + "; ".join(cells))
        parts.append(_clipped(row))
    if not rows:
        parts.append("No rows on this page.")
        if columns:
            parts.append("Columns: " + ", ".join(columns))
    parts.append(_cursor(result, "next_cursor", "trace_query"))
    return _join(parts)


def _card(card):
    parts = ["Map " + str(card.get("id", "")) + " — " + str(card.get("title", ""))]
    if card.get("note"):
        parts.append(card["note"])
    if card.get("tags"):
        parts.append("Tags: " + ", ".join(card["tags"]))
    if card.get("fields"):
        parts.append("Fields: " + "; ".join(str(key) + "=" + _value(value) for key, value in card["fields"].items()))
    if card.get("anchors"):
        parts.append("Anchors: " + "; ".join(_anchor(anchor) for anchor in card["anchors"]))
    if card.get("details_path"):
        parts.append("Details: " + str(card["details_path"]))
    for link in card.get("links") or []:
        parts.append(_link(link))
    return _join(parts)


def _link(link):
    text = str(link.get("src", link.get("from", ""))) + " — " + str(link.get("label", link.get("kind", "link"))) + " → " + str(link.get("dst", link.get("to", "")))
    if link.get("id"):
        text = str(link["id"]) + ": " + text
    if link.get("note"):
        text += "\n" + str(link["note"])
    return text


def _navigation(row):
    text = "Child " + str(row.get("dst", "?"))
    if row.get("title"):
        text += " — " + str(row["title"])
    if row.get("output_name"):
        text += "; output " + str(row["output_name"])
    qualifiers = []
    if row.get("indexed") is False:
        qualifiers.append("not indexed")
    if row.get("ambiguous"):
        qualifiers.append("ambiguous association")
    if row.get("parent_status") == "mismatch":
        qualifiers.append(f"conflicting parent: recorded {row.get('recorded_parent_id')}, observed {row.get('observed_session')}")
    if qualifiers:
        text += " (" + "; ".join(qualifiers) + ")"
    if row.get("anchor"):
        text += "\n  Launch: " + _anchor(row["anchor"])
    return text


def _inspect(result):
    rid = result["plan_result"]
    parts = [f"Plan {rid} — {result.get('total_rows', len(result.get('rows') or []))} blocks; "
             f"{result.get('known_text_chars', 0)} known chars; {result.get('predicted_text_chars', 0)} chars on next read"]
    rows = result.get("rows") or []
    if rows:
        lines = ["Handle | Location | Chars → delivered | Status"]
        for position, row in enumerate(rows):
            status = "unavailable" if row.get("available") is False else "requested" if row.get("requested", True) else "context"
            lines.append(f"{_handle(rid, row.get('row', position))} | {_anchor(row.get('anchor'))} | "
                         f"{row.get('text_chars') if row.get('text_chars') is not None else '—'} → "
                         f"{row.get('would_deliver_chars') if row.get('would_deliver_chars') is not None else '—'} | {status}")
        parts.append("\n".join(lines))
    for key, label in (("unavailable_rows", "unavailable"), ("partially_delivered_rows", "partial"), ("fully_deferred_rows", "deferred")):
        if result.get(key):
            parts.append(f"{result[key]} {label} blocks.")
    parts.append(_cursor(result, "plan_cursor", "trace_query", "More plan rows"))
    return _join(parts)


def _read(result):
    if "plan_result" in result:
        return _inspect(result)
    if result.get("buffer"):
        return _join(["Buffer " + str(result["buffer"]), _cursor(result, "next_cursor", "trace_read")])
    parts = []
    unavailable_seen = set()
    delivered = result.get("delivered") or []
    refs = set()
    for position, block in enumerate(result.get("blocks") or []):
        if "card" in block:
            parts.append(_card(block["card"]))
            parts.extend(_link(link) for link in block.get("links") or [])
            continue
        legacy = delivered[position] if position < len(delivered) else {}
        anchor = block.get("anchor", legacy.get("anchor", block.get("ref")))
        if anchor is None and block.get("entry_id"):
            anchor = {"entry": block["entry_id"]}
        ref = anchor.get("event", anchor.get("object")) if isinstance(anchor, dict) else anchor
        if ref:
            refs.add(ref)
        metadata = (result.get("sources") or {}).get(ref, block)
        labels = [_anchor(anchor) if anchor is not None else "Source"]
        for key in ("role", "kind", "tool"):
            if key == "kind" and metadata.get(key) == "message" and metadata.get("role"):
                continue
            if metadata.get(key) and str(metadata[key]) not in labels:
                labels.append(str(metadata[key]))
        if metadata.get("system_generated") is True:
            labels.append("system-generated")
        if block.get("requested") is False:
            labels.append("context")
        if block.get("format") == "map_card_json":
            labels.append("legacy map_card_json fragment")
        offset = block.get("char_offset", legacy.get("char_offset", 0))
        end = block.get("char_end", legacy.get("char_end", offset + len(block.get("text", ""))))
        if offset or block.get("complete") is False or block.get("format") == "map_card_json" or end != offset + len(block.get("text", "")):
            labels.append(f"chars {offset}:{end}" + ("; partial" if block.get("complete") is False else ""))
        if "text" in block:
            parts.append("[" + " · ".join(labels) + "]\n" + block["text"])
        else:
            location = _anchor(anchor)
            if location not in unavailable_seen and block.get("requested") is not False:
                parts.append("Unavailable: " + _notice({"anchor": anchor, "reason": block.get("reason")}))
            unavailable_seen.add(location)
    for row in result.get("session_navigation") or []:
        parts.append(_navigation(row))
    for link in result.get("references") or []:
        # Pairing is already represented by delivered source blocks, not navigation.
        if link.get("kind") != "call_result" and (link.get("src") in refs or link.get("dst") in refs):
            parts.append(_link(link))
    for link in result.get("map_links") or []:
        parts.append(_link(link))
    for stem, label in (("references", "References"), ("session_navigation", "Child navigation")):
        if result.get(stem + "_result"):
            parts.append(label + " result: " + str(result[stem + "_result"]))
        parts.append(_cursor(result, stem + "_cursor", "trace_query", "More " + label.lower()))
    if not any(parts):
        parts.append("No source blocks on this page.")
    parts.append(_cursor(result, "next_cursor", "trace_read"))
    return _join(parts)


def _cooccur(result):
    rid = result.get("result", "result")
    counts = result.get("counts") or {}
    matched = counts.get("matched_seed_events", result.get("row_count", 0))
    unmatched = counts.get("unmatched_seed_events", 0)
    parts = [f"{rid} — {matched} matched seeds; {unmatched} unmatched"]
    if result.get("unmatched_result"):
        parts.append("Unmatched: " + str(result["unmatched_result"]))
    for position, row in enumerate(result.get("rows") or [], result.get("page_offset", 0)):
        handle = _handle(rid, row.get("row", position))
        parts.append(f"[{handle}] " + _anchor(row.get("anchor", row.get("ref"))))
        parts.extend(_condition(condition) for condition in row.get("conditions") or [])
        parts.append(_clipped(row))
    for item in result.get("inputs") or []:
        if item.get("excluded_non_event"):
            parts.append(f"{item['excluded_non_event']} non-event selections excluded")
    parts.append(_cursor(result, "next_cursor", "trace_cooccur"))
    parts.append(_cursor(result, "unmatched_cursor", "trace_query", "Unmatched rows"))
    return _join(parts)


def _edit(result):
    parts = [key.capitalize() + ": " + ", ".join(map(str, result[key]))
             for key in ("created", "updated", "deleted", "linked", "unlinked") if result.get(key)]
    return "\n".join(parts) if parts else "No map changes."


def render(name: str, result: dict) -> str:
    """Render one of the six native tool results without changing its payload."""
    operations = {"trace_open": _open, "trace_search": _search, "trace_query": _query,
                  "trace_read": _read, "trace_cooccur": _cooccur, "trace_edit": _edit}
    try:
        renderer = operations[name]
    except KeyError:
        raise ValueError("unknown trace operation: " + name) from None
    return renderer(result)
