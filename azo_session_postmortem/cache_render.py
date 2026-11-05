"""Pure views of immutable cache documents; source text is always opaque.

Full-buffer ranges are one-based, inclusive LF line coordinates (CRLF is one
line ending). Separators belong to the renderer, never to a source body.
Preview budgets count characters. The bounded result/buffer pointer is always
returned, even for zero or a budget too small to hold it.
"""


def _line(value):
    """Keep metadata on one physical line without inspecting source bodies."""
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _short(value, limit):
    value = _line(value)
    return value if len(value) <= limit else value[:max(0, limit - 1)] + "…"


def _ack(document):
    ack = document.get("ack", {})
    if isinstance(ack, str):
        return ack
    lines = []
    for action in ("created", "updated", "deleted", "linked", "unlinked"):
        ids = ack.get(action)
        if ids:
            values = ids if isinstance(ids, (list, tuple)) else [ids]
            lines.append(action.capitalize() + ": " + ", ".join(_line(value) for value in values))
    return "\n".join(lines) or "No changes."


def _counts(document):
    if document["operation"] != "trace_cooccur":
        return ""
    # Complete relation blocks are authoritative; arbitrary backend metadata is
    # not presentation content. Keep only the two relevant branch counts.
    return " · ".join(group.capitalize() + ": " + str(sum(
        block["group"] == group for block in document["blocks"]))
        for group in ("matched", "unmatched"))


def _links(block):
    links = block.get("links", [])
    if block["group"] == "hit" and len(links) == 1 and links[0]["label"] == "occurrence":
        return [dict(links[0], text="")]
    return links


def _section(block):
    text = "## " + _line(block["id"]) + "\n"
    text += _line(block["title"]) + " [" + _line(block["group"]) + "]\n\n"
    text += block["text"]
    for link in _links(block):
        text += "\n\n→ " + _line(link["id"]) + " — " + _line(link["label"]) + "\n" + link["text"]
    return text


def render_full(document):
    """Return a complete indexed document, with no clipping or external state."""
    if document["operation"] == "trace_edit":
        return _ack(document)
    blocks = document["blocks"]
    ids = [block["id"] for block in blocks]
    if len(set(ids)) != len(ids):
        raise ValueError("block IDs must be unique")
    header = "# " + _line(document["title"]) + "\n"
    header += "Result: " + _line(document["id"]) + "\nWorkspace: " + _line(document["workspace"]) + "\n"
    if document.get("sql_result"):
        header += "SQL membership: " + _line(document["sql_result"]) + "\n"
    counts = _counts(document)
    if counts:
        header += counts + "\n"
    header += "\n## INDEX\n"
    # Every index entry occupies exactly one line; its digits cannot move ranges.
    start = header.count("\n") + len(blocks) + 2
    entries, sections = [], []
    for block in blocks:
        section = _section(block)
        end = start + section.count("\n") - int(section.endswith("\n"))
        entries.append("- " + _line(block["id"]) + " | L" + str(start) + "-L" + str(end)
                       + " | " + _line(block["group"]) + " | " + _line(block["title"])
                       + " | " + _short(block.get("excerpt") or block["text"], 120))
        sections.append(section)
        start += section.count("\n") + 2
    return header + "\n".join(entries) + "\n\n" + "\n\n".join(sections)


def _pointer(document, buffer_id):
    pointer = "Result " + _short(document["id"], 64)
    if buffer_id:
        pointer += " · buffer " + _short(buffer_id, 96)
    return pointer + "\n"


def _compact(block):
    return _heading(block).rstrip("\n") + " — " + _short(block.get("excerpt") or block["text"], 120)


def _heading(block):
    return "[" + _short(block["id"], 64) + "] " + _short(block["title"], 96) + " (" + _short(block["group"], 24) + ")\n"


def _excerpt(text, budget, head_tail=False):
    if len(text) <= budget:
        return text
    marker = "[excerpt: head/tail]\n" if head_tail else "[excerpt]\n"
    tail_marker = "\n[tail]\n" if head_tail else ""
    room = budget - len(marker) - len(tail_marker)
    if room < 2:
        return "[omitted]" if budget >= 9 else ""
    if head_tail:
        head = (room + 1) // 2
        return marker + text[:head] + tail_marker + text[-(room - head):]
    return marker + text[:room]


def _waterfill(lengths, budget):
    """Complete short values first, then divide the remainder fairly."""
    allocations = [0] * len(lengths)
    pending = sorted(range(len(lengths)), key=lengths.__getitem__)
    for offset, index in enumerate(pending):
        size = min(lengths[index], budget // (len(pending) - offset))
        allocations[index] = size
        budget -= size
    return allocations


def _card(block, budget, head_tail):
    header = _heading(block)
    room = budget - len(header)
    if room < 0:
        return ""
    links = _links(block)
    if budget >= _card_size(block):
        return header + block["text"] + "".join("\n" + _heading_link(link) + link["text"] for link in links)
    # Relations get their own fair share, rather than trailing a huge source body.
    link_budget = min(room // 2, sum(len(_heading_link(link)) + len(link["text"]) + 1 for link in links))
    link_text = ""
    if links:
        sizes = _waterfill([len(_heading_link(link)) + len(link["text"]) + 1 for link in links], link_budget)
        for link, size in zip(links, sizes):
            heading = _heading_link(link)
            if size >= len(heading) + 1 + min(10, len(link["text"])):
                link_text += "\n" + heading + _excerpt(link["text"], size - len(heading) - 1)
    return header + _excerpt(block["text"], room - len(link_text), head_tail) + link_text


def _heading_link(link):
    return "→ " + _short(link["id"], 64) + " — " + _short(link["label"], 100) + "\n"


def _card_size(block):
    return len(_heading(block)) + len(block["text"]) + sum(
        len(_heading_link(link)) + len(link["text"]) + 1 for link in _links(block))


def _ordered(document):
    blocks = document["blocks"]
    operation = document["operation"]
    if operation == "trace_search":
        singletons = {b["links"][0]["id"] for b in blocks
                      if b["group"] == "hit" and len(b.get("links", [])) == 1
                      and b["links"][0]["label"] == "occurrence"}
        return [b for b in blocks if not (b["group"] == "occurrence" and b["id"] in singletons)]
    if operation == "trace_read":
        groups = {}
        for block in blocks:
            if _requested(block):
                groups.setdefault(block.get("request_group", block["id"]), []).append(block)
        requested = [block for level in range(max(map(len, groups.values()), default=0))
                     for group in groups.values() for block in group[level:level + 1]]
        return requested + [block for block in blocks if not _requested(block)]
    if operation == "trace_open":
        return sorted(blocks, key=lambda b: b["group"] != "pinned")
    if operation == "trace_cooccur":
        # Show both branches before spending the budget on further positive seeds.
        matched = [b for b in blocks if b["group"] == "matched"]
        unmatched = [b for b in blocks if b["group"] == "unmatched"]
        first = matched[:1] + unmatched[:1]
        return first + matched[1:] + unmatched[1:] + [b for b in blocks if b["group"] not in ("matched", "unmatched")]
    return list(blocks)


def _requested(block):
    return block.get("requested", True) and block["group"] not in ("context", "navigation")


def _cards(blocks, budget, head_tail=False):
    selected = []
    minimum = 0
    for block in blocks:
        cost = len(_heading(block)) + min(48, len(block["text"])) + 2
        if minimum + cost > budget:
            break
        selected.append(block)
        minimum += cost
    if not selected:
        return "", 0
    # Include header lengths in the floor so unusually long labels cannot starve
    # a later target. Waterfill only the remaining content capacity.
    floors = [len(_heading(block)) + min(48, len(block["text"])) for block in selected]
    available = budget - 2 * len(selected) - sum(floors)
    extra = _waterfill([max(0, _card_size(block) - floor) for block, floor in zip(selected, floors)], available)
    return "\n\n".join(_card(block, floor + more, head_tail) for block, floor, more in zip(selected, floors, extra)), len(selected)


def render_preview(document, buffer_id="", preview_chars=6000):
    """Render useful bounded evidence, retaining explicit selection identities."""
    if isinstance(preview_chars, bool) or not isinstance(preview_chars, int) or preview_chars < 0:
        raise ValueError("preview_chars must be a nonnegative integer")
    if document["operation"] == "trace_edit":
        return _ack(document)
    pointer = _pointer(document, buffer_id)
    budget = preview_chars - len(pointer)
    if budget <= 0:
        return pointer
    blocks = _ordered(document)
    title = _short(document["title"], 180) + "\n"
    counts = _counts(document)
    if counts:
        title += counts + "\n"
    if len(title) + 32 > budget:
        title = ""
    # A small result needs its useful content, not the buffer's catalog layout.
    sizes = [_card_size(block) for block in blocks]
    if len(title) + sum(sizes) + 2 * max(0, len(blocks) - 1) <= budget:
        return pointer + title + "\n\n".join(
            _card(block, size, False) for block, size in zip(blocks, sizes))
    if document["operation"] == "trace_search":
        blocks = [dict(block, text=block["excerpt"])
                  if block.get("excerpt") and block["group"] == "hit"
                  else block for block in blocks]
    remaining = budget - len(title)
    # Reserve an explicit omission count; no cursor or repetitive warning footer.
    omitted_marker = "\n[" + str(len(blocks)) + " items omitted]\n"
    usable = max(0, remaining - len(omitted_marker))
    operation = document["operation"]
    if operation in ("trace_search", "trace_cooccur"):
        primary, secondary = blocks[:6], blocks[6:]
        reserve = min(usable // 4, sum(len(_compact(b)) + 1 for b in secondary))
        body, shown = _cards(primary, usable - reserve)
        for block in secondary:
            entry = "\n" + _compact(block)
            if len(body) + len(entry) > usable:
                break
            body += entry
            shown += 1
    elif operation in ("trace_read", "trace_open"):
        predicate = _requested if operation == "trace_read" else lambda b: b["group"] == "pinned"
        primary = [b for b in blocks if predicate(b)]
        secondary = [b for b in blocks if not predicate(b)]
        body, shown = _cards(primary, usable, head_tail=operation == "trace_read")
        if shown == len(primary):
            more, extra = _cards(secondary, max(0, usable - len(body) - 2))
            if more:
                body += ("\n\n" if body else "") + more
                shown += extra
    else:
        body, shown = _cards(blocks, usable)
    omitted = len(blocks) - shown
    suffix = "\n[" + str(omitted) + " items omitted]\n" if omitted else ""
    if len(suffix) > remaining - len(body):
        suffix = ""  # The pointer itself remains available at tiny budgets.
    return pointer + title + body + suffix
