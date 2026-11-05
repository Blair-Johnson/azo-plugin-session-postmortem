"""Fixture-only tests of the host-independent native TEXT renderer."""
from copy import deepcopy
import unittest

from azo_session_postmortem.render import render


class RenderTests(unittest.TestCase):
    def test_open_compact_catalog_deduplicates_notes_omits_capabilities(self):
        card = {"id": "decision", "title": "Keep exact text", "note": "Resume the audit.", "pinned": True}
        payload = {"workspace": "/tmp/work", "sources": [{"id": "S", "title": "Investigation", "warnings": ["missing child C"]}],
                   "pinned_entries": [card], "recent_entries": [card, {"id": "other", "title": "Other", "note": "Next task"}],
                   "saved_queries": [{"name": "errors", "last_result": "r3", "arguments": {"sql": "PRIVATE_RECIPE"}}],
                   "capabilities": {"schema": "HUGE_SCHEMA"}, "counts": {"events": 100000}, "next_cursor": "c1"}
        text = render("trace_open", payload)
        self.assertIn("S — Investigation", text)
        self.assertIn("S — Investigation (missing child C)", text)
        self.assertNotIn("Warning", text)
        self.assertEqual(text.count("Resume the audit."), 1)
        self.assertIn("other — Other\nNext task", text)
        self.assertIn("Saved: errors → r3", text)
        self.assertIn("trace_open cursor=c1", text)
        for hidden in ("PRIVATE_RECIPE", "HUGE_SCHEMA", "100000", "pinned=true"):
            self.assertNotIn(hidden, text)

    def test_search_raw_totals_not_collapsed_group_count(self):
        payload = {"result": "r7", "matched_total": {"source": 12, "map": 1}, "selected_count": {"source": 12, "map": 1},
                   "hits": {"source": [{"row": 4, "anchor": {"event": "long-session/e32", "field": "text", "lines": [2, 3]},
                                          "ref": "long-session/e32", "excerpt": "  α\r\nβ  ", "score": 18,
                                          "occurrence_count": 12, "occurrences_result": "r6", "retrieval_mode": "terms"}],
                            "map": [{"row": 5, "entry_id": "decision", "matched_field": "note", "excerpt": "Map excerpt"}]},
                   "unavailable_count": 0, "next_cursor": "c8"}
        text = render("trace_search", payload)
        self.assertIn("r7 — 13 raw matches; 2 displayed groups", text)
        self.assertIn("[r7 row 4] long-ses…/e32 L2–3; 12 occurrences → r6\n  α\r\nβ  ", text)
        self.assertIn("[r7 row 5] map decision\nMap excerpt", text)
        self.assertIn("trace_search cursor=c8", text)
        for hidden in ("long-session", "score", "terms", "matched_field", "selected_count", "scope_summary"):
            self.assertNotIn(hidden, text)

    def test_search_all_excerpts_preserved_without_extra_clipping(self):
        excerpts = ["\t" + "λ" * 800 + "\r\n ", "end\n  "]
        hits = [{"row": i, "anchor": {"object": f"S/o{i}"}, "excerpt": excerpt,
                 "occurrence_count": 1, "occurrences_result": "singleton-hidden"} for i, excerpt in enumerate(excerpts)]
        text = render("trace_search", {"result": "r1", "hits": {"source": hits}, "matched_total": {"source": 2}, "unavailable_count": 0})
        for excerpt in excerpts:
            self.assertIn(excerpt, text)
        self.assertNotIn("singleton-hidden", text)
        self.assertNotIn("occurrences", text)

    def test_search_omits_coverage_warnings_and_map_only(self):
        payload = {"result": "r1", "matched_total": {"source": 0}, "hits": {}, "unavailable_count": 3,
                   "unavailable": [{"anchor": {"object": "S/missing"}, "reason": "not persisted"}]}
        text = render("trace_search", payload)
        self.assertIn("0 raw matches", text)
        self.assertNotIn("unavailable", text)
        self.assertNotIn("S/missing", text)
        self.assertIn("No hits", text)
        self.assertNotIn("coverage", render("trace_search", {"result": "legacy", "hits": {}}))
        self.assertNotIn("unknown", render("trace_search", {"result": "map", "hits": {}, "matched_total": {"map": 0},
                                                                    "scope_summary": {"event_count": None, "object_count": None}}))

    def test_search_clipping_notice_keeps_navigation(self):
        text = render("trace_search", {"result": "r2", "hits": {"source": [{"row": 9, "anchor": {"event": "S/e9"},
                                                                                 "excerpt": "prefix", "_display_truncated": True}]},
                                           "unavailable_count": 0})
        self.assertIn("prefix", text)
        self.assertIn("(preview clipped)", text)
        self.assertNotIn("trace_read", text)

    def test_query_all_columns_nulls_metadata_and_positional_rows(self):
        payload = {"result": "r4", "page_offset": 20, "columns": ["row", "ref", "nullable", "system_generated", "preview", "empty", "metadata"],
                   "rows": [{"row": 999, "ref": "S/e9", "nullable": None, "system_generated": False,
                             "preview": "first\nsecond", "empty": [], "metadata": {"path": "x", "value": None}}],
                   "row_count": 30, "next_cursor": "c4"}
        text = render("trace_query", payload)
        self.assertIn("[r4 row 20] row=999", text)
        self.assertIn("nullable=null", text)
        self.assertIn("system_generated=false", text)
        self.assertIn("preview=first\nsecond", text)
        self.assertIn("empty=[]", text)
        self.assertIn("metadata=(path=x; value=null)", text)
        self.assertIn("trace_query cursor=c4", text)
        self.assertNotIn("[r4 row 999]", text)

    def test_query_nested_anchors_and_unmatched_conditions(self):
        text = render("trace_query", {"result": "r5", "columns": ["anchor", "conditions", "failed_conditions"],
                    "rows": [{"anchor": {"event": "S/e1", "field": "arguments", "lines": [3, 8]},
                              "conditions": [{"name": "receipt", "status": "no_events_in_session", "before": 4, "after": 5,
                                              "witness_count": 0, "witnesses_result": None, "witnesses": []}],
                              "failed_conditions": ["receipt"]}]})
        self.assertIn("anchor=S/e1/arguments L3–8", text)
        self.assertIn("name=receipt; status=no_events_in_session; before=4; after=5; witness_count=0", text)
        self.assertIn("witnesses_result=null; witnesses=[]", text)
        self.assertIn("failed_conditions=[receipt]", text)

    def test_query_explicit_bookkeeping_and_clipping_column_preserved(self):
        text = render("trace_query", {"result": "r5", "columns": ["score", "origin", "_display_truncated"],
                                     "rows": [{"score": 4, "origin": None, "_display_truncated": True}]})
        self.assertIn("score=4; origin=null; _display_truncated=true", text)
        self.assertIn("(preview clipped)", text)
        self.assertNotIn("trace_read", text)

    def test_read_exact_source_body_without_trailing_normalization(self):
        for body in (" α\r\n\tβ\r\n  ", "", "x", "a\rb\u2028c\n", "```\nheader-like text\n```", "λ" * 10000):
            with self.subTest(body=repr(body[:30])):
                text = render("trace_read", {"blocks": [{"anchor": {"event": "S/e1", "field": "text", "lines": [1, 3]}, "text": body}],
                                             "sources": {"S/e1": {"role": "assistant", "kind": "message"}}})
                header, actual_body = text.split("\n", 1)
                self.assertEqual(actual_body, body)
                self.assertEqual(header, "[S/e1 L1–3 · assistant]")

    def test_read_multiple_source_bodies_and_partial_continuation(self):
        first, second = "a\r\n \t", "β\n "
        text = render("trace_read", {"blocks": [{"anchor": {"event": "S/e2", "field": "arguments"}, "text": first},
                                                {"anchor": {"object": "S/o2"}, "text": second, "char_offset": 12, "char_end": 16,
                                                 "complete": False, "requested": False}],
                                     "sources": {"S/e2": {"role": "assistant", "kind": "call", "tool": "terminal_run"}},
                                     "next_cursor": "c10"})
        self.assertIn("[S/e2/arguments · assistant · call · terminal_run]\n" + first + "\n\n", text)
        self.assertIn("[S/o2 · context · chars 12:16; partial]\n" + second + "\n\n", text)
        self.assertIn("trace_read cursor=c10", text)

    def test_read_legacy_delivered_anchor_and_split_card_remain_exact(self):
        body = '{"note":"α\\r\\n'
        text = render("trace_read", {"blocks": [{"entry_id": "note", "text": body, "format": "map_card_json",
                                                "char_offset": 8, "complete": False}], "next_cursor": "c12"})
        self.assertIn("legacy map_card_json fragment", text)
        self.assertIn("chars 8:" + str(8 + len(body)), text)
        self.assertIn("\n" + body + "\n\nNext", text)
        legacy = render("trace_read", {"blocks": [{"text": "exact "}],
                                        "delivered": [{"anchor": {"object": "S/old"}, "char_offset": 4, "char_end": 10}]})
        self.assertEqual(legacy, "[S/old · chars 4:10]\nexact ")

    def test_read_unavailable_and_useful_links_without_pair_edges(self):
        payload = {"blocks": [{"anchor": {"object": "S/missing"}, "available": False, "reason": "bytes lost"},
                              {"anchor": {"event": "S/e1"}, "text": "body"}],
                   "unavailable": [{"anchor": {"object": "S/missing"}, "reason": "bytes lost"}], "unavailable_count": 3,
                   "references": [{"src": "S/e0", "dst": "S/missing", "kind": "references"},
                                  {"src": "S/e1", "dst": "S/output", "kind": "references"},
                                  {"src": "S/e1", "dst": "S/e2", "kind": "call_result"}],
                   "references_result": "r9", "references_cursor": "c9"}
        text = render("trace_read", payload)
        self.assertEqual(text.count("bytes lost"), 1)
        self.assertNotIn("3 unavailable sources", text)
        self.assertNotIn("Warning", text)
        self.assertIn("S/e0 — references → S/missing", text)
        self.assertIn("S/e1 — references → S/output", text)
        self.assertNotIn("call_result", text)
        self.assertIn("References result: r9", text)
        self.assertIn("trace_query cursor=c9", text)

    def test_read_child_navigation_retains_exact_destinations_qualifiers(self):
        nav = {"dst": "child-full-id", "src": "S/e4", "via": "S/output", "anchor": {"event": "S/e2", "field": "text", "lines": [3, 5]},
               "kind": "rlm_output", "output_name": "report", "job_id": "job7", "indexed": False, "ambiguous": True,
               "parent_status": "mismatch", "recorded_parent_id": "other", "observed_session": "S"}
        text = render("trace_read", {"blocks": [], "session_navigation": [nav], "session_navigation_result": "r8", "session_navigation_cursor": "c8"})
        for term in ("Child child-full-id", "not indexed", "ambiguous association", "conflicting parent: recorded other, observed S",
                     "output report", "Launch: S/e2 L3–5", "trace_query cursor=c8"):
            self.assertIn(term, text)

    def test_map_card_authored_content_not_bookkeeping(self):
        card = {"id": "m1", "title": "Decision", "note": "Keep this note.\r\n ", "kind": "note", "pinned": True, "revision": 999,
                "fields": {"rationale": "why", "evidence": {"result": "r2", "rows": [1, 2]}, "nullable": None},
                "anchors": [{"event": "S/e2", "field": "arguments", "lines": [1, 2]}], "tags": ["review"],
                "links": [{"id": "l1", "from": "m1", "to": "m2", "label": "supports", "note": "Check this relation."}]}
        text = render("trace_read", {"blocks": [{"card": card, "entry_id": "m1"}]})
        for term in ("Map m1 — Decision", card["note"], "rationale=why", "evidence=r2 rows [1; 2]", "nullable=null",
                     "S/e2/arguments L1–2", "l1: m1 — supports → m2", "Check this relation."):
            self.assertIn(term, text)
        self.assertNotIn("revision", text)
        self.assertNotIn("pinned", text)

    def test_inspect_cost_table_and_query_cursor(self):
        text = render("trace_read", {"plan_result": "r10", "plan_cursor": "c10", "total_rows": 5,
                     "known_text_chars": 120, "predicted_text_chars": 20, "unavailable_rows": 1,
                     "partially_delivered_rows": 1, "fully_deferred_rows": 2,
                     "rows": [{"row": 0, "anchor": {"event": "S/e1"}, "text_chars": 120, "would_deliver_chars": 20, "available": True},
                              {"row": 1, "anchor": {"object": "S/missing"}, "text_chars": None, "would_deliver_chars": None, "available": False}]})
        for term in ("Plan r10 — 5 blocks; 120 known chars; 20 chars on next read", "r10 row 0 | S/e1 | 120 → 20", "unavailable",
                     "1 partial blocks", "2 deferred blocks", "trace_query cursor=c10"):
            self.assertIn(term, text)
        self.assertNotIn("trace_read cursor=c10", text)

    def test_buffer_only_managed_handle_and_continuation(self):
        self.assertEqual(render("trace_read", {"buffer": "buf_abc", "buffer_path": "/private/packet.txt", "packet_chars": 999,
                                              "block_count": 12, "references": [{"src": "a", "dst": "b"}], "next_cursor": "c1"}),
                         "Buffer buf_abc\n\nNext: trace_read cursor=c1")

    def test_cooccur_witness_handles_signed_distances_and_cursors(self):
        payload = {"result": "r20", "counts": {"matched_seed_events": 3, "unmatched_seed_events": 2}, "unmatched_result": "r21",
                   "unmatched_cursor": "c21", "next_cursor": "c20",
                   "rows": [{"row": 2, "anchor": {"event": "S/e5"}, "conditions": [{"name": "reply", "status": "within_window",
                             "before": 2, "after": 3, "witness_count": 3, "witnesses_result": "r22",
                             "witnesses": [{"row": 0, "anchor": {"event": "S/e4"}, "delta_seq": -1},
                                           {"row": 1, "anchor": {"event": "S/e7"}, "delta_seq": 2}]}]}],
                   "inputs": [{"name": "seed", "result": "r0", "selected_rows": 200, "duplicate_anchors": 9,
                               "excluded_non_event": 2, "coverage": {"unavailable_count": 1}},
                              {"name": "reply", "result": "r1", "coverage": {"unavailable_count": None}}]}
        text = render("trace_cooccur", payload)
        for term in ("r20 — 3 matched seeds; 2 unmatched", "Unmatched: r21", "[r20 row 2] S/e5", "3 witnesses → r22",
                     "[r22 row 0] S/e4; distance -1", "[r22 row 1] S/e7; distance +2", "2 non-event selections excluded",
                     "trace_cooccur cursor=c20", "trace_query cursor=c21"):
            self.assertIn(term, text)
        for hidden in ("selected_rows", "duplicate_anchors", "200", "unavailable", "coverage", "Warning", "Caution"):
            self.assertNotIn(hidden, text)

    def test_open_only_concrete_child_notes_not_import_metadata(self):
        text = render("trace_open", {"sources": [{"id": "S", "warnings": [
            "metadata path /private/path", "config warning", "missing child C", "missing child C",
            "Child source unavailable: D: missing saved file /private/detail"]}]})
        self.assertIn("S (missing child C; missing child D)", text)
        self.assertEqual(text.count("missing child C"), 1)
        for hidden in ("Warning", "metadata", "config", "/private"):
            self.assertNotIn(hidden, text)

    def test_short_search_locations_keep_distinct_sessions_and_exact_suffixes(self):
        refs = ["abcdefgh-long/e1/arguments", "ijklmnop-long/e1", "tiny/e1"]
        text = render("trace_search", {"result": "r1", "matched_total": {"source": 3},
            "hits": {"source": [{"anchor": {"event": ref}, "excerpt": "body"} for ref in refs]}})
        for index, short in enumerate(("abcdefgh…/e1/arguments", "ijklmnop…/e1", "tiny/e1")):
            self.assertIn(f"[r1 row {index}] {short}", text)
        exact = render("trace_read", {"blocks": [{"anchor": {"event": refs[0]}, "text": "body"}]})
        self.assertIn(refs[0], exact)
        self.assertNotIn("…", exact)

    def test_read_ignores_unavailable_context_and_plan_aggregate(self):
        text = render("trace_read", {"blocks": [
            {"anchor": {"event": "S/e1"}, "text": "body"},
            {"anchor": {"object": "S/context"}, "requested": False, "reason": "missing"}],
            "unavailable": [{"anchor": "S/context", "reason": "missing"},
                            {"anchor": "S/later", "reason": "missing"}], "unavailable_count": 99})
        self.assertEqual(text, "[S/e1]\nbody")
        repeated = render("trace_read", {"blocks": [
            {"anchor": {"object": "S/missing"}, "reason": "not captured"},
            {"anchor": "S/missing", "reason": "different explanation"}]})
        self.assertEqual(repeated, "Unavailable: S/missing: not captured")

    def test_child_navigation_omits_routine_association_metadata(self):
        text = render("trace_read", {"session_navigation": [{
            "dst": "child-full-id", "title": "Review", "output_name": "report",
            "parent_status": "unknown", "association": "declared_output", "kind": "rlm_output",
            "src": "S/result", "via": "S/output", "job_id": "job-private",
            "anchor": {"event": "long-session/e2", "field": "arguments"}}]})
        self.assertEqual(text, "Child child-full-id — Review; output report\n  Launch: long-session/e2/arguments")

    def test_generated_query_pages_require_explicit_kind_marker(self):
        payload = {"result": "r8", "page_offset": 7, "presentation": "cooccur",
                   "columns": ["row", "ref", "session", "seq", "anchor", "conditions"],
                   "rows": [{"row": 7, "ref": "S/e1", "session": "S", "seq": 55,
                             "anchor": {"event": "S/e1"}, "conditions": [{"name": "reply",
                             "status": "no_events_in_session", "before": 1, "after": 2,
                             "witness_count": 0, "witnesses_result": None, "witnesses": []}]}],
                   "next_cursor": "c8"}
        text = render("trace_query", payload)
        self.assertIn("[r8 row 7] S/e1", text)
        self.assertIn("reply: no events in session", text)
        self.assertIn("trace_query cursor=c8", text)
        for hidden in ("session=", "seq=", "null", "witnesses=[]"):
            self.assertNotIn(hidden, text)
        del payload["presentation"]
        sql = render("trace_query", payload)
        for selected in ("row=7", "ref=S/e1", "session=S", "seq=55", "witnesses_result=null", "witnesses=[]"):
            self.assertIn(selected, sql)

    def test_generated_witness_page_keeps_location_distance_role_and_tool(self):
        payload = {"result": "r9", "presentation": "cooccur", "rows": [{
            "row": 0, "ref": "long-session/e2", "anchor": {"event": "long-session/e2"},
            "delta_seq": -2, "role": "assistant", "kind": "call", "tool": "terminal_run",
            "system_generated": None}], "columns": ["ref", "anchor", "delta_seq", "role", "kind", "tool", "system_generated"]}
        text = render("trace_query", payload)
        self.assertIn("[r9 row 0] long-session/e2; distance -2", text)
        self.assertIn("assistant \u00b7 call \u00b7 terminal_run", text)
        self.assertNotIn("system_generated", text)
        self.assertNotIn("null", text)

    def test_edit_receipts_only(self):
        payload = {"created": ["m1"], "updated": ["m2"], "deleted": ["m3"], "linked": ["l1"], "unlinked": ["l2"],
                   "entry_previews": [{"id": "m1", "note": "DO_NOT_REPEAT"}]}
        self.assertEqual(render("trace_edit", payload), "Created: m1\nUpdated: m2\nDeleted: m3\nLinked: l1\nUnlinked: l2")

    def test_empty_operations_and_legacy_optional_fields(self):
        cases = {"trace_open": "No catalog entries", "trace_query": "No rows", "trace_search": "No hits",
                 "trace_read": "No source blocks", "trace_edit": "No map changes", "trace_cooccur": "0 matched seeds; 0 unmatched"}
        for operation, expected in cases.items():
            with self.subTest(operation=operation):
                self.assertIn(expected, render(operation, {}))
        self.assertEqual(render("trace_read", {"blocks": None, "unavailable_count": None}), "No source blocks on this page.")
        with self.assertRaisesRegex(ValueError, "unknown trace operation"):
            render("other", {})

    def test_pure_deterministic_and_no_payload_mutation(self):
        payload = {"result": "r1", "columns": ["row", "nested"], "rows": [{"row": 8, "nested": [{"event": "S/e1"}]}]}
        original = deepcopy(payload)
        self.assertEqual(render("trace_query", payload), render("trace_query", payload))
        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
