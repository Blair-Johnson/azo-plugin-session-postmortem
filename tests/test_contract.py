"""Small end-to-end checks of the proposed tool contracts, without live sessions."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = str(root / "study")
        self.source = root / "session.json"
        lines = [f"routine output {i}" for i in range(1, 1201)]
        lines[949] = "partial_record counterexample: checksum invalid"
        self.source.write_text(json.dumps({
            "format_version": 2,
            "meta": {"session_id": "S", "created_at": "2026-01-01T00:00:00Z"},
            "attrs": {"session_title": "Fixture parser investigation"},
            "components": {}, "core": {},
            "entries": [
                {"index": 0, "step": 0, "messages": [
                    {"role": "user", "content": "Implement a parser for complete records only."}]},
                {"index": 1, "step": 1, "messages": [
                    {"role": "assistant", "content": "I will inspect the record lengths."}]},
                {"index": 2, "step": 2, "messages": [], "tool_calls": [
                    {"id": "call1", "name": "terminal_run", "parsed_args": {"command": "inspect sample.bin"},
                     "raw_args": "{}", "result": "header=12 payload=88", "error": False}]},
                {"index": 3, "step": 3, "messages": [
                    {"role": "user", "content": "Now preserve partial captures too."}]},
                {"index": 4, "step": 4, "messages": [
                    {"role": "assistant", "content": "The complete-record assumption no longer applies."}]},
                {"index": 5, "step": 5, "messages": [
                    {"role": "assistant", "content": "Identical success output."}]},
                {"index": 6, "step": 6, "messages": [
                    {"role": "assistant", "content": "Identical success output."}]},
            ],
            "trace_objects": [{"ref": "S/o-log", "session": "S", "kind": "buffer",
                               "text": "\n".join(lines), "available": True}],
            "trace_links": [{"src": "S/e3", "dst": "S/o-log", "kind": "references"}],
        }))
        self.call("trace_open", sessions=[str(self.source)], descendants=False)

    def call(self, tool, **arguments):
        from azo_session_postmortem.engine import dispatch
        return dispatch(tool, {"workspace": self.workspace, **arguments})

    def cli(self, tool, **arguments):
        python = [sys.executable] + (["-S"] if sys.flags.no_site else [])
        proc = subprocess.run([*python, "-m", "azo_session_postmortem", tool,
                               "--workspace", self.workspace, "--json", json.dumps(arguments),
                               "--record", str(Path(self.temp.name) / "calls.jsonl")],
                              cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def source_hits(self, result):
        return result["hits"]["source"]

    def test_search_slice_survives_fresh_process(self):
        result = self.cli("trace_search", scope={"refs": [{"object": "S/o-log", "lines": [900, 1000]}]},
                          mode="literal", query="partial_record", save_as="counterexample")
        hit = self.source_hits(result)[0]
        self.assertEqual(hit["anchor"]["lines"][0], 950)
        packet = self.cli("trace_read", targets=[{"result": result["result"], "rows": [hit["row"]]}],
                          line_context=1, max_chars=1200)
        self.assertIn("partial_record", json.dumps(packet))
        rerun = self.cli("trace_search", saved="counterexample")
        self.assertNotEqual(result["result"], rerun["result"])

    def test_query_rejects_mutation_and_samples_repeatably(self):
        with self.assertRaises(Exception):
            self.call("trace_query", sql="DELETE FROM events")
        with self.assertRaises(Exception):
            self.call("trace_query", sql="ATTACH DATABASE ':memory:' AS other")
        sql = "SELECT ref, kind FROM events ORDER BY seq"
        args = {"sql": sql, "sample": {"n": 3, "by": ["kind"], "seed": 7}}
        first = self.call("trace_query", **args)
        second = self.call("trace_query", **args)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(len(first["rows"]), 3)

    # Additional tests below exercise durable map edits and bounded navigation.

    def test_map_updates_search_and_atomic_relations(self):
        anchor = {"object": "S/o-log", "lines": [949, 951]}
        self.call("trace_edit", put=[{
            "id": "episode", "kind": "episode", "title": "Input scope",
            "note": "scope-change: tentative interpretation", "anchors": [anchor],
            "fields": {"condition": "CLI", "next_probe": "Read instructions"}, "pinned": True,
        }, {"id": "question", "kind": "note", "title": "Check population", "anchors": []}])
        first_ref = self.call("trace_query", sql="SELECT ref FROM events ORDER BY seq LIMIT 1")["rows"][0]["ref"]
        self.call("trace_edit", put=[{"id": "episode", "add_anchors": [first_ref],
                                     "fields": {"next_probe": "Compare changed requirements"}}],
                  link=[{"id": "qualifier", "from": "question", "to": "episode",
                         "label": "qualifies", "note": "Requires inspection"}])
        row = self.call("trace_query", sql="SELECT fields FROM map_entries WHERE id='episode'")["rows"][0]
        fields = json.loads(row["fields"]) if isinstance(row["fields"], str) else row["fields"]
        self.assertEqual(fields["condition"], "CLI")
        self.assertEqual(fields["next_probe"], "Compare changed requirements")
        found = self.call("trace_search", scope={"workspace": True}, on="map",
                          mode="literal", query="scope-change")
        hit = found["hits"]["map"][0]
        self.assertEqual(hit["entry_id"], "episode")
        card = self.cli("trace_read", targets=[{"result": found["result"], "rows": [hit["row"]]}])
        self.assertIn("Compare changed requirements", json.dumps(card))
        raw = self.call("trace_search", scope={"workspace": True}, on="source",
                        mode="literal", query="scope-change")
        self.assertEqual(self.source_hits(raw), [])
        with self.assertRaises(Exception):
            self.call("trace_edit", delete=["question"])
        self.call("trace_edit", delete=["question"], unlink=["qualifier"])
        with self.assertRaises(Exception):
            self.call("trace_edit", put=[{"id": "must-rollback", "kind": "note",
                                          "title": "Do not persist", "anchors": []}],
                      link=[{"id": "bad", "from": "missing", "to": "episode", "label": "test"}])
        rows = self.call("trace_query", sql="SELECT id FROM map_entries WHERE id='must-rollback'")["rows"]
        self.assertEqual(rows, [])

    def test_pairing_pagination_and_buffer_export(self):
        calls = self.call("trace_query", sql="SELECT ref FROM events WHERE kind='call'")["rows"]
        self.assertEqual(len(calls), 1)
        pair = self.call("trace_read", targets=[calls[0]["ref"]], max_chars=2000)
        self.assertIn("header=12 payload=88", json.dumps(pair))
        first = self.cli("trace_query", sql="SELECT ref FROM events ORDER BY seq", page_size=1)
        self.assertTrue(first["next_cursor"])
        second = self.cli("trace_query", cursor=first["next_cursor"], page_size=1)
        self.assertNotEqual(first["rows"], second["rows"])
        packet = self.cli("trace_read", targets=[{"object": "S/o-log", "lines": [1, 1200]}],
                          line_context=0, max_chars=300)
        self.assertTrue(packet["next_cursor"])
        following = self.cli("trace_read", cursor=packet["next_cursor"], max_chars=300)
        self.assertNotEqual(packet["delivered"], following["delivered"])
        exported = self.cli("trace_read", targets=[{"object": "S/o-log", "lines": [949, 951]}],
                            output="buffer", line_context=0)
        self.assertTrue(Path(exported["buffer_path"]).is_file())
        self.assertIn("partial_record", Path(exported["buffer_path"]).read_text())

    def test_reused_search_selection_keeps_its_slice(self):
        found = self.call("trace_search", scope={"workspace": True}, mode="literal", query="partial_record")
        scoped = self.call("trace_search", scope={"result": found["result"]},
                           mode="literal", query="routine output")
        self.assertEqual(self.source_hits(scoped), [])
        self.call("trace_edit", put=[{"id": "exact", "kind": "episode", "title": "Exact hit",
                                     "anchors": [{"result": found["result"], "rows": [0]}]}])
        scoped = self.call("trace_search", scope={"entries": ["exact"]},
                           mode="literal", query="routine output")
        self.assertEqual(self.source_hits(scoped), [])

    def test_collapsed_occurrences_remain_navigable(self):
        found = self.call("trace_search", scope={"workspace": True}, mode="literal",
                          query="Identical success output", collapse="exact")
        hit = self.source_hits(found)[0]
        self.assertEqual(hit["occurrence_count"], 2)
        self.assertTrue(hit["occurrences_result"])
        read = self.call("trace_read", targets=[{"result": hit["occurrences_result"], "rows": [0, 1]}],
                         line_context=0, pair=False)
        self.assertGreaterEqual(len(read["delivered"]), 2)

    def test_capabilities_and_bad_arguments_are_explicit(self):
        with self.assertRaises(Exception):
            self.call("trace_search", scope={"workspace": True}, mode="similar", query="partial inputs")
        with self.assertRaises(Exception):
            self.call("trace_search", scope={"workspace": True}, query="partial", surprise=True)
        reopened = self.cli("trace_open")
        self.assertIn("capabilities", reopened)


if __name__ == "__main__":
    unittest.main()
