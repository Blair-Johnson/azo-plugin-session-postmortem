"""Host integration tests; portable core-only installs skip these explicitly."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch


HOST_AVAILABLE = importlib.util.find_spec("agent_utils") is not None
ZOO_AVAILABLE = importlib.util.find_spec("agent_zoo") is not None
if HOST_AVAILABLE:
    from agent_utils import State, ToolCall
    from agent_utils.files.buffer_manager import BufferManager
    from azo_session_postmortem import native


@unittest.skipUnless(HOST_AVAILABLE, "agent_utils is not installed; run in the Agent Zoo host environment")
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = State()
        self.state.session_cwd = str(self.root)
        self.state.tool_schemas = {}
        self.state.buffer_manager = BufferManager()
        self.source = self.root / "session.json"
        self.source.write_text(json.dumps({
            "format_version": 2,
            "meta": {"session_id": "S"},
            "entries": [
                {"index": 0, "step": 0, "messages": [
                    {"role": "user", "content": "Inspect naïve records.\r\nKeep evidence exact.\n"}]},
                {"index": 1, "step": 1, "messages": [
                    {"role": "assistant", "content": "The record assumption changed."}]},
            ],
        }), encoding="utf-8")

    def invoke(self, name, *, expect_error=False, **arguments):
        arguments = {"workspace": "study", **arguments}
        call = ToolCall(id="native-test", name=name, raw_args=json.dumps(arguments), parsed_args=arguments)
        self.state.pending_tool_calls = [call]
        component = getattr(native, name)
        self.assertIs(component(self.state), self.state)
        self.assertIsNotNone(call.result)
        self.assertEqual(call.error, expect_error, call.result)
        self.assertIn(name, self.state.tool_schemas)
        return call.result if expect_error else json.loads(call.result)

    def open_source(self):
        return self.invoke("trace_open", sessions=[str(self.source)], descendants=False)

    def event_refs(self):
        return [row["ref"] for row in self.invoke("trace_query", sql="SELECT ref FROM events ORDER BY seq")["rows"]]

    def test_native_schema_publication_and_registration(self):
        features = []
        class Builder:
            def add(self, feature):
                features.append(feature)
        native.register_features(Builder(), session=None, config={})
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].name, "session_postmortem")
        self.assertEqual(list(native.TOOLS), features[0].components)
        self.state.pending_tool_calls = []
        for component in native.TOOLS:
            component(self.state)
            published = self.state.tool_schemas[component.name]
            self.assertTrue(published["available"])
            parameters = published["schema"]["function"]["parameters"]
            self.assertEqual(parameters["required"], ["workspace"])
            self.assertNotIn("state", parameters["properties"])
            self.assertNotIn("arguments", parameters["properties"])
            self.assertTrue(all(p.get("description") for p in parameters["properties"].values()))
            self.assertTrue({"session_cwd", "terminal_cwd"} <= component.optional_reads)
        self.assertEqual(set(self.state.tool_schemas), {"trace_open", "trace_query", "trace_search", "trace_read", "trace_edit"})
        props = native.trace_query.schema["function"]["parameters"]["properties"]
        self.assertEqual(props["params"]["type"], ["object", "null"])
        self.assertEqual(props["sample"]["type"], ["object", "null"])
        props = native.trace_read.schema["function"]["parameters"]["properties"]
        self.assertEqual(props["targets"]["items"]["anyOf"], [{"type": "string"}, {"type": "object"}])
        self.assertEqual(props["pair"]["type"], ["boolean", "null"])
        self.assertEqual(props["max_chars"]["type"], ["integer", "null"])
        self.assertIn("buffer_manager", native.trace_read.optional_reads)
        self.assertIn("buffer_manager", native.trace_read.writes)

    def test_workspace_uses_session_cwd_not_process_cwd(self):
        other = self.root / "other"
        other.mkdir()
        old = Path.cwd()
        try:
            os.chdir(other)
            opened = self.open_source()
        finally:
            os.chdir(old)
        self.assertEqual(opened["workspace"], str(self.root / "study"))
        self.assertIn("managed buffer", opened["capabilities"]["buffer_output"])
        self.assertTrue((self.root / "study" / "trace.sqlite3").is_file())
        self.assertFalse((other / "study").exists())

    def test_relative_source_uses_session_cwd(self):
        other = self.root / "unrelated"
        other.mkdir()
        old = Path.cwd()
        try:
            os.chdir(other)
            opened = self.invoke("trace_open", sessions=["session.json"], descendants=False)
        finally:
            os.chdir(old)
        self.assertEqual(opened["sources"][0]["id"], "S")
        self.assertTrue(Path(opened["capabilities"]["query_schema"]).is_file())

    def test_legacy_terminal_cwd_is_supported(self):
        del self.state.session_cwd
        self.state.terminal_cwd = str(self.root)
        self.assertEqual(self.open_source()["workspace"], str(self.root / "study"))

    def test_blank_workspace_rejected_before_engine(self):
        with patch.object(native.engine, "dispatch") as dispatch:
            self.invoke("trace_open", workspace="", expect_error=True)
            dispatch.assert_not_called()

    def test_buffer_is_managed_and_packet_text_is_exact(self):
        self.open_source()
        refs = self.event_refs()
        result = self.invoke("trace_read", targets=[refs[0]], output="buffer", pair=False, line_context=0)
        self.assertNotEqual(result["buffer"], result["buffer_path"])
        managed = self.state.buffer_manager.resolve_for_read(self.state, result["buffer"])
        packet = Path(result["buffer_path"]).read_bytes().decode("utf-8")
        self.assertEqual(managed.text, packet)
        self.assertIn("naïve", managed.text)
        self.assertIn("\r\n", managed.text)
        self.assertEqual(len(managed.text), result["packet_chars"])
        self.assertFalse(self.state.entries)

    def test_inline_does_not_require_or_create_a_manager(self):
        self.open_source()
        refs = self.event_refs()
        del self.state.buffer_manager
        result = self.invoke("trace_read", targets=[refs[0]], output="inline")
        self.assertIn("blocks", result)
        self.assertFalse(hasattr(self.state, "buffer_manager"))

    def test_missing_buffer_manager_has_no_dispatch_or_disk_side_effects(self):
        self.open_source()
        refs = self.event_refs()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        del self.state.buffer_manager
        with patch.object(native.engine, "dispatch", wraps=native.engine.dispatch) as dispatch:
            error = self.invoke("trace_read", targets=[refs[0]], output="buffer", expect_error=True)
            dispatch.assert_not_called()
        self.assertIn("BufferManager", error)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.state.entries)

    def test_read_continuation_preserves_engine_omission_rules(self):
        self.open_source()
        refs = self.event_refs()
        first = self.invoke("trace_read", targets=refs, max_chars=5, output="buffer")
        self.assertIsNotNone(first["next_cursor"])
        second = self.invoke("trace_read", cursor=first["next_cursor"], max_chars=10, output="buffer")
        self.assertIn(second["buffer"], self.state.buffer_manager)
        self.assertNotEqual(first["buffer"], second["buffer"])

    def test_saved_search_and_query_cursor_use_engine_defaults(self):
        self.open_source()
        found = self.invoke("trace_search", query="record", mode="literal", save_as="records")
        saved = self.invoke("trace_search", saved="records")
        self.assertEqual(found["matched_total"], saved["matched_total"])
        first = self.invoke("trace_query", sql="SELECT ref FROM events ORDER BY seq", page_size=1)
        second = self.invoke("trace_query", cursor=first["next_cursor"], page_size=1)
        self.assertNotEqual(first["rows"], second["rows"])

    def test_structured_map_edits_and_search_do_not_change_transcript(self):
        self.open_source()
        refs = self.event_refs()
        edited = self.invoke("trace_edit", put=[{
            "id": "records", "kind": "episode", "title": "Changed record assumption",
            "note": "Caller-authored semantic interpretation", "anchors": [refs[1]],
        }])
        self.assertIn("records", edited["created"])
        found = self.invoke("trace_search", query="semantic interpretation", on="map", mode="literal")
        self.assertTrue(found["hits"]["map"])
        self.assertFalse(found["hits"]["source"])
        self.assertFalse(self.state.entries)

    @unittest.skipUnless(ZOO_AVAILABLE, "agent_zoo is not installed; fresh host-loader test requires it")
    @unittest.skipUnless(ZOO_AVAILABLE, "agent_zoo is not installed; loader test needs the full host")
    def test_fresh_host_loader_with_symlink_and_unrelated_cwd(self):
        shim = Path(__file__).resolve().parents[1] / "src" / "session_postmortem.py"
        common = self.root / "plugins" / "common"
        common.mkdir(parents=True)
        installed = common / "session_postmortem.py"
        try:
            installed.symlink_to(shim)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        script = textwrap.dedent('''
            import json
            from pathlib import Path
            import sys
            from agent_utils import State
            from agent_zoo.plugins import _apply_file_plugin
            before = list(sys.path)
            class Builder:
                def __init__(self): self.features = []
                def add(self, feature): self.features.append(feature)
            for scope in ("default", "rlm"):
                builder = Builder()
                _apply_file_plugin(Path(sys.argv[1]), builder=builder, session=None, config={})
                assert len(builder.features) == 1, (scope, builder.features)
                state = State()
                state.tool_schemas = {}
                for component in builder.features[0].components:
                    component(state)
                assert len(state.tool_schemas) == 5
                assert all(p["available"] for p in state.tool_schemas.values())
            assert sys.path == before
            assert "azo_session_postmortem" not in sys.modules
            print(json.dumps(sorted(state.tool_schemas)))
        ''')
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(installed)],
            cwd=self.root, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), sorted(component.name for component in native.TOOLS))


if __name__ == "__main__":
    unittest.main()
