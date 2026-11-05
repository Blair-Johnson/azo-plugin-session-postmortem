"""Single-occurrence pointers must not echo the hit's excerpt."""
from copy import deepcopy

from azo_session_postmortem.cache_render import render_full, render_preview


def document(links, group="hit"):
    return {
        "id": "S1", "operation": "trace_search", "title": "Search: 1 hit",
        "workspace": "/work",
        "blocks": [{"id": "S1-H001", "title": "source text", "group": group,
                    "text": "UNIQUE HIT PASSAGE", "links": links}],
    }


def occurrence(identity="S1-O001", text="UNIQUE HIT PASSAGE"):
    return {"id": identity, "label": "occurrence", "text": text}


def test_single_occurrence_has_pointer_without_repeated_excerpt():
    doc = document([occurrence()])
    original = deepcopy(doc)
    preview = render_preview(doc)
    assert preview.count("UNIQUE HIT PASSAGE") == 1
    assert "→ S1-O001 — occurrence" in preview
    section = render_full(doc).split("## S1-H001\n", 1)[1]
    assert section.count("UNIQUE HIT PASSAGE") == 1
    assert "→ S1-O001 — occurrence" in section
    assert doc == original


def test_multiple_occurrences_keep_their_distinct_excerpts():
    doc = document([occurrence(text="FIRST CONTEXT"),
                    occurrence("S1-O002", "SECOND CONTEXT")])
    for rendered in (render_preview(doc), render_full(doc)):
        assert "FIRST CONTEXT" in rendered
        assert "SECOND CONTEXT" in rendered
        assert "S1-O001" in rendered and "S1-O002" in rendered


def test_non_occurrence_witness_is_not_suppressed():
    doc = document([dict(occurrence(), label="receipt delta=+2")])
    assert render_preview(doc).count("UNIQUE HIT PASSAGE") == 2


def test_single_occurrence_respects_small_preview_budgets():
    doc = document([occurrence()])
    doc["blocks"][0]["text"] += " long context" * 1000
    for budget in (100, 200, 400, 1000):
        preview = render_preview(doc, preview_chars=budget)
        assert len(preview) <= budget
        assert preview.count("UNIQUE HIT PASSAGE") <= 1


def test_single_occurrence_card_stays_in_buffer_not_inline_preview():
    doc = document([occurrence()])
    doc["blocks"].append({"id": "S1-O001", "title": "Occurrence source text",
                          "group": "occurrence", "text": "UNIQUE HIT PASSAGE"})
    preview = render_preview(doc)
    assert preview.count("UNIQUE HIT PASSAGE") == 1
    assert "→ S1-O001 — occurrence" in preview
    assert "[S1-O001]" not in preview
    assert "## S1-O001\n" in render_full(doc)
