"""Adversarial trace_cooccur contract tests using synthetic retained selections.

Run with unittest discovery; operation calls always go through real dispatch.
Only safety constants are monkeypatched. Direct DB writes model legacy metadata.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from azo_session_postmortem.engine import dispatch


class CooccurAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = str(Path(self.temp.name) / "study")
        sources = []
        texts = {
            "S": ["far", "WIT before boundary", "WIT before near",
                  "SEED first\nunselected secret\nSEED second\nSEED third",
                  "WIT after near", "WIT after boundary", "WIT outside",
                  "SHARED exact", "SHARED exact", "end"],
            "T": ["OTHER_ONLY", "quiet", "quiet", "SEED_T",
                  "quiet", "quiet", "quiet", "SHARED exact",
                  "SHARED exact", "end"],
        }
        for sid, lines in texts.items():
            events = [{"text": text, "role": "assistant", "fields": {}}
                      for text in lines]
            if sid == "S":
                events[3]["fields"] = {"aux": "FIELD_WIT first\nFIELD_WIT second"}
            source = Path(self.temp.name) / (sid + ".json")
            source.write_text(json.dumps({
                "meta": {"session_id": sid}, "events": events,
                "trace_objects": ([
                    {"ref": "S/o-readable", "text": "OBJECT_ONLY", "available": True},
                    {"ref": "S/o-missing", "available": False, "reason": "synthetic missing"},
                ] if sid == "S" else []),
            }))
            sources.append(str(source))
        self.call("trace_open", sessions=sources, descendants=False)

    def call(self, tool, **arguments):
        return dispatch(tool, {"workspace": self.workspace, **arguments})

    def cli(self, tool, **arguments):
        proc = subprocess.run(
            [sys.executable, "-m", "azo_session_postmortem", tool,
             "--workspace", self.workspace, "--json", json.dumps(arguments)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    @contextmanager
    def db(self):
        db = sqlite3.connect(Path(self.workspace) / "trace.sqlite3")
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self):
        """All operation-owned persistent state, including IDs and exact JSON."""
        with self.db() as db:
            return {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                    for table in ("_results", "_cursors", "result_items", "saved_queries")}

    def stored(self, result):
        with self.db() as db:
            kind, rows, meta = db.execute(
                "SELECT kind,rows,meta FROM _results WHERE id=?", (int(result[1:]),)
            ).fetchone()
        return kind, json.loads(rows), json.loads(meta)

    def query(self, sql, **kwargs):
        return self.call("trace_query", sql=sql, max_chars=100000, **kwargs)

    def refs(self, *refs):
        slots = ",".join(":" + str(i) for i in range(len(refs)))
        return self.query("SELECT ref FROM events WHERE ref IN (" + slots + ") ORDER BY session,seq",
                          params={str(i): ref for i, ref in enumerate(refs)})["result"]

    def search(self, word, **kwargs):
        return self.call("trace_search", query=word, mode="literal", max_chars=100000, **kwargs)

    @staticmethod
    def condition(result, before=0, after=0, name="w", **selector):
        return {"name": name, "selection": {"result": result, **selector},
                "before": before, "after": after}

    def join(self, seed, against, **kwargs):
        return self.call("trace_cooccur", seed={"result": seed}, against=against,
                         max_chars=100000, **kwargs)

    def query_all(self, result, page_size=1):
        page = self.query("SELECT * FROM result_items WHERE result_id=:r ORDER BY row",
                          params={"r": result}, page_size=page_size)
        rows = list(page["rows"])
        while page["next_cursor"]:
            page = self.call("trace_query", cursor=page["next_cursor"], page_size=page_size,
                             max_chars=100000)
            rows.extend(page["rows"])
        return rows

    def test_same_event_different_fields_and_lines_preserve_all_seed_anchors(self):
        seed = self.search("SEED", scope={"refs": [{"event": "S/e4", "field": "text"}]})
        witness = self.search("FIELD_WIT", scope={"refs": [{"event": "S/e4", "field": "aux"}]})
        out = self.join(seed["result"], [self.condition(witness["result"])])
        self.assertEqual(out["operation"], "cooccur")
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["counts"], {"seed_events": 1, "matched_seed_events": 1,
                                         "unmatched_seed_events": 0})
        row = out["rows"][0]
        anchors = [hit["anchor"] for hit in seed["hits"]["source"]]
        self.assertEqual([a["lines"] for a in anchors], [[1, 1], [3, 3], [4, 4]])
        self.assertEqual(row["anchor"], anchors[0])
        self.assertEqual(row["seed_anchor_count"], 3)
        self.assertEqual(self.stored(out["result"])[1][0]["_anchors"], anchors)
        condition = row["conditions"][0]
        self.assertEqual(condition["status"], "within_window")
        self.assertEqual(condition["witness_count"], 2)
        self.assertEqual([w["anchor"] for w in condition["witnesses"]],
                         [hit["anchor"] for hit in witness["hits"]["source"]])
        self.assertEqual([w["delta_seq"] for w in condition["witnesses"]], [0, 0])
        for w in condition["witnesses"]:
            self.assertTrue({"row", "ref", "anchor", "delta_seq", "kind", "role", "tool",
                             "system_generated"} <= w.keys())
            self.assertIn(w["system_generated"], (True, False, None))
        self.assertNotIn("excerpt", row)

    def test_asymmetric_inclusive_boundaries_and_nearest_first_order(self):
        seed = self.refs("S/e4")
        witnesses = self.refs("S/e1", "S/e2", "S/e3", "S/e5", "S/e6", "S/e7")
        for before, after, expected in [(2, 1, [-1, 1, -2]), (1, 2, [-1, 1, 2]),
                                        (0, 2, [1, 2]), (2, 0, [-1, -2])]:
            with self.subTest(before=before, after=after):
                out = self.join(seed, [self.condition(witnesses, before, after)])
                c = out["rows"][0]["conditions"][0]
                _, rows, meta = self.stored(c["witnesses_result"])
                self.assertEqual([r["delta_seq"] for r in rows], expected)
                self.assertEqual(c["witness_count"], len(expected))
                self.assertEqual([r["delta_seq"] for r in c["witnesses"]], expected[:2])
                self.assertIn("columns", meta)
                self.assertIsNone(meta["sampling"])
                self.assertEqual(out["rows"][0]["anchor"], "S/e4")

    def test_all_conditions_are_seed_relative_not_pairwise_or_cartesian(self):
        seed = self.refs("S/e4")
        left = self.refs("S/e1", "S/e2", "S/e3")
        right = self.refs("S/e5", "S/e6", "S/e7")
        out = self.join(seed, [self.condition(left, 3, 0, "left"),
                               self.condition(right, 0, 3, "right")])
        self.assertEqual(out["row_count"], 1)
        self.assertEqual(out["rows"][0]["failed_conditions"], [])
        self.assertEqual([c["witness_count"] for c in out["rows"][0]["conditions"]], [3, 3])

    def test_collapsed_cross_session_occurrences_expand_but_never_cross_join(self):
        found = self.search("SHARED exact", collapse="exact")
        self.assertEqual(len(found["hits"]["source"]), 1)
        hit = found["hits"]["source"][0]
        self.assertEqual(hit["occurrence_count"], 4)
        against = self.refs("S/e8", "S/e9")
        for seed in (found["result"], hit["occurrences_result"]):
            with self.subTest(seed=seed):
                out = self.join(seed, [self.condition(against, 50, 50)])
                self.assertEqual(out["counts"], {"seed_events": 4, "matched_seed_events": 2,
                                                 "unmatched_seed_events": 2})
                self.assertEqual([r["ref"] for r in out["rows"]], ["S/e8", "S/e9"])
                self.assertEqual(out["inputs"][0]["anchor_count"], 4)
                unmatched = self.call("trace_query", cursor=out["unmatched_cursor"], max_chars=100000)
                self.assertEqual([r["ref"] for r in unmatched["rows"]], ["T/e8", "T/e9"])
                for row in unmatched["rows"]:
                    c = row["conditions"][0]
                    self.assertEqual(c["status"], "no_same_session_match")
                    self.assertEqual(c["witness_count"], 0)
                    self.assertEqual(c["witnesses"], [])
                    self.assertIsNone(c["witnesses_result"])
        out = self.join(against, [self.condition(found["result"])])
        self.assertEqual(out["counts"]["matched_seed_events"], 2)
        for row in out["rows"]:
            self.assertEqual(row["conditions"][0]["witness_count"], 1)
            self.assertEqual(row["conditions"][0]["witnesses"][0]["ref"], row["ref"])

    def test_failed_condition_keeps_one_deterministic_nearest_signed_witness(self):
        seed = self.refs("S/e4", "T/e4")
        ties = self.query("SELECT ref FROM events WHERE ref IN ('S/e3','S/e5') ORDER BY seq DESC")["result"]
        out = self.join(seed, [self.condition(ties)])
        self.assertEqual(out["rows"], [])
        self.assertIsNone(out["next_cursor"])
        page = self.call("trace_query", cursor=out["unmatched_cursor"], max_chars=100000)
        self.assertEqual(page["result"], out["unmatched_result"])
        first, second = page["rows"]
        self.assertEqual(first["failed_conditions"], ["w"])
        c = first["conditions"][0]
        self.assertEqual(c["status"], "outside_window")
        self.assertEqual(c["witness_count"], 1)
        self.assertEqual(c["witnesses"][0]["ref"], "S/e3")
        self.assertEqual(c["witnesses"][0]["delta_seq"], -1)
        self.assertEqual(len(self.stored(c["witnesses_result"])[1]), 1)
        self.assertEqual(second["conditions"][0]["status"], "no_same_session_match")

    def test_read_search_map_and_result_items_do_not_leak_witnesses_into_seeds(self):
        seed = self.search("SEED", scope={"refs": [{"event": "S/e4", "field": "text"}]})
        witnesses = self.search("WIT ", scope={"sessions": ["S"]})
        out = self.join(seed["result"], [self.condition(witnesses["result"], 3, 3)])
        selected = {"result": out["result"], "rows": [0]}
        packet = self.call("trace_read", targets=[selected], pair=False, line_context=0)
        text = "\n".join(b["text"] for b in packet["blocks"])
        self.assertIn("SEED first", text)
        self.assertIn("SEED third", text)
        self.assertNotIn("unselected secret", text)
        self.assertNotIn("WIT", text)
        for word in ("WIT", "unselected secret"):
            self.assertEqual(self.search(word, scope={"result": out["result"]})["hits"]["source"], [])
        self.call("trace_edit", put=[{"id": "selected", "kind": "episode", "title": "Selection",
                                      "anchors": [selected]}])
        self.assertEqual(self.search("WIT", scope={"entries": ["selected"]})["hits"]["source"], [])
        self.assertEqual(len(self.search("SEED", scope={"entries": ["selected"]})["hits"]["source"]), 3)
        items = self.query_all(out["result"])
        self.assertEqual({r["ref"] for r in items}, {"S/e4"})
        self.assertEqual({tuple(json.loads(r["anchor"])["lines"]) for r in items},
                         {(1, 1), (3, 3), (4, 4)})
        witness_result = out["rows"][0]["conditions"][0]["witnesses_result"]
        items = self.query_all(witness_result)
        self.assertEqual(len(items), 5)
        self.assertEqual([r["ref"] for r in items], ["S/e3", "S/e5", "S/e2", "S/e6", "S/e7"])
        self.assertEqual(len(self.search("WIT", scope={"result": witness_result})["hits"]["source"]), 5)
        witness_packet = self.call("trace_read", targets=[{"result": witness_result}], pair=False, line_context=0)
        self.assertTrue(all("WIT" in b["text"] for b in witness_packet["blocks"]))
        self.call("trace_edit", put=[{"id": "witnesses", "kind": "episode", "title": "Witnesses",
                                      "anchors": [{"result": witness_result}]}])
        self.assertEqual(len(self.search("WIT", scope={"entries": ["witnesses"]})["hits"]["source"]), 5)

    def test_original_row_indexes_empty_selections_and_deduplication(self):
        seed = self.query("SELECT ref FROM events WHERE session='S' ORDER BY seq", page_size=1)
        witness = self.refs("S/e4")
        out = self.call("trace_cooccur", seed={"result": seed["result"], "rows": [3]},
                        against=[self.condition(witness)], max_chars=100000)
        self.assertEqual([r["ref"] for r in out["rows"]], ["S/e4"])
        duplicate = self.query("SELECT 'S/e4' AS ref UNION ALL SELECT 'S/e4'")["result"]
        out = self.join(duplicate, [self.condition(witness)])
        self.assertEqual(out["inputs"][0]["selected_rows"], 2)
        self.assertEqual(out["inputs"][0]["duplicate_anchors"], 1)
        self.assertEqual(out["inputs"][0]["anchor_count"], 1)
        self.assertEqual(out["rows"][0]["seed_anchor_count"], 1)
        empty = self.query("SELECT ref FROM events WHERE 0")["result"]
        for selector in ({"result": empty}, {"result": seed["result"], "rows": []}):
            out = self.call("trace_cooccur", seed=selector, against=[self.condition(witness)])
            self.assertEqual(out["counts"], {"seed_events": 0, "matched_seed_events": 0,
                                             "unmatched_seed_events": 0})
            self.assertEqual(out["rows"], [])
            self.assertIsNone(out["unmatched_cursor"])
            self.assertIsNone(out["next_cursor"])
            self.assertEqual(out["inputs"][0]["selected_rows"], 0)
        out = self.join(witness, [self.condition(witness, rows=[])])
        self.assertEqual(out["counts"]["unmatched_seed_events"], 1)
        self.assertEqual(out["inputs"][1]["selected_rows"], 0)

    def test_objects_map_cards_and_aggregates_are_exclusions_not_event_expansion(self):
        seed = self.refs("S/e4")
        self.call("trace_edit", put=[{"id": "card", "kind": "episode", "title": "MAP_ONLY",
                                      "anchors": ["S/e4"]}])
        results = [self.search("OBJECT_ONLY")["result"], self.search("MAP_ONLY", on="map")["result"],
                   self.query("SELECT count(ref) AS n FROM events")["result"],
                   self.query("SELECT id AS entry_id FROM map_entries")["result"]]
        for result in results:
            with self.subTest(result=result):
                out = self.join(seed, [self.condition(result, 100, 100)])
                self.assertEqual(out["counts"]["matched_seed_events"], 0)
                self.assertEqual(out["inputs"][1]["excluded_non_event"], 1)
                self.assertEqual(out["inputs"][1]["event_count"], 0)
                self.assertEqual(out["inputs"][1]["anchor_count"], 0)
                reverse = self.join(result, [self.condition(seed)])
                self.assertEqual(reverse["counts"]["seed_events"], 0)
                self.assertEqual(reverse["inputs"][0]["excluded_non_event"], 1)

    def test_coverage_is_per_input_unknown_vs_zero_and_never_summed(self):
        query = self.refs("S/e4")
        search = self.search("SEED", scope={"refs": [{"event": "S/e4", "field": "text"}]})["result"]
        covered = self.search("SEED")["result"]
        out = self.join(query, [self.condition(search), self.condition(covered, name="x"),
                                self.condition(covered, name="y")])
        self.assertEqual([i["coverage"]["unavailable_count"] for i in out["inputs"]], [None, 0, 1, 1])
        self.assertTrue(all(i["coverage"]["scope"] == "input_result" for i in out["inputs"]))
        self.assertNotIn("unavailable_count", out)
        self.assertNotIn("unavailable_count", out["counts"])
        subset = self.call("trace_cooccur", seed={"result": covered, "rows": []},
                           against=[self.condition(search)])
        self.assertEqual(subset["inputs"][0]["coverage"]["unavailable_count"], 1)
        with self.db() as db:
            meta = self.stored(search)[2]
            meta.pop("unavailable")
            meta.pop("unavailable_count")
            db.execute("UPDATE _results SET meta=? WHERE id=?", (json.dumps(meta), int(search[1:])))
        legacy = self.join(search, [self.condition(query)])
        self.assertIsNone(legacy["inputs"][0]["coverage"]["unavailable_count"])
        self.assertEqual(legacy["inputs"][0]["coverage"]["unavailable"], [])

    def test_persistent_cursors_recipes_immutable_inputs_and_tiny_budget(self):
        seed = self.refs("S/e2", "S/e3", "S/e4", "S/e5", "T/e4")
        witness = self.refs("S/e4")
        original = {r: self.stored(r) for r in (seed, witness)}
        first = self.join(seed, [self.condition(witness, 2, 2)], page_size=1, save_as="nearby")
        self.assertEqual(first["row_count"], 4)
        counts = first["counts"]
        rows = list(first["rows"])
        page = first
        while page["next_cursor"]:
            page = self.cli("trace_cooccur", cursor=page["next_cursor"], page_size=2, max_chars=100000)
            self.assertEqual(page["counts"], counts)
            self.assertEqual(page["result"], first["result"])
            rows.extend(page["rows"])
        self.assertEqual([r["row"] for r in rows], [0, 1, 2, 3])
        self.assertEqual([r["ref"] for r in rows], ["S/e2", "S/e3", "S/e4", "S/e5"])
        kind, stored, meta = self.stored(first["result"])
        self.assertEqual(kind, "query")
        self.assertIn("columns", meta)
        self.assertIsNone(meta["sampling"])
        self.assertEqual(self.stored(first["unmatched_result"])[0], "query")
        self.assertIsNone(self.stored(first["unmatched_result"])[2]["sampling"])
        self.call("trace_edit", put=[{"id": "unrelated", "kind": "note", "title": "New map edit"}])
        self.query("SELECT count(ref) FROM events")
        repeat = self.cli("trace_cooccur", saved="nearby", page_size=100, max_chars=100000)
        self.assertNotEqual(repeat["result"], first["result"])
        self.assertEqual(repeat["counts"], counts)
        self.assertEqual([r["anchor"] for r in repeat["rows"]], [r["anchor"] for r in rows])
        self.assertEqual(self.stored(first["result"])[1], stored)
        self.assertEqual({r: self.stored(r) for r in original}, original)
        recipe = self.query("SELECT tool,arguments,last_result FROM saved_queries WHERE name='nearby'")["rows"][0]
        self.assertEqual(recipe["tool"], "cooccur")
        self.assertEqual(recipe["last_result"], repeat["result"])
        self.assertEqual(json.loads(recipe["arguments"])["seed"], {"result": seed})
        tiny = self.call("trace_cooccur", seed={"result": seed}, against=[self.condition(witness, 2, 2)],
                         max_chars=1, page_size=100)
        self.assertTrue(tiny["rows"])
        self.assertTrue(tiny["rows"][0]["_display_truncated"])
        self.assertEqual(tiny["counts"], counts)
        for extra in ({"seed": {"result": seed}}, {"against": [self.condition(witness)]},
                      {"save_as": "bad"}, {"saved": "nearby"}):
            before = self.state()
            with self.assertRaises(ValueError):
                self.call("trace_cooccur", cursor=first["next_cursor"], **extra)
            self.assertEqual(self.state(), before)
        for extra in ({"seed": {"result": seed}}, {"against": [self.condition(witness)]}):
            before = self.state()
            with self.assertRaises(ValueError):
                self.call("trace_cooccur", saved="nearby", **extra)
            self.assertEqual(self.state(), before)

    def test_invalid_arguments_fail_without_any_persistent_write(self):
        seed = self.refs("S/e4")
        good = {"seed": {"result": seed}, "against": [self.condition(seed)], "save_as": "must-not-save"}
        bad = [
            {"surprise": True}, {"MAX_INPUT_ANCHORS": 1}, {"seed": None}, {"seed": seed},
            {"seed": {"result": seed, "unknown": 1}}, {"seed": {"result": "r999999"}},
            {"seed": {"result": "r1;DROP TABLE events"}}, {"seed": {"result": True}},
            {"seed": {"result": seed, "rows": None}}, {"seed": {"result": seed, "rows": [True]}},
            {"seed": {"result": seed, "rows": [-1]}}, {"seed": {"result": seed, "rows": [99]}},
            {"seed": {"result": seed, "rows": [0.0]}}, {"seed": {"result": seed, "rows": "0"}},
            {"against": []}, {"against": None}, {"against": {}},
            {"against": [self.condition(seed)] * 4},
            {"against": [self.condition(seed), self.condition(seed)]},
            {"against": [self.condition(seed, name="seed")]},
            {"against": [self.condition(seed, name="")]},
            {"against": [self.condition(seed, name=True)]},
            {"page_size": True}, {"page_size": -1}, {"max_chars": True}, {"max_chars": -1},
        ]
        for key in ("before", "after"):
            for value in (True, -1, 1.5, "1", None):
                condition = self.condition(seed)
                condition[key] = value
                bad.append({"against": [condition]})
        for key in ("name", "selection", "before", "after"):
            condition = self.condition(seed)
            condition.pop(key)
            bad.append({"against": [condition]})
        bad.extend([{"against": [{**self.condition(seed), "unknown": 1}]},
                    {"against": [{**self.condition(seed), "selection": None}]},
                    {"against": [self.condition(seed, rows=None)]},
                    {"against": [self.condition(seed, extra=True)]}])
        for changed in bad:
            with self.subTest(changed=changed):
                before = self.state()
                with self.assertRaises(ValueError):
                    self.call("trace_cooccur", **{**good, **changed})
                self.assertEqual(self.state(), before)

    def test_malformed_or_stale_selected_anchors_are_errors_not_exclusions(self):
        seed = self.refs("S/e4")
        anchors = ["S/unknown", {"event": "S/e4", "field": "removed"},
                   {"event": "S/e4", "field": "text", "lines": [1, 999]},
                   {"event": "S/e4", "field": "text", "lines": [True, 1]},
                   {"event": "S/e4", "field": "text", "extra": "bad"}]
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                result = self.query("SELECT :a AS anchor", params={"a": json.dumps(anchor)})["result"]
                before = self.state()
                with self.assertRaises(ValueError):
                    self.join(seed, [self.condition(result)], save_as="bad-anchor")
                self.assertEqual(self.state(), before)

    def test_equal_distance_ties_sort_exact_fields_and_lines_not_input_order(self):
        anchors = [
            {"event": "S/e4", "field": "text", "lines": [4, 4]},
            {"event": "S/e4", "field": "text", "lines": [1, 1]},
            {"event": "S/e4", "field": "aux", "lines": [2, 2]},
            {"event": "S/e4", "field": "aux", "lines": [1, 1]},
        ]
        selected = self.query(" UNION ALL ".join("SELECT :a%d AS anchor" % i for i in range(4)),
                              params={"a%d" % i: json.dumps(a) for i, a in enumerate(anchors)})["result"]
        seed = self.refs("S/e3")
        matched = self.join(seed, [self.condition(selected, 0, 1)])
        c = matched["rows"][0]["conditions"][0]
        self.assertEqual([r["anchor"] for r in self.stored(c["witnesses_result"])[1]],
                         [anchors[3], anchors[2], anchors[1], anchors[0]])
        unmatched = self.join(seed, [self.condition(selected)])
        row = self.call("trace_query", cursor=unmatched["unmatched_cursor"])["rows"][0]
        self.assertEqual(row["conditions"][0]["witnesses"][0]["anchor"], anchors[3])
        self.assertEqual(row["conditions"][0]["witnesses"][0]["delta_seq"], 1)

    def test_unavailable_preview_is_bounded_per_input(self):
        source = Path(self.temp.name) / "unavailable.json"
        source.write_text(json.dumps({"meta": {"session_id": "U"}, "events": [],
            "trace_objects": [{"ref": "U/missing-%d" % i, "available": False,
                               "reason": "synthetic unavailable"} for i in range(12)]}))
        self.call("trace_open", sessions=[str(source)], descendants=False)
        found = self.search("SHARED exact", collapse="exact")
        self.assertEqual(found["unavailable_count"], 13)
        occurrence = found["hits"]["source"][0]["occurrences_result"]
        out = self.join(found["result"], [self.condition(occurrence)])
        self.assertEqual(len(out["inputs"]), 2)
        for item in out["inputs"]:
            self.assertEqual(item["coverage"]["unavailable_count"], 13)
            self.assertEqual(len(item["coverage"]["unavailable"]), 8)
            self.assertEqual(item["coverage"]["scope"], "input_result")
        self.assertNotIn("unavailable_count", out)

    def test_hard_limits_roll_back_results_cursors_and_recipe(self):
        from azo_session_postmortem import cooccur
        seed = self.refs("S/e3", "S/e4")
        witnesses = self.refs("S/e2", "S/e3", "S/e4", "S/e5", "S/e6")
        for constant, low in (("MAX_INPUT_ANCHORS", 3), ("MAX_SEED_EVENTS", 1),
                              ("MAX_WITNESS_ROWS", 6)):
            with self.subTest(constant=constant):
                before = self.state()
                with patch.object(cooccur, constant, low):
                    with self.assertRaisesRegex(ValueError, constant) as caught:
                        self.join(seed, [self.condition(witnesses, 3, 3, "a"),
                                         self.condition(witnesses, 3, 3, "b")],
                                  save_as="limit-failure", page_size=1)
                self.assertIn("narrow", str(caught.exception).lower())
                self.assertEqual(self.state(), before)
        out = self.join(seed, [self.condition(witnesses, 3, 3)], save_as="limit-success")
        self.assertEqual(out["counts"]["matched_seed_events"], 2)


if __name__ == "__main__":
    unittest.main()
