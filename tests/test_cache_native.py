"""Cache/native boundary tests use real managed buffers and isolated envelopes."""
import importlib.util
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HOST = importlib.util.find_spec("agent_utils") is not None
if HOST:
    from agent_utils import State, ToolCall
    from agent_utils.files.buffer_manager import BufferManager
    from azo_session_postmortem import native


@unittest.skipUnless(HOST, "run in the Agent Zoo host environment")
class CacheNativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = State()
        self.state.session_cwd = str(self.root)
        self.state.tool_schemas = {}
        self.state.buffer_manager = BufferManager()
        self.state.step = 17

    def invoke(self, name, error=False, **arguments):
        args = {"workspace": "study", **arguments}
        call = ToolCall(id="cache-native", name=name, raw_args=json.dumps(args), parsed_args=args)
        self.state.pending_tool_calls = [call]
        self.assertIs(getattr(native, name)(self.state), self.state)
        self.assertEqual(call.error, error, call.result)
        return call.result

    def envelope(self, operation, budget=6000):
        text = "# Complete evidence\nUnicode naïve\r\n  exact tail  \n" + "later\n" * 100
        path = self.root / "durable.txt"
        path.write_bytes(text.encode())
        return {"document": {"id": "R1", "operation": operation,
                "workspace": str(self.root / "study"), "title": "Full", "blocks": []},
                "text": text, "path": str(path), "preview_chars": budget}

    def test_every_retrieval_delivers_full_text_and_durable_path(self):
        for component in native.TOOLS:
            if component.name == "trace_edit":
                continue
            with self.subTest(operation=component.name):
                envelope = self.envelope(component.name)
                kwargs = {"targets": ["S/e1"]} if component.name == "trace_read" else {}
                manager = self.state.buffer_manager
                with patch.object(native.cache, "dispatch", return_value=envelope) as dispatch, \
                     patch.object(manager, "create_scratch_buffer", wraps=manager.create_scratch_buffer) as create, \
                     patch.object(native.cache_render, "render_preview", return_value="useful preview") as render:
                    self.assertEqual(self.invoke(component.name, **kwargs), "useful preview")
                dispatch.assert_called_once_with(component.name,
                    {"workspace": str(self.root / "study"), **kwargs})
                create.assert_called_once_with(envelope["text"], path=envelope["path"], turn=17)
                document, buffer_id, budget = render.call_args.args
                self.assertIs(document, envelope["document"])
                self.assertEqual(budget, 6000)
                managed = manager.resolve_for_read(self.state, buffer_id)
                self.assertEqual(managed.text, Path(envelope["path"]).read_bytes().decode())
                self.assertNotEqual(buffer_id, envelope["path"])
                self.assertTrue({"buffer_manager", "step"} <= component.optional_reads)
                self.assertIn("buffer_manager", component.writes)

    def test_manager_preflight_all_retrievals_before_dispatch_or_workspace_creation(self):
        for manager in (None, object()):
            self.state.buffer_manager = manager
            for component in native.TOOLS:
                if component.name == "trace_edit":
                    continue
                with self.subTest(operation=component.name, manager=manager), \
                     patch.object(native.cache, "dispatch") as dispatch:
                    kwargs = {"targets": ["S/e1"]} if component.name == "trace_read" else {}
                    error = self.invoke(component.name, error=True, **kwargs)
                    self.assertIn("BufferManager", error)
                    dispatch.assert_not_called()
                    self.assertFalse((self.root / "study").exists())

    def test_edit_ack_needs_no_manager_and_creates_no_buffer(self):
        del self.state.buffer_manager
        envelope = {"document": {"operation": "trace_edit", "ack": {"created": ["note"]}},
                    "preview_chars": 6000}
        with patch.object(native.cache, "dispatch", return_value=envelope), \
             patch.object(native.cache_render, "render_preview", return_value="Created note") as render:
            self.assertEqual(self.invoke("trace_edit", put=[{"id": "note"}]), "Created note")
        render.assert_called_once_with(envelope["document"], "", 6000)
        self.assertNotIn("buffer_manager", native.trace_edit.optional_reads)
        self.assertFalse(hasattr(self.state, "buffer_manager"))

    def test_nullable_defaults_preserve_saved_recipe_and_nested_values(self):
        nested = {"items": ["S1-H001"]}
        for name, args in (
            ("trace_query", {"saved": "query", "params": None, "sample": None}),
            ("trace_search", {"saved": "search", "context_lines": None,
                              "follow_object_links": None, "preview_chars": None}),
            ("trace_cooccur", {"saved": "join", "seed": None, "against": None}),
            ("trace_search", {"scope": nested, "follow_object_links": False,
                              "context_lines": 0, "preview_chars": 0}),
            ("trace_edit", {"put": [{"id": "a", "note": None, "fields": {"k": None}}]}),
        ):
            with self.subTest(operation=name), \
                 patch.object(native.cache, "dispatch", return_value=self.envelope(name)) as dispatch, \
                 patch.object(native.cache_render, "render_preview", return_value="preview"):
                self.invoke(name, **args)
                expected = {k: v for k, v in args.items() if v is not None}
                self.assertEqual(dispatch.call_args.args[1],
                                 {"workspace": str(self.root / "study"), **expected})

    def test_source_path_resolution_preserves_session_ids(self):
        (self.root / "session.json").write_text("{}")
        with patch.object(native.cache, "dispatch", return_value=self.envelope("trace_open")) as dispatch, \
             patch.object(native.cache_render, "render_preview", return_value="preview"):
            self.invoke("trace_open", sessions=["session.json", "S", "missing/file.json"], descendants=False)
        self.assertEqual(dispatch.call_args.args[1]["sessions"],
                         [str(self.root / "session.json"), "S", str(self.root / "missing/file.json")])
        self.assertIs(dispatch.call_args.args[1]["descendants"], False)

    def test_invalid_preview_budget_has_no_workspace_side_effects(self):
        for name in ("trace_open", "trace_query", "trace_search", "trace_read", "trace_cooccur"):
            for budget in (-1, True, 1.5, "10"):
                with self.subTest(operation=name, budget=budget):
                    kwargs = {"targets": ["S/e1"]} if name == "trace_read" else {}
                    self.assertIn("preview_chars", self.invoke(name, error=True, preview_chars=budget, **kwargs))
                    self.assertFalse((self.root / "study").exists())


@unittest.skipUnless(HOST, "run in the Agent Zoo host environment")
class CacheNativeCompletenessTests(unittest.TestCase):
    if HOST:
        import test_native as fixtures
        setUp = fixtures.NativeTests.setUp
        invoke = fixtures.NativeTests.invoke
        open_source = fixtures.NativeTests.open_source

    def assert_index(self, document):
        text = self.last_text
        lines = text.split("\n")
        entries = re.findall(r"^- (\S+) \| L(\d+)-L(\d+) \|", text, re.MULTILINE)
        self.assertEqual(len(entries), len(document["blocks"]))
        self.assertEqual(len({item for item, _, _ in entries}), len(entries))
        blocks = {b["id"]: b for b in document["blocks"]}
        for item, start, end in entries:
            selected = "\n".join(lines[int(start)-1:int(end)])
            self.assertTrue(selected.startswith("## " + item + "\n"))
            self.assertIn(blocks[item]["text"].rstrip("\n"), selected)
            self.assertEqual(text.count("## " + item + "\n"), 1)
        self.assertTrue(any(p.read_bytes().decode() == text
                            for p in (self.root / "study" / "results").glob("*.txt")))

    def test_beyond_first_page_and_64_read_blocks_are_complete_in_managed_file(self):
        bodies = [f"record {i:03d} exact \r\ntail {i:03d}\n" for i in range(80)]
        self.source.write_text(json.dumps({"meta": {"session_id": "S"}, "entries": [
            {"index": i, "messages": [{"role": "assistant", "content": body}]}
            for i, body in enumerate(bodies)]}))
        self.open_source()
        query = self.invoke("trace_query", sql="SELECT ref FROM events ORDER BY seq", preview_chars=300)
        self.assertEqual(len(query["blocks"]), 80)
        self.assert_index(query)
        search = self.invoke("trace_search", query="record", mode="literal", preview_chars=300)
        self.assertEqual(len(search["blocks"]), 80)
        self.assert_index(search)
        read = self.invoke("trace_read", targets=[{"result": query["id"]}], pair=False, preview_chars=300)
        self.assertEqual(len(read["blocks"]), 80)
        self.assertEqual([b["text"] for b in read["blocks"]], bodies)
        self.assert_index(read)
        last = query["blocks"][-1]["id"]
        selected = self.invoke("trace_read", targets=[last], pair=False)
        self.assertEqual(selected["blocks"][0]["text"], bodies[-1])

    def test_huge_middle_target_does_not_hide_short_late_target(self):
        bodies = ["first short answer", "middle start " + "large " * 20000 + " middle tail", "late distinct answer"]
        self.source.write_text(json.dumps({"meta": {"session_id": "S"}, "entries": [
            {"index": i, "messages": [{"role": "assistant", "content": body}]}
            for i, body in enumerate(bodies)]}))
        self.open_source()
        read = self.invoke("trace_read", targets=["S/e1", "S/e2", "S/e3"], pair=False, preview_chars=1200)
        self.assertIn(bodies[0], self.last_preview)
        self.assertIn(bodies[2], self.last_preview)
        self.assertIn("middle start", self.last_preview)
        self.assertIn("middle tail", self.last_preview)
        self.assertEqual([b["text"] for b in read["blocks"]], bodies)
        self.assert_index(read)


if __name__ == "__main__":
    unittest.main()
