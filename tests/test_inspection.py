"""Read-plan inspection predicts delivery without persisting source bodies."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import unittest

import test_contract as fixtures
from azo_session_postmortem import engine


class InspectionTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    call = fixtures.ContractTests.call
    cli = fixtures.ContractTests.cli

    @contextmanager
    def db(self):
        db = sqlite3.connect(Path(self.workspace) / "trace.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def stored_rows(self, result):
        with self.db() as db:
            kind, rows, meta = engine._get_result(db, result)
        self.assertEqual(kind, "query")
        self.assertIsNone(meta["sampling"])
        return rows

    def add_objects(self, texts):
        refs = []
        with self.db() as db:
            for i, text in enumerate(texts):
                ref = f"S/inspection-{i}"
                refs.append(ref)
                db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
                           (ref, "buffer", 0 if text is None else len(text.splitlines()),
                            0 if text is None else len(text), int(text is not None), "S", "Unavailable fixture"))
                if text is not None:
                    db.execute("INSERT INTO _content VALUES(?,?,?)", (ref, "text", text))
        return refs

    def prediction(self, **arguments):
        plan = self.call("trace_read", inspect=True, **arguments)
        rows = self.stored_rows(plan["plan_result"])
        read = self.call("trace_read", **arguments)
        self.assertEqual(plan["predicted_text_chars"], read["text_chars"])
        self.assertEqual(plan["predicted_text_chars"], sum(r["would_deliver_chars"] or 0 for r in rows))
        self.assertEqual(plan["total_rows"], len(rows))
        self.assertEqual(plan["known_text_chars"], sum(r["text_chars"] or 0 for r in rows))
        self.assertEqual(plan["unavailable_rows"], read["unavailable_count"])
        for row, block in zip(rows, read["blocks"]):
            self.assertEqual(row["would_deliver_chars"], engine._read_size(block))
        for row in rows[len(read["blocks"]):]:
            self.assertIn(row["would_deliver_chars"], (0, None))
        self.assertEqual(plan["partially_delivered_rows"], sum(
            0 < r["would_deliver_chars"] < r["text_chars"] for r in rows if r["available"]))
        self.assertEqual(plan["fully_deferred_rows"], sum(
            r["text_chars"] > 0 and r["would_deliver_chars"] == 0 for r in rows if r["available"]))
        return plan, rows, read

    def test_capabilities_and_default_false_are_additive(self):
        caps = self.call("trace_open")["capabilities"]
        self.assertTrue(caps["read_inspection"])
        self.assertNotIn("workflow_guide", caps)
        self.assertEqual(caps["query_schema"], "docs/api.md")
        args = dict(targets=["S/e1"], pair=False)
        ordinary = self.call("trace_read", **args)
        self.assertEqual(ordinary, self.call("trace_read", inspect=False, **args))
        self.assertNotIn("plan_result", ordinary)
        self.assertEqual(ordinary["blocks"][0]["text"], "Implement a parser for complete records only.")
        self.assertEqual(ordinary["packet_chars"], len(engine._json(ordinary)))

    def test_strict_boolean_initial_inline_only_and_cursor_validation(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "inspect must be a boolean"):
                self.call("trace_read", targets=["S/e1"], inspect=value)
        for output in ("buffer", "unknown", None):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "requires output"):
                self.call("trace_read", targets=["S/e1"], inspect=True, output=output)
        for budget in (0, -1, True, 2.5, None):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "max_chars"):
                self.call("trace_read", targets=["S/e1"], inspect=True, max_chars=budget)
        with self.assertRaisesRegex(ValueError, "targets are required"):
            self.call("trace_read", inspect=True)
        initial = self.call("trace_read", targets=["S/o-log"], max_chars=2)
        for cursor in (initial["next_cursor"], "", "not-a-cursor"):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, "inspect=true cannot accompany cursor"):
                self.call("trace_read", cursor=cursor, inspect=True)
        next_page = self.call("trace_read", cursor=initial["next_cursor"], inspect=False, max_chars=100000)
        self.assertEqual(next_page["blocks"][0]["char_offset"], 2)
        for value in (None, 0, "false"):
            with self.subTest(cursor_inspect=value), self.assertRaisesRegex(ValueError, "inspect must be a boolean"):
                self.call("trace_read", cursor=initial["next_cursor"], inspect=value)
        with self.assertRaisesRegex(ValueError, "selection or expansion"):
            self.call("trace_read", cursor=initial["next_cursor"], inspect=False, pair=False)
        self.assertFalse((Path(self.workspace) / "packets").exists())

    def test_exact_metadata_unicode_costs_no_bodies_or_read_side_effects(self):
        secret = 'BODY_ONLY_SENTINEL_α🙂\\\"\n' * 40
        refs = self.add_objects([secret, "", None])
        plan = self.call("trace_read", inspect=True, targets=refs, max_chars=7)
        self.assertEqual(set(plan), {"plan_result", "rows", "plan_cursor", "total_rows", "known_text_chars",
                                    "unavailable_rows", "partially_delivered_rows", "fully_deferred_rows",
                                    "max_chars", "budget_unit", "predicted_text_chars"})
        self.assertEqual(plan["budget_unit"], "text_chars")
        self.assertEqual(plan["known_text_chars"], len(secret))
        self.assertEqual(plan["predicted_text_chars"], 7)
        self.assertEqual(plan["partially_delivered_rows"], 1)
        self.assertEqual(plan["fully_deferred_rows"], 0)
        self.assertNotIn("BODY_ONLY_SENTINEL", json.dumps(plan))
        rows = self.stored_rows(plan["plan_result"])
        fields = {"row", "anchor", "requested", "kind", "role", "tool", "text_chars", "would_deliver_chars", "available"}
        for i, row in enumerate(rows):
            self.assertEqual(set(row), fields | {"_anchors"})
            self.assertEqual(row["row"], i)
            self.assertEqual(row["_anchors"], [row["anchor"]])
            self.assertEqual(set(plan["rows"][i]), fields)
        self.assertEqual(rows[1]["text_chars"], 0)
        self.assertTrue(rows[1]["available"])
        self.assertIsNone(rows[2]["text_chars"])
        self.assertIsNone(rows[2]["would_deliver_chars"])
        self.assertFalse(rows[2]["available"])
        self.assertEqual(rows[2]["kind"], "buffer")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM _cursors").fetchone()[0], 0)
            stored = db.execute("SELECT rows,meta FROM _results").fetchall()
            self.assertNotIn("BODY_ONLY_SENTINEL", str([tuple(r) for r in stored]))
            self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(result_items)")],
                             ["result_id", "row", "ref", "anchor", "entry_id"])
        self.assertFalse((Path(self.workspace) / "packets").exists())

    def test_plan_pagination_is_bounded_query_snapshot_after_fresh_reopen(self):
        refs = self.add_objects([f"NEVER_IN_PLAN_BODY_{i}" * 2 for i in range(48)])
        plan = self.cli("trace_read", targets=refs, inspect=True, max_chars=100)
        snapshot = self.stored_rows(plan["plan_result"])
        self.assertLessEqual(len(plan["rows"]), 16)
        self.assertLessEqual(len(engine._json(plan["rows"])), 4000)
        self.assertTrue(plan["plan_cursor"])
        self.assertNotIn("next_cursor", plan)
        self.assertNotIn("NEVER_IN_PLAN_BODY", json.dumps(plan))
        with self.db() as db:
            self.assertEqual({r[0] for r in db.execute("SELECT kind FROM _cursors")}, {"query"})
            for row in db.execute("SELECT rows,meta FROM _results"):
                self.assertNotIn("NEVER_IN_PLAN_BODY", str(tuple(row)))
            for row in db.execute("SELECT payload FROM _cursors"):
                self.assertNotIn("NEVER_IN_PLAN_BODY", row[0])
            db.execute("UPDATE _content SET text='Changed' WHERE ref LIKE 'S/inspection-%'")
        self.cli("trace_open")
        with self.assertRaisesRegex(ValueError, "does not belong to read"):
            self.call("trace_read", cursor=plan["plan_cursor"])
        seen = list(plan["rows"])
        cursor = plan["plan_cursor"]
        while cursor:
            page = self.cli("trace_query", cursor=cursor, page_size=7, max_chars=2500)
            self.assertEqual(page["result"], plan["plan_result"])
            self.assertEqual(page["row_count"], 48)
            seen.extend(page["rows"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, [{k: v for k, v in r.items() if k != "_anchors"} for r in snapshot])
        items = self.call("trace_query", sql="SELECT row,ref,anchor FROM result_items WHERE result_id=:r ORDER BY row",
                          params={"r": plan["plan_result"]}, page_size=100, max_chars=100000)
        self.assertEqual(items["row_count"], 48)
        self.assertEqual([r["ref"] for r in items["rows"]], refs)
        with self.assertRaisesRegex(ValueError, "no such column"):
            self.call("trace_query", sql="SELECT text_chars FROM result_items")

    def test_giant_anchor_cannot_exceed_initial_display_cap(self):
        field = "oversized_field_" + "x" * 5000
        with self.db() as db:
            db.execute("INSERT INTO _content VALUES(?,?,?)", ("S/e1", field, "Hidden body"))
        plan = self.call("trace_read", targets=[{"event": "S/e1", "field": field}], inspect=True, pair=False)
        self.assertEqual(plan["rows"], [])
        self.assertEqual(plan["total_rows"], 1)
        self.assertTrue(plan["plan_cursor"])
        page = self.call("trace_query", cursor=plan["plan_cursor"], max_chars=10000)
        self.assertEqual(page["rows"][0]["anchor"]["field"], field)
        self.assertEqual(page["rows"][0]["text_chars"], len("Hidden body"))

    def test_result_rows_preserve_exact_slices_and_card_navigation(self):
        anchor = {"object": "S/o-log", "lines": [949, 951]}
        self.call("trace_edit", put=[{"id": "card", "kind": "episode", "title": "Card-only sentinel",
                                     "note": "NOTE_ONLY_SENTINEL", "anchors": [anchor]}])
        plan = self.call("trace_read", targets=[anchor, {"entry": "card"}], inspect=True,
                         pair=False, line_context=0, max_chars=10)
        rows = self.stored_rows(plan["plan_result"])
        self.assertEqual(rows[0]["anchor"], {"entry": "card", "part": "card"})
        self.assertEqual(rows[0]["kind"], "map_card")
        self.assertEqual(rows[1]["anchor"], anchor)
        self.assertNotIn("NOTE_ONLY_SENTINEL", json.dumps(rows))
        self.assertNotIn("Card-only sentinel", json.dumps(plan))
        selection = {"result": plan["plan_result"], "rows": [1, 0]}
        read = self.cli("trace_read", targets=[selection], pair=False, line_context=0)
        self.assertEqual(read["blocks"][0]["card"]["id"], "card")
        self.assertEqual(read["blocks"][1]["anchor"], anchor)
        self.assertIn("partial_record", read["blocks"][1]["text"])
        self.assertNotIn("routine output 948", read["blocks"][1]["text"])
        projected = self.call("trace_query", sql="SELECT row,ref,anchor,entry_id FROM result_items WHERE result_id=:r ORDER BY row",
                              params={"r": plan["plan_result"]})
        self.assertEqual(projected["rows"][0]["entry_id"], "card")
        self.assertEqual(json.loads(projected["rows"][1]["anchor"]), anchor)
        reread = self.call("trace_read", targets=[{"result": projected["result"]}], pair=False, line_context=0)
        self.assertEqual(reread["blocks"], read["blocks"])

    def test_overlapping_slices_whole_fields_and_line_context_keep_read_order(self):
        with self.db() as db:
            db.execute("UPDATE _content SET text=? WHERE ref='S/o-log'", ("a\nbb\nccc\ndddd\neeeee\nf",))
        first = {"object": "S/o-log", "lines": [2, 3]}
        overlap = {"object": "S/o-log", "lines": [3, 4]}
        plan, rows, _ = self.prediction(targets=["S/o-log", first, overlap, first], line_context=1,
                                        pair=False, max_chars=12)
        self.assertEqual([r["anchor"]["lines"] for r in rows], [[2, 4], [1, 1], [5, 6]])
        self.assertTrue(all(r["requested"] for r in rows))
        self.assertEqual(plan["known_text_chars"], len("a\nbb\nccc\ndddd\neeeee\nf"))
        _, rows, _ = self.prediction(targets=[first], line_context=1, pair=False, max_chars=1000)
        self.assertEqual([r["anchor"]["lines"] for r in rows], [[2, 3], [1, 1], [4, 4]])
        self.assertEqual([r["requested"] for r in rows], [True, False, False])

    def test_pair_event_context_and_projections_match_real_reader(self):
        with self.db() as db:
            db.execute("INSERT INTO _content VALUES(?,?,?)", ("S/e3", "provider_replay", "HIDDEN_REPLAY" * 30))
        for projection in ("content", "all"):
            with self.subTest(projection=projection):
                _, rows, read = self.prediction(targets=["S/e3"], pair=True, before=1, after=2,
                                                projection=projection, max_chars=100000)
                self.assertTrue(any(not r["requested"] for r in rows))
                self.assertTrue(any(r["tool"] == "terminal_run" for r in rows))
                self.assertTrue(any(r["kind"] == "result" for r in rows))
                self.assertTrue(any(r["role"] == "assistant" for r in rows))
                self.assertEqual(any(r["anchor"].get("field") == "provider_replay" for r in rows), projection == "all")
                self.assertEqual("HIDDEN_REPLAY" in json.dumps(read), projection == "all")
        _, rows, _ = self.prediction(targets=[{"event": "S/e3", "field": "provider_replay"}],
                                    pair=False, max_chars=17)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text_chars"], len("HIDDEN_REPLAY" * 30))
        _, rows, _ = self.prediction(targets=[{"session": "S", "events": [2, 4]}],
                                    pair=False, max_chars=30)
        self.assertTrue(all(r["requested"] for r in rows))
        self.assertEqual({r["anchor"]["event"] for r in rows}, {"S/e2", "S/e3", "S/e4"})

    def test_oversized_cards_stop_or_split_exactly_like_reader(self):
        self.call("trace_edit", put=[{"id": "small", "kind": "note", "title": "Small"},
                                     {"id": "large", "kind": "note", "title": "Large", "note": "CARD_BODY" * 200}])
        small = self.call("trace_read", targets=[{"entry": "small"}], inspect=True)["known_text_chars"]
        plan, rows, read = self.prediction(targets=[{"entry": "large"}, "S/e1"], pair=False, max_chars=11)
        self.assertEqual(read["blocks"][0]["format"], "map_card_json")
        self.assertEqual(plan["partially_delivered_rows"], 1)
        self.assertEqual(plan["fully_deferred_rows"], 1)
        for budget in (small, small + 20):
            with self.subTest(budget=budget):
                plan, rows, read = self.prediction(targets=[{"entry": "small"}, {"entry": "large"}, "S/e1"],
                                                  pair=False, max_chars=budget)
                self.assertEqual(plan["predicted_text_chars"], small)
                self.assertEqual(read["block_count"], 1)
                self.assertEqual(plan["partially_delivered_rows"], 0)
                self.assertEqual(plan["fully_deferred_rows"], 2)
                self.assertEqual([r["would_deliver_chars"] for r in rows], [small, 0, 0])

    def test_block_limit_defers_nonempty_rows_even_with_plenty_of_text_budget(self):
        refs = self.add_objects(["x"] * (engine.READ_BLOCK_LIMIT + 6))
        plan, _, read = self.prediction(targets=refs, max_chars=100000)
        self.assertEqual(read["block_count"], engine.READ_BLOCK_LIMIT)
        self.assertEqual(plan["predicted_text_chars"], engine.READ_BLOCK_LIMIT)
        self.assertEqual(plan["fully_deferred_rows"], 6)
        self.assertEqual(plan["partially_delivered_rows"], 0)

    def test_zero_length_rows_consume_blocks_not_text_or_deferred_counts(self):
        refs = self.add_objects([""] * (engine.READ_BLOCK_LIMIT + 2) + ["nonempty", None])
        plan, rows, read = self.prediction(targets=refs, max_chars=100000)
        self.assertEqual(read["block_count"], engine.READ_BLOCK_LIMIT)
        self.assertEqual(plan["predicted_text_chars"], 0)
        self.assertEqual(plan["fully_deferred_rows"], 1)
        self.assertEqual(plan["partially_delivered_rows"], 0)
        self.assertEqual(plan["unavailable_rows"], 1)
        self.assertEqual(rows[-2]["would_deliver_chars"], 0)
        self.assertIsNone(rows[-1]["would_deliver_chars"])
        # A zero-length body is still delivered while budget and block slots remain.
        _, _, read = self.prediction(targets=refs[:2], max_chars=1)
        self.assertEqual(read["block_count"], 2)

    def test_unavailable_rows_remain_unknown_even_when_not_budget_deferred(self):
        refs = self.add_objects([None] * (engine.READ_BLOCK_LIMIT + 3))
        plan, rows, read = self.prediction(targets=refs, max_chars=1)
        self.assertEqual(read["block_count"], engine.READ_BLOCK_LIMIT)
        self.assertEqual(plan["unavailable_rows"], engine.READ_BLOCK_LIMIT + 3)
        self.assertEqual(plan["known_text_chars"], 0)
        self.assertEqual(plan["fully_deferred_rows"], 0)
        self.assertTrue(all(r["text_chars"] is None and r["would_deliver_chars"] is None for r in rows))

    def test_empty_plan_and_fully_projected_out_event(self):
        for targets in ([], [{"result": self.call("trace_query", sql="SELECT ref FROM events WHERE 0")["result"]}]):
            plan, rows, read = self.prediction(targets=targets)
            self.assertEqual(rows, [])
            self.assertIsNone(plan["plan_cursor"])
            self.assertEqual(plan["total_rows"], 0)
            self.assertEqual(plan["known_text_chars"], 0)
            self.assertEqual(read["block_count"], 0)
        with self.db() as db:
            db.execute("DELETE FROM _content WHERE ref='S/e1'")
            db.execute("INSERT INTO _content VALUES(?,?,?)", ("S/e1", "provider_replay", "Hidden"))
        plan, rows, _ = self.prediction(targets=["S/e1"], pair=False)
        self.assertEqual(rows, [])
        self.assertEqual(plan["unavailable_rows"], 0)


if __name__ == "__main__":
    unittest.main()
