"""Navigate real native previews through displayed immutable handles, not JSON."""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

HOST_AVAILABLE = importlib.util.find_spec("agent_utils") is not None
if HOST_AVAILABLE:
    from agent_utils import State, ToolCall
    from agent_utils.files.buffer_manager import BufferManager
    from azo_session_postmortem import native


@unittest.skipUnless(HOST_AVAILABLE, "run in the Agent Zoo host environment")
class NativeTextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = State()
        self.state.session_cwd = str(self.root)
        self.state.tool_schemas = {}
        self.state.buffer_manager = BufferManager()
        self.bodies = ["candidate one — naïve\r\n  exact tail  \n", "receipt alpha",
                       "candidate two", "receipt beta", "candidate three"]
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps({
            "format_version": 2, "meta": {"session_id": "S"},
            "entries": [{"index": i, "messages": [{"role": "assistant", "content": body}]}
                        for i, body in enumerate(self.bodies)],
        }), encoding="utf-8")

    def invoke(self, name, *, error=False, **arguments):
        arguments = {"workspace": "study", **arguments}
        call = ToolCall(id="native-text", name=name, raw_args=json.dumps(arguments), parsed_args=arguments)
        self.state.pending_tool_calls = [call]
        with patch.object(native.cache_render, "render_preview", wraps=native.cache_render.render_preview) as render:
            self.assertIs(getattr(native, name)(self.state), self.state)
        self.assertEqual(call.error, error, call.result)
        self.assertIsInstance(call.result, str)
        if not error:
            if name != "trace_edit":
                self.assertFalse(call.result.lstrip().startswith("{"), call.result)
            self.last_buffer = render.call_args.args[1]
            self.full_text = (self.state.buffer_manager.resolve_for_read(self.state, self.last_buffer).text
                              if self.last_buffer else "")
        return call.result

    def handles(self, text, kind):
        return list(dict.fromkeys(re.findall(rf"\b[A-Z]\d+-{kind}\d+\b", text)))

    def handle(self, text, kind="H", index=0):
        handles = self.handles(text, kind)
        self.assertGreater(len(handles), index, text)
        return handles[index]

    def result(self, item):
        return item.split("-", 1)[0]

    def open_source(self):
        return self.invoke("trace_open", sessions=["source.json"], descendants=False)

    def test_all_six_operations_from_displayed_handles_and_map_reopen(self):
        opened = self.open_source()
        self.assertIn("S", opened)
        found = self.invoke("trace_search", query="candidate", mode="literal", follow_object_links=False)
        first, second = self.handle(found), self.handle(found, index=1)
        read = self.invoke("trace_read", targets=[first], pair=False, line_context=0)
        self.assertIn(self.bodies[0].splitlines(keepends=True)[0], read)
        self.assertNotIn("exact tail", read)
        self.assertNotIn("receipt alpha", read)
        self.assertIn("candidate two", self.invoke("trace_read", targets=[second], pair=False, line_context=0))

        # A SQL payload's row column is not its immutable row selection identity.
        queried = self.invoke("trace_query", sql="SELECT 900+seq AS row, ref FROM events ORDER BY seq")
        self.assertIn("901", queried)
        query_first, query_second = self.handle(queried, "R"), self.handle(queried, "R", 1)
        self.assertIn(self.bodies[0], self.invoke("trace_read", targets=[query_first], pair=False))
        self.assertIn("receipt alpha", self.invoke("trace_read", targets=[query_second], pair=False))

        receipts = self.invoke("trace_search", query="receipt", mode="literal")
        witness_result = self.result(self.handle(receipts))
        joined = self.invoke("trace_cooccur", seed={"result": self.result(first)}, against=[{
            "name": "receipt", "selection": {"result": witness_result}, "before": 0, "after": 1,
        }])
        matched = self.handle(joined, "M")
        unmatched = self.handle(joined, "U")
        self.assertIn("candidate one", joined)
        self.assertIn("candidate three", joined)
        self.assertIn(self.bodies[0].splitlines(keepends=True)[0],
                      self.invoke("trace_read", targets=[matched], pair=False, line_context=0))
        self.assertIn("candidate three", self.invoke("trace_read", targets=[unmatched], pair=False))
        witness = self.handle(joined, "W")
        self.assertIn("receipt", self.invoke("trace_read", targets=[witness], pair=False))
        self.assertIn("receipt alpha", self.invoke("trace_read", targets=[{
            "result": self.result(matched), "group": "witnesses"}], pair=False))

        edited = self.invoke("trace_edit", put=[{
            "id": "finding", "kind": "episode", "title": "Candidate evidence", "pinned": True,
            "note": "Caller-authored finding", "anchors": [second],
        }])
        self.assertIn("finding", edited)
        reopened = self.invoke("trace_open")
        self.assertIn("Candidate evidence", reopened)
        self.assertIn("Caller-authored finding", reopened)
        card = self.invoke("trace_read", targets=[{"entry": "finding", "part": "card"}])
        self.assertIn("Candidate evidence", card)
        anchors = self.invoke("trace_read", targets=[{"entry": "finding", "part": "anchors"}], pair=False)
        self.assertIn("candidate two", anchors)
        self.assertFalse(self.state.entries)

    def test_preview_budgets_never_truncate_buffer_or_source_selection(self):
        self.open_source()
        options = dict(targets=[{"event": "S/e1", "field": "text"}], pair=False, line_context=0)
        ordinary = self.invoke("trace_read", **options)
        self.assertIn(self.bodies[0], ordinary)
        for budget in (None, 7, 0):
            with self.subTest(budget=budget):
                preview = self.invoke("trace_read", **options, preview_chars=budget)
                self.assertIn(self.bodies[0], self.full_text)
                if budget == 0:
                    self.assertIn(self.last_buffer, preview)
                    self.assertNotIn(self.bodies[0], preview)
                else:
                    self.assertIn(self.last_buffer, preview)
        for control, value in (("output", "compact"), ("max_chars", 7), ("cursor", "c1"), ("inspect", True)):
            error = self.invoke("trace_read", **options, **{control: value}, error=True)
            self.assertIn(control, error)

    def test_buffer_has_index_exact_text_and_durable_file(self):
        self.open_source()
        receipt = self.invoke("trace_read", targets=[{"event": "S/e1", "field": "text"}], pair=False)
        match = re.search(r"\b(buf_[a-zA-Z0-9]+)\b", receipt)
        self.assertIsNotNone(match, receipt)
        managed = self.state.buffer_manager.resolve_for_read(self.state, match.group(1))
        self.assertIn("## INDEX", managed.text)
        self.assertIn(self.bodies[0], managed.text)
        self.assertIn("\r\n", managed.text)
        self.assertTrue(any(p.read_bytes() == managed.text.encode("utf-8")
                            for p in (self.root / "study" / "results").glob("*.txt")))
        item = self.handle(receipt, "B")
        self.assertIn(self.bodies[0], self.invoke("trace_read", targets=[item], pair=False, line_context=0))

    def test_zero_preview_read_remains_accessible_and_manager_is_required(self):
        self.open_source()
        preview = self.invoke("trace_read", targets=["S/e1", "S/e2"], pair=False, preview_chars=0)
        self.assertIn(self.last_buffer, preview)
        self.assertNotIn(self.bodies[0], preview)
        self.assertIn(self.bodies[0], self.full_text)
        self.assertIn("receipt alpha", self.full_text)
        del self.state.buffer_manager
        self.assertIn("BufferManager", self.invoke("trace_read", targets=["S/e1"], error=True))


if __name__ == "__main__":
    unittest.main()
