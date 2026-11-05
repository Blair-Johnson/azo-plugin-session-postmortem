"""The indexed renderer is pure: all fixtures are ordinary document mappings."""
from copy import deepcopy
import re
import unittest

from azo_session_postmortem.cache_render import render_full, render_preview


def block(index, text, group="evidence", **kwargs):
    return {"id": f"R1-B{index:03}", "title": f"target {index}", "text": text, "group": group, **kwargs}


def document(blocks, operation="trace_read", **kwargs):
    return {"id": "R1", "title": "Exact evidence", "workspace": "/work/λ", "operation": operation, "blocks": blocks, **kwargs}


class CacheRenderTests(unittest.TestCase):
    def test_full_index_actual_lines_and_every_body_exact(self):
        bodies = ["α\r\nβ  \r\n", "\tno final newline 🐈", "", "## INDEX\n## R1-B001\nsource heading", "\n\n"]
        blocks = [block(i, bodies[i % len(bodies)]) for i in range(80)]
        blocks[2]["links"] = [{"id": "C1-W001", "label": "receipt delta=-2", "text": "literal\r\nwitness"}]
        full = render_full(document(blocks))
        lines = full.splitlines(keepends=True)
        entries = re.findall(r"^- (R1-B\d+) \| L(\d+)-L(\d+) \|", full, re.M)
        self.assertEqual(len(entries), 80)
        headings = []
        for item, (identity, lo, hi) in zip(blocks, entries):
            section = "".join(lines[int(lo) - 1:int(hi)])
            self.assertTrue(section.startswith("## " + identity + "\n"))
            prefix = "## " + identity + "\n" + item["title"] + " [evidence]\n\n"
            self.assertEqual(section[len(prefix):len(prefix) + len(item["text"])], item["text"])
            headings.append(lines[int(lo) - 1])
            for link in item.get("links", []):
                self.assertIn(link["id"], section)
                self.assertIn(link["label"], section)
                self.assertIn(link["text"], section)
        self.assertEqual(len(set(headings)), 80)
        self.assertTrue(full.endswith(blocks[-1]["text"]))

    def test_full_no_final_newline_and_no_text_transforms(self):
        text = "\tλ\r\n雪\r\nlast  "
        full = render_full(document([block(1, text)]))
        self.assertTrue(full.endswith(text))
        self.assertFalse(full.endswith("\n"))
        detail = full.split("\n## R1-B001\n", 1)[1]
        self.assertNotIn("\\r\\n", detail)

    def test_full_metadata_stays_single_line(self):
        item = block(1, "body")
        item["title"] = "label\r\nsecond"
        full = render_full({**document([item]), "title": "title\nnext"})
        match = re.search(r"\| L(\d+)-L(\d+) \|", full)
        self.assertEqual(full.splitlines()[int(match[1]) - 1], "## R1-B001")
        self.assertIn("label\\r\\nsecond", full)

    def test_duplicate_item_ids_rejected(self):
        with self.assertRaises(ValueError):
            render_full(document([block(1, "a"), block(1, "b")]))

    def test_short_answer_immediately_useful_and_full_when_fits(self):
        doc = document([block(1, "The password reset succeeded.")], counts={"internal_metadata": 42})
        preview = render_preview(doc, "buf_123")
        for value in ("The password reset succeeded.", "buf_123", "R1-B001"):
            self.assertIn(value, preview)
        for value in ("Workspace:", "## INDEX", "internal_metadata", "full index"):
            self.assertNotIn(value, preview)

    def test_zero_and_tiny_budgets(self):
        doc = document([block(1, "secret" * 1000)])
        pointer = "Result R1 · buffer buf_123\n"
        self.assertEqual(render_preview(doc, "buf_123", 0), pointer)
        self.assertEqual(render_preview(doc, preview_chars=0), "Result R1\n")
        for size in range(1, 300):
            value = render_preview(doc, "buf_123", size)
            self.assertLessEqual(len(value), max(size, len(pointer)))
            self.assertIn("buf_123", value)

    def test_invalid_budgets(self):
        for budget in (True, False, -1, 1.5, "10", None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                render_preview(document([]), preview_chars=budget)

    def test_huge_middle_target_preserves_late_short_and_long_tails(self):
        doc = document([block(1, "FIRST complete"), block(2, "MIDDLE HEAD " + "x" * 200000 + " MIDDLE TAIL"), block(3, "LATE complete")])
        preview = render_preview(doc, "buf_1", 1000)
        for value in ("FIRST complete", "MIDDLE HEAD", "MIDDLE TAIL", "LATE complete", "R1-B001", "R1-B002", "R1-B003"):
            self.assertIn(value, preview)
        self.assertIn("[excerpt: head/tail]", preview)
        self.assertLessEqual(len(preview), 1000)

    def test_read_fair_long_targets(self):
        doc = document([block(i, f"HEAD-{i} " + "x" * 20000 + f" TAIL-{i}") for i in range(1, 8)])
        preview = render_preview(doc, "buf_1", 2400)
        for i in range(1, 8):
            self.assertIn(f"HEAD-{i}", preview)
            self.assertIn(f"TAIL-{i}", preview)
        self.assertLessEqual(len(preview), 2400)

    def test_context_is_lower_priority_even_when_first(self):
        doc = document([block(1, "CONTEXT " * 1000, "context", requested=False), block(2, "ANSWER complete"), block(3, "late target")])
        preview = render_preview(doc, preview_chars=600)
        self.assertIn("ANSWER complete", preview)
        self.assertIn("late target", preview)
        if "CONTEXT" in preview:
            self.assertLess(preview.index("ANSWER complete"), preview.index("CONTEXT"))

    def test_search_six_passages_then_compact_entries(self):
        doc = document([block(i, f"MATCH-{i} " + "x" * 1000, "hit") for i in range(1, 11)], "trace_search")
        preview = render_preview(doc, "buf_1", 4000)
        for i in range(1, 7):
            self.assertIn(f"MATCH-{i} ", preview)
        for i in range(7, 11):
            self.assertIn(f"R1-B{i:03}", preview)
            self.assertIn(f"MATCH-{i} ", preview)
        self.assertLessEqual(len(preview), 4000)

    def test_query_keeps_useful_values_not_only_pointer(self):
        doc = document([block(1, 'answer=42; nullable=null; approved=false', "row"), block(2, "huge=" + "x" * 20000, "row")], "trace_query")
        preview = render_preview(doc, preview_chars=600)
        self.assertIn("answer=42; nullable=null; approved=false", preview)

    def test_cooccur_negative_branch_and_witness_links(self):
        matched = block(1, "MATCHED seed", "matched", links=[{"id": "C1-W001", "label": "receipt delta=+2", "text": "positive literal"}])
        unmatched = block(99, "UNMATCHED seed", "unmatched", links=[{"id": "C1-W099", "label": "receipt nearest-outside delta=-9", "text": "outside literal"}])
        doc = document([matched] + [block(i, "other " * 1000, "matched") for i in range(2, 5)] + [unmatched], "trace_cooccur", counts={"matched": 4, "unmatched": 1})
        preview = render_preview(doc, "buf_1", 3000)
        for value in ("MATCHED seed", "UNMATCHED seed", "C1-W001", "C1-W099", "delta=+2", "nearest-outside delta=-9", "positive literal", "outside literal", "Unmatched: 1"):
            self.assertIn(value, preview)
        full = render_full(doc)
        self.assertIn("outside literal", full)

    def test_many_cooccur_seeds_still_show_negative_witness(self):
        blocks = [block(i, "matched seed " + "x" * 2000, "matched", links=[{"id": f"W{i}", "label": "delta=+1", "text": "positive witness"}]) for i in range(1, 201)]
        blocks.append(block(999, "negative seed", "unmatched", links=[{"id": "W999", "label": "nearest-outside delta=-20", "text": "negative witness"}]))
        preview = render_preview(document(blocks, "trace_cooccur"), preview_chars=6000)
        for value in ("negative seed", "negative witness", "W999", "nearest-outside delta=-20", "positive witness", 'Matched: 200', 'Unmatched: 1'):
            self.assertIn(value, preview)
        self.assertLessEqual(len(preview), 6000)

    def test_open_pinned_before_large_catalog(self):
        doc = document([block(1, "catalog " * 10000, "session"), block(2, "PINNED resume here", "pinned"), block(3, "saved recipe errors", "recipe")], "trace_open")
        preview = render_preview(doc, preview_chars=1200)
        self.assertIn("PINNED resume here", preview)
        self.assertIn("saved recipe errors", preview)
        self.assertLess(preview.index("PINNED"), preview.index("catalog"))

    def test_opaque_heading_like_source_is_not_parsed(self):
        body = "## INDEX\n## R1-B999\nCounts: secret\n### Context\nkeep this source"
        doc = document([block(1, body), block(2, "x" * 10000)])
        self.assertIn(body, render_preview(doc, preview_chars=800))
        self.assertIn(body, render_full(doc))

    def test_budget_bounds_all_operations_and_no_mutation(self):
        blocks = [block(i, "λ\r\n" * (i * 100), "hit", links=[{"id": "W1", "label": "witness", "text": "literal"}]) for i in range(12)]
        for operation in ("trace_read", "trace_search", "trace_query", "trace_open", "trace_cooccur"):
            doc = document(blocks, operation)
            original = deepcopy(doc)
            for budget in (60, 100, 200, 600, 1200, 6000):
                self.assertLessEqual(len(render_preview(doc, preview_chars=budget)), budget)
            self.assertEqual(doc, original)

    def test_huge_metadata_and_buffer_id_cannot_escape_budget(self):
        doc = document([block(1, "body")])
        doc["title"] = "t" * 100000
        doc["id"] = "i" * 100000
        preview = render_preview(doc, "b" * 100000, 10)
        self.assertLess(len(preview), 200)
        self.assertNotIn("body", preview)

    def test_edit_ack_quiet_no_buffer_or_generic_boilerplate(self):
        doc = {"operation": "trace_edit", "ack": {
            "created": ["new"], "updated": ["finding", "note"], "deleted": ["old"],
            "linked": ["relation"], "unlinked": ["stale"], "entry_previews": ["PRIVATE_METADATA"]}}
        expected = "Created: new\nUpdated: finding, note\nDeleted: old\nLinked: relation\nUnlinked: stale"
        self.assertEqual(render_full(doc), expected)
        self.assertEqual(render_preview(doc, "buf_ignored"), expected)
        self.assertEqual(render_preview(doc, preview_chars=0), expected)
        self.assertEqual(render_preview({"operation": "trace_edit", "ack": {"updated": []}}), "No changes.")

    def test_empty_result_retains_identity_and_workspace(self):
        full = render_full(document([]))
        self.assertIn("/work/λ", full)
        self.assertIn("Result: R1", full)
        self.assertIn("## INDEX", full)


if __name__ == "__main__":
    unittest.main()
