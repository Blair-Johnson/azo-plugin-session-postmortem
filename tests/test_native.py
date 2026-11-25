"""Host integration tests; portable core-only installs skip these explicitly."""

import importlib.util
import json
import os
import re
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
    from agent_utils.core.events import IoCompleted, IoFailed, RenderRequested, RunToolCall, SessionRestored, SessionStarted, ToolCallFinished
    from agent_utils.core.io import SubmitOp
    from agent_utils.core.session import Session
    from agent_utils.core.state import State, ToolCall
    from agent_utils.features.buffers.manager import BufferManager
    from azo_session_postmortem import native


@unittest.skipUnless(HOST_AVAILABLE, "agent_utils is not installed; run in the Agent Zoo host environment")
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = State(cwd=str(self.root))
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
        emitted = []
        with patch.object(native.cache_render, "render_preview", wraps=native.cache_render.render_preview) as rendered:
            component.handle(RunToolCall(call), self.state, emitted.append)
            for eff in self.state.io.take_outbox():
                if isinstance(eff, SubmitOp) and isinstance(eff.request, native.PostmortemDispatchRequest):
                    try:
                        value = native.PostmortemDriver.execute_request(eff.request)
                    except Exception as exc:
                        component.handle(IoFailed(op_id=eff.op_id, error=exc), self.state, emitted.append)
                    else:
                        component.handle(IoCompleted(op_id=eff.op_id, value=value), self.state, emitted.append)
        self.assertEqual(len(emitted), 1)
        self.assertIsInstance(emitted[0], ToolCallFinished)
        self.assertIs(emitted[0].tool_call, call)
        self.assertIsNotNone(call.result)
        self.assertEqual(call.error, expect_error, call.result)
        if expect_error:
            return call.result
        self.last_preview = call.result
        document, self.last_buffer, self.last_budget = rendered.call_args.args
        self.last_text = (self.state.buffer_manager.resolve_for_read(self.state, self.last_buffer).text
                          if self.last_buffer else "")
        return document.get("ack", document)

    def open_source(self):
        return self.invoke("trace_open", sessions=[str(self.source)], descendants=False)

    def event_refs(self):
        document = self.invoke("trace_query", sql="SELECT ref FROM events ORDER BY seq")
        return [re.search(r"S/e\d+", block["text"]).group() for block in document["blocks"]]

    @unittest.skipUnless(ZOO_AVAILABLE, "feature registration requires the Agent Zoo host")
    def test_native_schema_publication_and_registration(self):
        from agent_zoo.pipelines.default import build_default_builder
        from agent_zoo.pipelines.rlm import build_default_rlm_builder, build_readonly_rlm_builder

        features = []
        class Builder:
            def add(self, feature):
                features.append(feature)
        native.register_features(Builder(), config={})
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].name, "session_postmortem")
        self.assertIsInstance(features[0].components[0], native.SessionPostmortemSkill)
        self.assertEqual(list(native.TOOLS), features[0].components[1:])
        self.assertEqual(features[0].drivers, [native.PostmortemDriver])

        # Compose onto real host builders and verify schema + skill publication + Sans-I/O cleanliness.
        from agent_zoo.testing.sans_io_guard import assert_pipeline_sans_io, audit_module_sans_io
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(audit_module_sans_io(repo_root / "src" / "session_postmortem.py"), [])
        self.assertEqual(audit_module_sans_io(repo_root / "azo_session_postmortem" / "native.py"), [])

        for make_builder in (build_default_builder, build_default_rlm_builder, build_readonly_rlm_builder):
            session = Session(state=State(cwd=str(self.root)))
            builder = make_builder(session, config={}, include_plugins=False, include_llm_drivers=False)
            native.register_features(builder, config={})
            pipeline = builder.build()
            pipeline.init_state(session.state)
            assert_pipeline_sans_io(
                pipeline,
                session=session,
                events=(SessionStarted(), SessionRestored(), RenderRequested()),
            )
            for component in native.TOOLS:
                published = session.state.tool_schemas[component.name]
                self.assertTrue(published["available"])
                parameters = published["schema"]["function"]["parameters"]
                self.assertEqual(parameters["required"], ["workspace", "targets"] if component.name == "trace_read" else ["workspace"])
                self.assertNotIn("state", parameters["properties"])
                self.assertNotIn("arguments", parameters["properties"])
                self.assertTrue(all(p.get("description") for p in parameters["properties"].values()))
                self.assertTrue({"cwd", "session_cwd", "terminal_cwd"} <= component.optional_reads)
            self.assertIsNotNone(session.state.skill_registry.resolve("session-postmortem")[0])

        props = native.trace_query.schema["function"]["parameters"]["properties"]
        self.assertEqual(props["params"]["type"], ["object", "null"])
        self.assertEqual(props["sample"]["type"], ["object", "null"])
        props = native.trace_read.schema["function"]["parameters"]["properties"]
        self.assertEqual(props["targets"]["type"], "array")
        self.assertEqual(props["targets"]["items"]["type"], ["string", "object"])
        self.assertEqual(props["pair"]["type"], ["boolean", "null"])
        self.assertEqual(props["preview_chars"]["type"], ["integer", "null"])
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
        self.assertTrue(any("S" in b["text"] for b in opened["blocks"]))

    def test_legacy_terminal_cwd_is_supported(self):
        self.state.cwd = ""
        self.state.terminal_cwd = str(self.root)
        self.assertEqual(self.open_source()["workspace"], str(self.root / "study"))

    def test_blank_workspace_rejected_before_engine(self):
        with patch.object(native.cache, "dispatch") as dispatch:
            self.invoke("trace_open", workspace="", expect_error=True)
            dispatch.assert_not_called()

    def test_buffer_is_managed_and_packet_text_is_exact(self):
        self.open_source()
        refs = self.event_refs()
        result = self.invoke("trace_read", targets=[refs[0]], pair=False, line_context=0)
        self.assertTrue(self.last_buffer.startswith("buf_"))
        files = list((self.root / "study" / "results").glob("*.txt"))
        self.assertTrue(any(p.read_bytes().decode() == self.last_text for p in files))
        self.assertIn("Inspect naïve records.\r\nKeep evidence exact.\n", self.last_text)
        self.assertEqual(result["blocks"][0]["text"], "Inspect naïve records.\r\nKeep evidence exact.\n")
        self.assertFalse(self.state.entries)

    def test_edit_does_not_require_or_create_a_manager(self):
        self.open_source()
        del self.state.buffer_manager
        result = self.invoke("trace_edit", put=[{"id": "note", "kind": "note", "title": "Note"}])
        self.assertIn("note", result["created"])
        self.assertFalse(hasattr(self.state, "buffer_manager"))

    def test_missing_buffer_manager_has_no_dispatch_or_disk_side_effects(self):
        self.open_source()
        refs = self.event_refs()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        del self.state.buffer_manager
        with patch.object(native.cache, "dispatch", wraps=native.cache.dispatch) as dispatch:
            error = self.invoke("trace_read", targets=[refs[0]], expect_error=True)
            dispatch.assert_not_called()
        self.assertIn("BufferManager", error)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.state.entries)

    def test_zero_preview_still_delivers_complete_evidence(self):
        self.open_source()
        refs = self.event_refs()
        result = self.invoke("trace_read", targets=refs, pair=False, preview_chars=0)
        self.assertEqual(len(result["blocks"]), 2)
        self.assertIn("Inspect naïve records", self.last_text)
        self.assertIn("The record assumption changed.", self.last_text)
        self.assertIn(self.last_buffer, self.last_preview)
        self.assertNotIn("Inspect naïve records", self.last_preview)

    def test_null_read_defaults_and_rejected_removed_controls(self):
        self.open_source()
        ref = self.event_refs()[0]
        normal = self.invoke("trace_read", targets=[ref], pair=False)
        nullable = self.invoke("trace_read", targets=[ref], pair=False, preview_chars=None)
        self.assertEqual([b["text"] for b in normal["blocks"]], [b["text"] for b in nullable["blocks"]])
        for key, value in (("inspect", True), ("cursor", "c1"), ("output", "buffer"), ("max_chars", 5)):
            error = self.invoke("trace_read", targets=[ref], **{key: value}, expect_error=True)
            self.assertIn(key, error)

    def test_bundled_skill_registration_and_activation(self):
        from agent_utils.features.skills import SkillRegistry

        component = native.SessionPostmortemSkill()
        component.handle(RenderRequested(), self.state, lambda _ev: None)
        registry = SkillRegistry()
        self.state.skill_registry = registry
        with patch.object(registry, "add_sources", wraps=registry.add_sources) as add:
            component.handle(RenderRequested(), self.state, lambda _ev: None)
            component.handle(RenderRequested(), self.state, lambda _ev: None)
            self.assertEqual(add.call_count, 1)
        skill, _ = registry.resolve("session-postmortem")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.source_scope, "bundled")
        self.assertIn("session-postmortem", registry.list_skills())
        activated = registry.format_activation(skill)
        self.assertIn("trace_read", activated)
        self.assertIn("trace_search", activated)
        replacement = SkillRegistry()
        self.state.skill_registry = replacement
        component.handle(RenderRequested(), self.state, lambda _ev: None)
        self.assertIsNotNone(replacement.resolve("session-postmortem")[0])

        # Per-session isolation via state.runtime (shared component across two sessions).
        other_state = State(cwd=str(self.root))
        other_state.skill_registry = SkillRegistry()
        component.handle(SessionRestored(), other_state, lambda _ev: None)
        self.assertIsNotNone(other_state.skill_registry.resolve("session-postmortem")[0])

    def test_postmortem_driver_async_lifecycle(self):
        driver = native.PostmortemDriver()
        self.assertEqual(driver.durability, "commit")
        self.assertEqual(driver.request_types, (native.PostmortemDispatchRequest,))
        emitted = []
        driver.start(emitted.append)
        try:
            req = native.PostmortemDispatchRequest(
                tool_name="trace_open",
                arguments={"workspace": "driver_study", "sessions": ["session.json"], "descendants": False},
                cwd=str(self.root),
            )
            driver.submit("op-1", req)
            driver.drain(timeout_s=5.0)
            self.assertEqual(len(emitted), 1)
            self.assertIsInstance(emitted[0], IoCompleted)
            self.assertEqual(emitted[0].op_id, "op-1")
            self.assertEqual(emitted[0].value["document"]["operation"], "trace_open")
            self.assertEqual(driver.quiesce(policy="terminate"), [])
        finally:
            driver.close()

    def test_small_previews_keep_full_independent_managed_buffers(self):
        self.open_source()
        refs = self.event_refs()
        first = self.invoke("trace_read", targets=refs, preview_chars=5)
        first_buffer, first_text = self.last_buffer, self.last_text
        second = self.invoke("trace_read", targets=refs, preview_chars=10)
        self.assertIn(self.last_buffer, self.state.buffer_manager)
        self.assertNotEqual(first_buffer, self.last_buffer)
        self.assertEqual([b["text"] for b in first["blocks"]], [b["text"] for b in second["blocks"]])
        self.assertIn("The record assumption changed.", first_text)
        self.assertIn("The record assumption changed.", self.last_text)

    def test_read_recovery_errors_and_whole_field(self):
        self.open_source()
        ref = self.event_refs()[0]
        whole = {"event": ref, "field": "text"}
        error = self.invoke("trace_read", targets=[{**whole, "lines": [1, 3]}], expect_error=True)
        for detail in (ref, "field='text'", "requested=[1, 3]", "available line count=2",
                       "omit lines to read the whole field"):
            self.assertIn(detail, error)
        result = self.invoke("trace_read", targets=[whole], pair=False, preview_chars=5)
        self.assertEqual(result["blocks"][0]["text"], "Inspect naïve records.\r\nKeep evidence exact.\n")
        self.assertIn(result["blocks"][0]["text"], self.last_text)

    def test_saved_search_and_query_use_cache_defaults(self):
        self.open_source()
        found = self.invoke("trace_search", query="record", mode="literal", context_lines=0, save_as="records")
        saved = self.invoke("trace_search", saved="records")
        self.assertEqual([b["text"] for b in found["blocks"]], [b["text"] for b in saved["blocks"]])
        first = self.invoke("trace_query", sql="SELECT ref FROM events ORDER BY seq", save_as="events")
        second = self.invoke("trace_query", saved="events")
        self.assertEqual(len(first["blocks"]), 2)
        self.assertEqual([b["text"] for b in first["blocks"]], [b["text"] for b in second["blocks"]])

    def test_structured_map_edits_and_search_do_not_change_transcript(self):
        self.open_source()
        refs = self.event_refs()
        edited = self.invoke("trace_edit", put=[{
            "id": "records", "kind": "episode", "title": "Changed record assumption",
            "note": "Caller-authored semantic interpretation", "anchors": [refs[1]],
        }])
        self.assertIn("records", edited["created"])
        found = self.invoke("trace_search", query="semantic interpretation", on="map", mode="literal")
        self.assertTrue(found["blocks"])
        self.assertTrue(all(b["group"] == "map" for b in found["blocks"]))
        self.assertFalse(self.state.entries)


    def test_nested_arguments_and_partial_patch_semantics(self):
        self.open_source()
        refs = self.event_refs()
        anchor = {"event": refs[0], "field": "text", "lines": [1, 1]}
        self.invoke("trace_edit", put=[{
            "id": "a", "kind": "episode", "title": "Keep title", "anchors": [anchor],
            "note": "clear me", "tags": ["old"], "pinned": True,
            "fields": {"keep": {"nested": [1, False, None]}, "drop": "old"},
        }, {"id": "b", "kind": "note", "title": "Other"}], link=[{
            "id": "edge", "from": "a", "to": "b", "label": "related", "note": None,
        }])
        self.invoke("trace_edit", put=[{
            "id": "a", "note": None, "tags": None, "pinned": None,
            "details_path": None, "fields": {"drop": None, "new": 2},
            "remove_anchors": [anchor], "add_anchors": [{"session": "S", "events": [1, 1]}],
        }])
        selected = self.invoke("trace_query", sql="SELECT * FROM map_entries WHERE id=:entry",
                               params={"entry": "a"}, sample={"n": 1, "by": ["kind"], "seed": 7})
        card = dict(line.split(": ", 1) for line in selected["blocks"][0]["text"].splitlines())
        self.assertEqual(card["title"], "Keep title")
        self.assertEqual(json.loads(card["note"]), "")
        self.assertEqual(json.loads(card["tags"]), [])
        self.assertEqual(int(card["pinned"]), 0)
        self.assertEqual(json.loads(card["fields"]), {"keep": {"nested": [1, False, None]}, "new": 2})
        self.assertTrue(self.invoke("trace_read", targets=[{"result": selected["id"], "rows": [0]}])["blocks"])
        self.assertTrue(self.invoke("trace_search", scope={"refs": [anchor]}, query="Inspect")["blocks"])
        self.invoke("trace_edit", put=[{"id": "a", "fields": None}], unlink=["edge"], delete=["b"])
        card = self.invoke("trace_query", sql="SELECT fields FROM map_entries WHERE id=:entry",
                           params={"entry": "a"})["blocks"][0]
        self.assertEqual(card["text"], "fields: {}")
    @unittest.skipUnless(ZOO_AVAILABLE, "agent_zoo is not installed; fresh host-loader test requires it")
    def test_fresh_host_loader_with_symlink_and_unrelated_cwd(self):
        repo_root = Path(__file__).resolve().parents[1]
        shim = repo_root / "src" / "session_postmortem.py"
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
            from agent_utils.core.events import RenderRequested
            from agent_utils.core.session import Session
            from agent_utils.core.state import State
            from agent_zoo.pipelines.default import build_default_builder
            from agent_zoo.pipelines.rlm import build_default_rlm_builder
            from agent_zoo.plugins.loader import _apply_file_plugin
            before = list(sys.path)
            expected = {"trace_open", "trace_query", "trace_search", "trace_read", "trace_edit", "trace_cooccur"}
            for scope, make_builder in (("default", build_default_builder), ("rlm", build_default_rlm_builder)):
                session = Session(state=State())
                builder = make_builder(session, config={}, include_plugins=False, include_llm_drivers=False)
                _apply_file_plugin(Path(sys.argv[1]), builder=builder, session=session, config={})
                assert builder.has("session_postmortem"), scope
                pipeline = builder.build()
                pipeline.init_state(session.state)
                pipeline.step(session.state, RenderRequested())
                found = {name: session.state.tool_schemas[name] for name in expected}
                assert all(p["available"] for p in found.values()), (scope, found)
            shim_parent = str(Path(sys.argv[1]).resolve().parent)
            added_paths = [p for p in sys.path if p not in before]
            assert all(p == shim_parent for p in added_paths), (before, sys.path)
            assert "azo_session_postmortem" not in sys.modules
            print(json.dumps(sorted(expected)))
        ''')
        env = dict(os.environ)
        if env.get("PYTHONPATH"):
            parts = [
                p for p in env["PYTHONPATH"].split(os.pathsep)
                if p and Path(p).resolve() != repo_root
            ]
            env["PYTHONPATH"] = os.pathsep.join(parts)
        completed = subprocess.run(
            [sys.executable, "-P", "-B", "-c", script, str(installed)],
            cwd=self.root, env=env, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), sorted(component.name for component in native.TOOLS))



@unittest.skipUnless(HOST_AVAILABLE, "agent_utils is not installed; run in the Agent Zoo host environment")
class NativeSchemaTests(unittest.TestCase):
    def parameters(self, name):
        return getattr(native, name).schema["function"]["parameters"]

    def test_all_operation_parameters_are_model_visible(self):
        expected = {
            "trace_open": "workspace sessions descendants refresh preview_chars",
            "trace_query": "workspace sql params sample saved save_as preview_chars",
            "trace_search": "workspace query scope mode context_lines on collapse projection follow_object_links saved save_as preview_chars",
            "trace_read": "workspace targets before after pair line_context projection preview_chars",
            "trace_edit": "workspace put delete link unlink",
            "trace_cooccur": "workspace seed against saved save_as preview_chars",
        }
        for name, names in expected.items():
            with self.subTest(tool=name):
                schema = self.parameters(name)
                self.assertEqual(set(schema["properties"]), set(names.split()))
                self.assertEqual(schema["required"], ["workspace", "targets"] if name == "trace_read" else ["workspace"])
                self.assertTrue(all(p.get("description") for p in schema["properties"].values()))
        self.assertEqual(self.parameters("trace_edit")["properties"]["put"]["type"], ["array", "null"])
        self.assertEqual(self.parameters("trace_edit")["properties"]["put"]["items"], {"type": "object"})
        self.assertEqual(self.parameters("trace_edit")["properties"]["link"]["type"], ["array", "null"])
        self.assertEqual(self.parameters("trace_edit")["properties"]["link"]["items"], {"type": "object"})
        self.assertEqual(self.parameters("trace_query")["properties"]["sample"]["type"], ["object", "null"])
        self.assertEqual(self.parameters("trace_query")["properties"]["params"]["type"], ["object", "null"])
        self.assertEqual(self.parameters("trace_search")["properties"]["scope"]["type"], ["object", "null"])
        self.assertEqual(self.parameters("trace_read")["properties"]["targets"]["type"], "array")
        self.assertEqual(self.parameters("trace_read")["properties"]["targets"]["items"], {"type": ["string", "object"]})

    @unittest.skipUnless(ZOO_AVAILABLE, "provider conversion requires the Agent Zoo host")
    def test_actual_host_provider_conversion_preserves_schemas(self):
        from agent_zoo.llm.providers.common import _scrub_fn_tool_schemas
        from agent_zoo.llm.providers.openai_responses import _tools_for_responses

        published = [component.schema for component in native.TOOLS]
        before = json.dumps(published, sort_keys=True)
        converted = _tools_for_responses(_scrub_fn_tool_schemas(published))
        self.assertEqual(len(converted), 6)
        for schema in converted:
            self.assertEqual(schema["parameters"], self.parameters(schema["name"]))
        self.assertEqual(json.dumps(published, sort_keys=True), before)

    def test_json_schema_accepts_real_payloads_and_rejects_bad_top_level_types(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is not installed; no dependency installation performed")
        validators = {}
        for component in native.TOOLS:
            schema = self.parameters(component.name)
            Draft202012Validator.check_schema(schema)
            validators[component.name] = Draft202012Validator(schema)
        addresses = ["S/e1", {"event": "S/e1"},
                     {"event": "S/e1", "field": "text", "lines": [1, 2]},
                     {"object": "S/o1", "lines": [1, 1]}, {"session": "S", "events": [1, 2]},
                     {"result": "r1", "rows": [0, 3]}, {"entry": "a", "part": "anchors"}]
        valid = [
            ("trace_open", {"sessions": ["S"], "descendants": False}),
            ("trace_query", {"sql": "SELECT :name", "params": {"name": "a", "n": 2, "f": 1.5, "b": False, "nil": None},
                             "sample": {"n": 0, "by": ["kind"], "seed": 7}}),
            ("trace_read", {"targets": [*addresses, {"entry": "a"}, {"entry": "a", "part": "card"}]}),
            ("trace_edit", {"put": [{"id": "a"}, {"id": "b", "kind": "episode", "title": "B", "anchors": addresses,
                            "fields": {"anything": {"deep": [1, "two", None, {"flag": True}]}}}],
                            "link": [{"id": "edge", "from": "a", "to": "b", "label": "related", "note": None}],
                            "delete": ["old"], "unlink": ["old-edge"]}),
            ("trace_edit", {"put": [{"id": "a", "note": None, "tags": None, "fields": None,
                                     "pinned": None, "details_path": None}]}),
            ("trace_edit", {"put": [{"id": "a", "fields": {"delete-key": None},
                                     "add_anchors": addresses, "remove_anchors": addresses}]}),
            *(("trace_search", {"scope": scope}) for scope in (
                {"workspace": True}, {"sessions": ["S"]}, {"entries": ["a"]},
                {"refs": addresses}, {"result": "r1"}, {"items": ["S1-H001"]},
                {"result": "C1", "group": "unmatched"})),
        ]
        for name, args in valid:
            with self.subTest(tool=name, payload=args):
                validators[name].validate({"workspace": "study", **args})
        for name, validator in validators.items():
            validator.validate({"workspace": "study", **({"targets": ["S/e1"]} if name == "trace_read" else {})})
            validator.validate({key: "study" if key == "workspace" else ["S/e1"] if key == "targets" else None
                                for key in self.parameters(name)["properties"]})
        invalid = [
            ("trace_open", {"preview_chars": "bad"}),
            ("trace_search", {"scope": "workspace"}),
            ("trace_read", {"targets": [123]}),
            ("trace_edit", {"put": ["not-an-object"]}),
            ("trace_edit", {"link": ["not-an-object"]}),
            ("trace_cooccur", {"against": ["not-an-object"]}),
        ]
        for name, args in invalid:
            with self.subTest(tool=name, invalid=args):
                self.assertFalse(validators[name].is_valid({"workspace": "study", **args}))

if __name__ == "__main__":
    unittest.main()
