"""Focused source-adapter tests; run with unittest discovery from this directory."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from azo_session_postmortem import source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, doc):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
        return str(path)

    def test_provider_structured_dedup_and_preserved_entries(self):
        clock = lambda ns: {"occurred_at_ns": ns}
        doc = {"meta": {"session_id": "A"}, "entries": [
            {"index": 0, "forgotten": True, "compressed": True,
             "event_time": clock(1_000_000_000),
             "messages": [{"role": "user", "content": "pre-compaction original"}]},
            {"index": 1, "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c", "function": {"name": "view", "arguments": '{"buffer":"buf_abc123"}'}}]},
                {"role": "tool", "tool_call_id": "c", "content": "provider copy"}],
             "tool_calls": [{"id": "c", "name": "view", "parsed_args": {"buffer": "buf_abc123"},
                             "result": "canonical output", "started_at": clock(2_000_000_000),
                             "finished_at": clock(3_000_000_000)}]}]}
        session, = source.load_sources([self.write("a.json", doc)], descendants=False)
        self.assertEqual([e["kind"] for e in session["events"]], ["message", "call", "result"])
        self.assertEqual(session["events"][0]["text"], "pre-compaction original")
        self.assertEqual(session["events"][2]["text"], "canonical output")
        self.assertEqual([e["elapsed_s"] for e in session["events"]], [0, 1, 2])
        self.assertIn({"src": "A/e2", "dst": "A/e3", "kind": "call_result"}, session["links"])
        gap, = session["objects"]
        self.assertFalse(gap["available"])
        self.assertEqual(gap["ref"], "A/o_buf_abc123")

    def test_reused_call_id_is_not_global_dedup(self):
        entry = {"messages": [], "tool_calls": [{"id": "same", "name": "view", "result": "same text"}]}
        session, = source.load_sources([self.write("a.json", {"id": "A", "entries": [entry, entry]})])
        self.assertEqual([e["kind"] for e in session["events"]], ["call", "result", "call", "result"])

    def test_saved_buffers_and_opaque_values(self):
        doc = {"id": "A", "entries": [], "components": {"buffer_manager": {"buffers": {
            "buf_safe": {"__component__": "untrusted.Class", "__state__": {
                "id": "buf_safe", "text": "stored content", "initial_text": "original",
                "allocated": True, "path": "/not/read", "checkpoints": {
                    "before": {"__dataclass__": "untrusted.Snapshot", "__fields__": {"text": "checkpoint"}}}}},
            "buf_opaque": {"__pickle_inline__": "not a pickle", "__type__": "evil.Class"}
        }}}}
        session, = source.load_sources([self.write("a.json", doc)])
        texts = {o["text"] for o in session["objects"] if o["available"]}
        self.assertEqual(texts, {"stored content", "original", "checkpoint"})
        self.assertFalse(next(o for o in session["objects"] if o["ref"] == "A/o_buf_opaque")["available"])

    def test_children_and_missing_child(self):
        self.write("child.json", {"id": "B", "parent_id": "A", "entries": []})
        path = self.write("a.json", {"id": "A", "entries": [], "children": ["child.json", "missing.json"]})
        sessions = list(source.load_sources([path]))
        self.assertEqual([s["id"] for s in sessions], ["A", "B"])
        self.assertTrue(sessions[0]["warnings"])
        self.assertIn({"src": "A", "dst": "B", "kind": "child"}, sessions[0]["links"])
        self.assertEqual(len(list(source.load_sources([path], descendants=False))), 1)

    def test_fixture_objects_and_links(self):
        doc = {"id": "A", "entries": [], "trace_objects": [
            {"ref": "A/o1", "kind": "artifact", "text": "stored artifact", "available": True}],
            "trace_links": [{"src": "A/e99", "dst": "A/o1", "kind": "references"}]}
        session, = source.load_sources([self.write("a.json", doc)])
        self.assertEqual(session["objects"][0]["ref"], "A/o1")
        self.assertEqual(session["links"], doc["trace_links"])

    def test_ambiguous_prefix_and_missing_source(self):
        for sid in ("abc-one", "abc-two"):
            (self.root / "p" / "sessions" / sid).mkdir(parents=True)
        with patch.object(source, "_root", return_value=self.root):
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                list(source.load_sources(["abc"]))
            with self.assertRaisesRegex(ValueError, "unavailable"):
                list(source.load_sources(["no-such-prefix"]))

    def test_managed_loose_json_is_not_authority(self):
        path = Path(self.write("p/sessions/sid/session.json", {"id": "WRONG", "entries": []}))
        (path.parent / "journals").mkdir()
        from types import SimpleNamespace
        try:
            import agent_zoo.session_load
        except ImportError:
            self.skipTest("managed-session reader requires the optional agent_zoo host")
        loaded = SimpleNamespace(state=SimpleNamespace(document={"id": "sid", "entries": []},
            commit_id="commit", instance_id="instance", source_format="journal"),
            freshness="reconciled-observation", warning=None)
        with patch("agent_zoo.session_load.load_session", return_value=loaded) as loader:
            session, = source.load_sources([str(path)], descendants=False)
        self.assertEqual(session["id"], "sid")
        self.assertTrue(loader.call_args.kwargs["reconcile_shared"])
        self.assertEqual(session["provenance"]["commit_id"], "commit")


    def test_portable_state_root_uses_host_precedence_and_absolute_paths(self):
        import os
        import sys
        relative = "postmortem-relative-state"
        cases = [
            ({"AGENT_ZOO_HOME": relative, "AGENT_ZOO_STATE_ROOT": "ignored"}, Path(relative).resolve()),
            ({"AGENT_ZOO_HOME": "", "AGENT_ZOO_STATE_ROOT": relative}, Path(relative).resolve()),
            ({"HOME": str(self.root), "AGENT_ZOO_HOME": "~/chosen"}, self.root / "chosen"),
            ({"HOME": str(self.root)}, self.root / ".local/share/agent-zoo"),
        ]
        for env, expected in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {"agent_zoo.projects": None}):
                self.assertEqual(source._root(), expected.resolve() / "projects")
                self.assertFalse(expected.exists())
if __name__ == "__main__":
    unittest.main()
