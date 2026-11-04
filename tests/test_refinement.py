"""Regression cases derived from the first real-session CLI investigations."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import unittest

import test_contract as fixtures
from azo_session_postmortem import engine


class RefinementTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    call = fixtures.ContractTests.call
    cli = fixtures.ContractTests.cli

    @contextmanager
    def db(self):
        db = sqlite3.connect(Path(self.workspace) / "trace.sqlite3")
        try:
            with db:
                yield db
        finally:
            db.close()

    def inject(self):
        with self.db() as db:
            db.executemany("INSERT OR REPLACE INTO _content VALUES(?,?,?)", [
                ("S/e1", "provider_replay", "OPAQUE_SENTINEL" * 500),
                ("S/e1", "system_generated", "false"),
                ("S/e2", "metadata", '{"system_generated":true}'),
                ("S/e3", "system_generated", "not-a-boolean"),
            ])
            db.execute("ALTER TABLE events DROP COLUMN system_generated")

    def test_content_default_and_explicit_metadata_access(self):
        self.inject()
        args = dict(query="OPAQUE_SENTINEL", mode="literal")
        self.assertEqual(self.call("trace_search", **args)["hits"]["source"], [])
        self.assertTrue(self.call("trace_search", projection="all", **args)["hits"]["source"])
        scoped = self.call("trace_search", scope={"refs": [{"event": "S/e1", "field": "provider_replay"}]}, **args)
        self.assertTrue(scoped["hits"]["source"])
        page = self.call("trace_read", targets=["S/e1"], pair=False)
        self.assertNotIn("OPAQUE_SENTINEL", json.dumps(page))
        explicit = self.call("trace_read", targets=[{"event": "S/e1", "field": "provider_replay"}], pair=False)
        self.assertIn("OPAQUE_SENTINEL", json.dumps(explicit))
        self.assertNotIn("targets", page["blocks"][0])
        self.assertIn("S/e1", page["sources"])

    def test_legacy_backfill_preserves_results_maps_and_read_cursor(self):
        result = self.call("trace_query", sql="SELECT ref FROM events ORDER BY seq", page_size=1)
        read = self.call("trace_read", targets=["S/o-log"], max_chars=11)
        self.call("trace_edit", put=[{"id": "next", "kind": "note", "title": "Continue", "pinned": True}])
        self.inject()
        rows = self.cli("trace_query", sql="SELECT ref,system_generated FROM events ORDER BY seq")["rows"]
        flags = {r["ref"]: r["system_generated"] for r in rows}
        self.assertEqual(flags["S/e1"], 0)
        self.assertEqual(flags["S/e2"], 1)
        self.assertIsNone(flags["S/e3"])
        self.assertIsNone(flags["S/e4"])
        self.assertEqual(self.cli("trace_open")["pinned_entries"][0]["id"], "next")
        self.assertTrue(self.call("trace_query", cursor=result["next_cursor"])["rows"])
        next_read = self.call("trace_read", cursor=read["next_cursor"], max_chars=11)
        self.assertEqual(next_read["blocks"][0]["char_offset"], 11)

    def test_long_line_and_unicode_survive_tiny_pages(self):
        text = "αβ🙂" * 17 + "\nlast line"
        with self.db() as db:
            db.execute("UPDATE _content SET text=? WHERE ref='S/o-log'", (text,))
        page = self.call("trace_read", targets=["S/o-log"], max_chars=7)
        pieces = []
        while True:
            self.assertLessEqual(page["text_chars"], 7)
            self.assertEqual(page["packet_chars"], len(json.dumps(page, ensure_ascii=False, separators=(",", ":"), sort_keys=True)))
            pieces.extend(b["text"] for b in page["blocks"])
            if not page["next_cursor"]:
                break
            page = self.call("trace_read", cursor=page["next_cursor"], max_chars=7)
        self.assertEqual("".join(pieces), text)
        exported = self.call("trace_read", targets=["S/o-log"], output="buffer")
        self.assertEqual(exported["packet_chars"], len(Path(exported["buffer_path"]).read_text()))


    def test_old_saved_recipe_retains_all_fields(self):
        self.inject()
        self.call("trace_search", query="OPAQUE_SENTINEL", mode="literal",
                  projection="all", save_as="old-recipe")
        with self.db() as db:
            raw = db.execute("SELECT arguments FROM saved_queries WHERE name='old-recipe'").fetchone()[0]
            recipe = json.loads(raw)
            recipe.pop("projection")
            db.execute("UPDATE saved_queries SET arguments=? WHERE name='old-recipe'", (json.dumps(recipe),))
        self.assertTrue(self.cli("trace_search", saved="old-recipe")["hits"]["source"])

    def test_pending_previews_are_bounded_but_read_is_lossless(self):
        with self.db() as db:
            db.executemany("INSERT INTO _content VALUES(?,?,?)",
                           [("S/e1", f"extra_{i}", "x" * 20) for i in range(20)])
        first = self.call("trace_read", targets=["S/e1"], projection="all", max_chars=1)
        self.assertGreater(first["remaining_count"], 8)
        self.assertEqual(len(first["remaining"]), 8)
        seen = {b["anchor"]["field"] for b in first["blocks"]}
        while first["next_cursor"]:
            first = self.call("trace_read", cursor=first["next_cursor"], max_chars=200)
            seen.update(b["anchor"]["field"] for b in first["blocks"])
        self.assertTrue({f"extra_{i}" for i in range(20)} <= seen)

    def test_unavailable_notice_is_not_hidden_after_large_target(self):
        with self.db() as db:
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
                       ("S/o-missing", "buffer", 0, 0, 0, "S", "Unsupported saved buffer store"))
        page = self.call("trace_read", targets=["S/o-log", "S/o-missing"], max_chars=5)
        self.assertEqual(page["unavailable_count"], 1)
        self.assertEqual(page["unavailable"][0]["anchor"], "S/o-missing")
        self.assertIn("note", self.call("trace_open")["capabilities"]["map_kinds"])

    def test_missing_fields_and_unknown_anchors_fail_explicitly(self):
        for anchor in ("S/unknown", {"event": "S/e1", "field": "absent"}):
            with self.subTest(anchor=anchor):
                with self.assertRaisesRegex(ValueError, "unknown"):
                    self.call("trace_read", targets=[anchor])
                with self.assertRaisesRegex(ValueError, "unknown"):
                    self.call("trace_search", scope={"refs": [anchor]}, query="anything")
        for anchor in ({"event": "S/e1", "field": "removed"},
                       "S/unknown", {"session": "S", "events": [999, 1000]}):
            self.call("trace_edit", put=[{"id": "stale", "kind": "note", "title": "Retained annotation"}])
            with self.db() as db:
                db.execute("UPDATE map_entries SET anchors=? WHERE id='stale'", (json.dumps([anchor]),))
            with self.subTest(stored_anchor=anchor):
                with self.assertRaisesRegex(ValueError, "unknown"):
                    self.call("trace_read", targets=[{"entry": "stale", "part": "anchors"}])
                with self.assertRaisesRegex(ValueError, "unknown"):
                    self.call("trace_search", scope={"entries": ["stale"]}, query="anything")
            # The authored card itself remains readable for repair.
            self.assertEqual(self.call("trace_read", targets=[{"entry": "stale"}])["blocks"][0]["card"]["id"], "stale")
            with self.db() as db:
                db.execute("UPDATE map_entries SET anchors='[]' WHERE id='stale'")

    def test_lexical_counts_are_complete_before_display_collapse(self):
        for mode in ("literal", "terms"):
            with self.subTest(mode=mode):
                page = self.call("trace_search", query="routine output", mode=mode,
                                 scope={"refs": ["S/o-log"]}, top_k=1, page_size=7, max_chars=1800)
                self.assertEqual(page["matched_total"], {"source": 1199, "map": 0})
                self.assertEqual(page["selected_count"], page["matched_total"])
                rows = []
                while True:
                    self.assertLessEqual(len(page["hits"]["source"]), 7)
                    self.assertLessEqual(sum(len(engine._json(hit)) for hit in page["hits"]["source"]), 1800)
                    rows.extend(hit["row"] for hit in page["hits"]["source"])
                    if not page["next_cursor"]:
                        break
                    page = self.call("trace_search", cursor=page["next_cursor"], page_size=7, max_chars=1800)
                self.assertEqual(rows, list(range(1199)))
        collapsed = self.call("trace_search", query="Identical success output", mode="literal", collapse="exact")
        self.assertEqual(collapsed["matched_total"]["source"], 2)
        self.assertEqual(collapsed["selected_count"]["source"], 2)
        self.assertEqual(len(collapsed["hits"]["source"]), 1)
        self.assertEqual(collapsed["hits"]["source"][0]["occurrence_count"], 2)

    def test_long_line_search_previews_include_the_match(self):
        text = "ß " * 1000 + "needle " + "tail " * 1000
        with self.db() as db:
            db.execute("UPDATE _content SET text=? WHERE ref='S/o-log'", (text,))
        for mode in ("literal", "terms"):
            with self.subTest(mode=mode):
                hit, = self.call("trace_search", query="needle", mode=mode)["hits"]["source"]
                self.assertLessEqual(len(hit["excerpt"]), 600)
                self.assertIn("needle", hit["excerpt"])
                self.assertEqual(hit["anchor"]["lines"], [1, 1])

        pattern, terms = engine._matcher("i", "terms")
        self.assertEqual(len(list(engine._matches("İ", "terms", pattern, terms))), 1)
    def test_query_display_budget_keeps_full_result_and_navigation(self):
        text = '\"\\\n🙂' * 5000
        result = self.call("trace_query", sql="SELECT ref, :text AS text FROM events ORDER BY seq",
                           params={"text": text}, max_chars=200, page_size=8)
        self.assertEqual(result["row_count"], 8)
        self.assertLessEqual(sum(len(engine._json(row)) for row in result["rows"]), 200)
        self.assertTrue(result["rows"][0]["_display_truncated"])
        self.assertEqual(result["rows"][0]["ref"], "S/e1")
        with self.db() as db:
            saved = json.loads(db.execute("SELECT rows FROM _results WHERE id=?", (int(result["result"][1:]),)).fetchone()[0])
        self.assertEqual(saved[0]["text"], text)
        self.assertTrue(self.call("trace_read", targets=[{"result": result["result"], "rows": [0]}])["blocks"])

    def test_refresh_preserves_maps_results_and_cursor_snapshots(self):
        self.call("trace_edit", put=[{"id": "keep", "kind": "episode", "title": "Keep",
                                      "anchors": ["S/e1"], "pinned": True, "fields": {"next": "probe"}},
                                     {"id": "related", "kind": "note", "title": "Related"}],
                  link=[{"id": "edge", "from": "related", "to": "keep", "label": "qualifies"}])
        query = self.call("trace_query", sql="SELECT ref,preview FROM events ORDER BY seq", page_size=1, save_as="keep-query")
        read = self.call("trace_read", targets=["S/o-log"], max_chars=11)
        tables = ("map_entries", "map_members", "map_links", "saved_queries", "result_items", "_results", "_cursors", "_sources")
        with self.db() as db:
            before = {table: db.execute("SELECT * FROM " + table).fetchall() for table in tables}
        doc = json.loads(self.source.read_text())
        doc["entries"][1]["messages"][0]["content"] = "Changed saved text"
        self.source.write_text(json.dumps(doc))
        self.call("trace_open", refresh=True)
        with self.db() as db:
            after = {table: db.execute("SELECT * FROM " + table).fetchall() for table in tables}
        self.assertEqual(before, after)
        self.assertIn("Changed saved text", json.dumps(self.call("trace_read", targets=["S/e2"])))
        continued = self.call("trace_query", cursor=query["next_cursor"], page_size=1)
        self.assertEqual(continued["rows"][0]["preview"], "I will inspect the record lengths.")
        continued_read = self.call("trace_read", cursor=read["next_cursor"], max_chars=11)
        self.assertEqual(continued_read["blocks"][0]["char_offset"], 11)
        doc["entries"][1]["messages"][0]["content"] = "Must roll back"
        self.source.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.call("trace_open", refresh=True, sessions=[str(self.source.parent / "missing.json")])
        self.assertNotIn("Must roll back", json.dumps(self.call("trace_read", targets=["S/e2"])))
        self.assertEqual(self.call("trace_open")["pinned_entries"][0]["id"], "keep")
if __name__ == "__main__":
    unittest.main()
