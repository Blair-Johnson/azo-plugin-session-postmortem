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

    @unittest.skipUnless(ZOO_AVAILABLE, "feature registration requires the Agent Zoo host")
    def test_native_schema_publication_and_registration(self):
        features = []
        class Builder:
            def add(self, feature):
                features.append(feature)
        native.register_features(Builder(), session=None, config={})
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].name, "session_postmortem")
        self.assertIsInstance(features[0].components[0], native.SessionPostmortemSkill)
        self.assertEqual(list(native.TOOLS), features[0].components[1:])
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
        self.assertEqual(set(self.state.tool_schemas), {"trace_open", "trace_query", "trace_search", "trace_read", "trace_edit", "trace_cooccur"})
        props = native.trace_query.schema["function"]["parameters"]["properties"]
        self.assertEqual(props["params"]["anyOf"][0]["type"], "object")
        self.assertEqual(set(props["sample"]["anyOf"][0]["properties"]), {"n", "by", "seed"})
        props = native.trace_read.schema["function"]["parameters"]["properties"]
        targets = props["targets"]["anyOf"][0]["items"]["anyOf"]
        self.assertEqual(targets[0], {"type": "string"})
        self.assertEqual(len(targets), 8)
        self.assertTrue(all(v.get("properties") for v in targets[1:]))
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

    def test_inspection_is_native_and_needs_no_buffer_manager(self):
        self.open_source()
        refs = self.event_refs()
        del self.state.buffer_manager
        result = self.invoke("trace_read", targets=refs, pair=False, inspect=True, max_chars=5)
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(result["partially_delivered_rows"], 1)
        self.assertEqual(result["fully_deferred_rows"], 1)
        self.assertNotIn("blocks", result)
        self.assertNotIn("buffer", result)
        self.assertNotIn("Inspect naïve records", json.dumps(result, ensure_ascii=False))
        self.assertFalse((self.root / "study" / "packets").exists())
        selected = self.invoke("trace_read", targets=[{"result": result["plan_result"], "rows": [1]}],
                               pair=False, line_context=0)
        self.assertEqual(selected["blocks"][0]["text"], "The record assumption changed.")
        self.assertFalse(hasattr(self.state, "buffer_manager"))

    def test_inspection_null_defaults_and_invalid_combinations(self):
        self.open_source()
        ref = self.event_refs()[0]
        normal = self.invoke("trace_read", targets=[ref], pair=False)
        nullable = self.invoke("trace_read", targets=[ref], pair=False, inspect=None)
        explicit = self.invoke("trace_read", targets=[ref], pair=False, inspect=False)
        self.assertEqual(normal["blocks"], nullable["blocks"])
        self.assertEqual(normal["blocks"], explicit["blocks"])
        error = self.invoke("trace_read", targets=[ref], inspect=True,
                            output="buffer", expect_error=True)
        self.assertIn("inline", error)
        self.assertFalse((self.root / "study" / "packets").exists())
        first = self.invoke("trace_read", targets=[ref], max_chars=5)
        error = self.invoke("trace_read", cursor=first["next_cursor"], inspect=True,
                            expect_error=True)
        self.assertIn("inspect", error)
        rest = self.invoke("trace_read", cursor=first["next_cursor"], inspect=None)
        self.assertIsNone(rest["next_cursor"])

    def test_bundled_skill_registration_and_activation(self):
        from agent_utils.skills import SkillRegistry

        component = native.SessionPostmortemSkill()
        self.assertIs(component(self.state), self.state)
        registry = SkillRegistry(paths=[])
        self.state.skill_registry = registry
        with patch.object(registry, "add_sources", wraps=registry.add_sources) as add:
            self.assertIs(component(self.state), self.state)
            component(self.state)
            self.assertEqual(add.call_count, 1)
        skill, _ = registry.resolve("session-postmortem")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.source_scope, "bundled")
        self.assertIn("session-postmortem", registry.list_skills())
        activated = registry.format_activation(skill)
        self.assertIn("trace_read", activated)
        self.assertIn("inspect", activated)
        replacement = SkillRegistry(paths=[])
        self.state.skill_registry = replacement
        component(self.state)
        self.assertIsNotNone(replacement.resolve("session-postmortem")[0])

    def test_read_continuation_preserves_engine_omission_rules(self):
        self.open_source()
        refs = self.event_refs()
        first = self.invoke("trace_read", targets=refs, max_chars=5, output="buffer")
        self.assertIsNotNone(first["next_cursor"])
        second = self.invoke("trace_read", cursor=first["next_cursor"], max_chars=10, output="buffer")
        self.assertIn(second["buffer"], self.state.buffer_manager)
        self.assertNotEqual(first["buffer"], second["buffer"])

    def test_read_recovery_errors_and_null_continuation(self):
        self.open_source()
        ref = self.event_refs()[0]
        whole = {"event": ref, "field": "text"}
        error = self.invoke("trace_read", targets=[{**whole, "lines": [1, 3]}], expect_error=True)
        for detail in (ref, "field='text'", "requested=[1, 3]", "available line count=2",
                       "omit lines to read the whole field"):
            self.assertIn(detail, error)
        first = self.invoke("trace_read", targets=[whole], pair=False, max_chars=5)
        cursor = first["next_cursor"]
        database = self.root / "study" / "trace.sqlite3"
        snapshot = database.read_bytes()
        error = self.invoke("trace_read", cursor=cursor, before=0, after=0, pair=True,
                            line_context=10, projection="content", expect_error=True)
        self.assertIn("arguments: after, before, line_context, pair, projection", error)
        self.assertIn("allowed arguments with cursor: cursor, max_chars, output, workspace", error)
        self.assertEqual(database.read_bytes(), snapshot)
        second = self.invoke("trace_read", cursor=cursor, targets=None, before=None, after=None,
                             pair=None, line_context=None, projection=None, max_chars=1000)
        text = "".join(b["text"] for page in (first, second) for b in page["blocks"])
        self.assertEqual(text, "Inspect naïve records.\r\nKeep evidence exact.\n")
        self.assertIsNone(second["next_cursor"])

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
        card = selected["rows"][0]
        self.assertEqual(card["title"], "Keep title")
        self.assertEqual(card["note"], "")
        self.assertEqual(json.loads(card["tags"]), [])
        self.assertEqual(card["pinned"], 0)
        self.assertEqual(json.loads(card["fields"]), {"keep": {"nested": [1, False, None]}, "new": 2})
        self.assertTrue(self.invoke("trace_read", targets=[{"result": selected["result"], "rows": [0]}])["blocks"])
        self.assertTrue(self.invoke("trace_search", scope={"refs": [anchor]}, query="Inspect")["hits"]["source"])
        self.invoke("trace_edit", put=[{"id": "a", "fields": None}], unlink=["edge"], delete=["b"])
        card = self.invoke("trace_query", sql="SELECT fields FROM map_entries WHERE id=:entry",
                           params={"entry": "a"})["rows"][0]
        self.assertEqual(json.loads(card["fields"]), {})
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
                assert len(state.tool_schemas) == 6
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



@unittest.skipUnless(HOST_AVAILABLE, "agent_utils is not installed; run in the Agent Zoo host environment")
class NativeSchemaTests(unittest.TestCase):
    def parameters(self, name):
        return getattr(native, name).schema["function"]["parameters"]

    def structured(self, name, parameter):
        schema = self.parameters(name)["properties"][parameter]
        self.assertEqual(schema["anyOf"][1], {"type": "null"})
        return schema["anyOf"][0]

    def test_all_operation_parameters_are_model_visible(self):
        expected = {
            "trace_open": "workspace sessions descendants refresh page_size cursor",
            "trace_query": "workspace sql params saved sample page_size max_chars cursor save_as",
            "trace_search": "workspace scope query mode on collapse top_k page_size max_chars saved cursor save_as projection include_references",
            "trace_read": "workspace targets before after pair line_context max_chars output cursor projection inspect",
            "trace_edit": "workspace put delete link unlink",
            "trace_cooccur": "workspace seed against page_size max_chars cursor saved save_as",
        }
        for name, names in expected.items():
            with self.subTest(tool=name):
                schema = self.parameters(name)
                self.assertEqual(set(schema["properties"]), set(names.split()))
                self.assertEqual(schema["required"], ["workspace"])
        patch = self.structured("trace_edit", "put")["items"]
        self.assertEqual(set(patch["properties"]), {
            "id", "kind", "title", "note", "anchors", "tags", "fields", "pinned",
            "details_path", "add_anchors", "remove_anchors",
        })
        self.assertEqual(patch["required"], ["id"])
        self.assertFalse(patch["additionalProperties"])
        self.assertEqual(patch["properties"]["kind"]["enum"], ["episode", "note"])
        for name in ("note", "tags", "fields", "pinned", "details_path"):
            self.assertIn({"type": "null"}, patch["properties"][name]["anyOf"])
        self.assertTrue(patch["properties"]["fields"]["anyOf"][0]["additionalProperties"])
        relation = self.structured("trace_edit", "link")["items"]
        self.assertEqual(set(relation["properties"]), {"id", "from", "to", "label", "note"})
        self.assertEqual(relation["required"], ["id", "from", "to", "label"])
        self.assertFalse(relation["additionalProperties"])
        self.assertEqual(set(self.structured("trace_query", "sample")["properties"]), {"n", "by", "seed"})
        self.assertEqual(self.structured("trace_query", "sample")["required"], ["n"])
        self.assertEqual(self.structured("trace_query", "params")["additionalProperties"]["type"],
                         ["string", "number", "boolean", "null"])

    def test_nested_addresses_scopes_and_enums(self):
        expected = [{"event", "field", "lines"}, {"object", "lines"}, {"session", "events"},
                    {"result", "rows"}, {"event", "field"}, {"object"}, {"entry", "part"}]
        target = self.structured("trace_read", "targets")["items"]
        anchors = self.structured("trace_edit", "put")["items"]["properties"]
        scopes = self.structured("trace_search", "scope")["anyOf"]
        self.assertEqual([set(v["properties"]) for v in scopes],
                         [{"workspace"}, {"sessions"}, {"entries"}, {"refs"}, {"result"}])
        self.assertTrue(all(v["required"] == list(v["properties"]) and not v["additionalProperties"] for v in scopes))
        self.assertEqual(scopes[0]["properties"]["workspace"]["enum"], [True])
        for schema in [target, scopes[3]["properties"]["refs"]["items"],
                       *(anchors[name]["items"] for name in ("anchors", "add_anchors", "remove_anchors"))]:
            variants = schema["anyOf"]
            self.assertEqual(variants[0], {"type": "string"})
            self.assertEqual([set(v["properties"]) for v in variants[1:]], expected)
            self.assertTrue(all(v["required"] and not v["additionalProperties"] for v in variants[1:]))
            lines = variants[1]["properties"]["lines"]
            self.assertEqual((lines["minItems"], lines["maxItems"]), (2, 2))
            self.assertEqual(lines["items"], {"type": "integer", "minimum": 1})
            self.assertEqual(variants[4]["properties"]["rows"]["items"]["minimum"], 0)
        self.assertEqual(target["anyOf"][-1]["properties"]["part"]["enum"], ["card", "anchors"])
        self.assertEqual(anchors["anchors"]["items"]["anyOf"][-1]["properties"]["part"]["enum"], ["anchors"])
        for name, parameter, values in (
            ("trace_search", "mode", ["terms", "literal", "regex"]),
            ("trace_search", "on", ["source", "map", "both"]),
            ("trace_search", "collapse", ["none", "exact"]),
            ("trace_search", "projection", ["content", "all"]),
            ("trace_read", "projection", ["content", "all"]),
            ("trace_read", "output", ["inline", "compact", "buffer"]),
        ):
            self.assertEqual(self.structured(name, parameter)["enum"], values)

    @unittest.skipUnless(ZOO_AVAILABLE, "provider conversion requires the Agent Zoo host")
    def test_actual_host_provider_conversion_preserves_nested_schemas(self):
        from agents.llm import _scrub_fn_tool_schemas, _tools_for_responses
        state = State()
        state.tool_schemas = {}
        for component in native.TOOLS:
            component(state)
        published = [value["schema"] for value in state.tool_schemas.values()]
        before = json.dumps(published, sort_keys=True)
        converted = _tools_for_responses(_scrub_fn_tool_schemas(published))
        self.assertEqual(len(converted), 6)
        for schema in converted:
            self.assertEqual(schema["parameters"], self.parameters(schema["name"]))
        self.assertEqual(json.dumps(published, sort_keys=True), before)

    def test_whole_targets_survive_all_properties_required(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is not installed; no dependency installation performed")
        target = self.structured("trace_read", "targets")["items"]
        original = json.dumps(target, sort_keys=True)
        strengthened = json.loads(original)
        for branch in strengthened["anyOf"]:
            if branch.get("type") == "object":
                branch["required"] = list(branch["properties"])
        validator = Draft202012Validator(strengthened)
        for address in ({"event": "S/e1", "field": "text"}, {"object": "S/o1"},
                        {"event": "S/e1", "field": "text", "lines": [1, 2]},
                        {"object": "S/o1", "lines": [1, 2]},
                        {"session": "S", "events": [1, 2]},
                        {"result": "r1", "rows": [0]}, {"entry": "a", "part": "card"}):
            with self.subTest(address=address):
                validator.validate(address)
        self.assertEqual(json.dumps(target, sort_keys=True), original)
        props = self.parameters("trace_read")["properties"]
        for name in ("targets", "before", "after", "pair", "line_context", "projection", "inspect"):
            self.assertIn("null", props[name]["description"])
            self.assertIn("cursor", props[name]["description"])

    def test_json_schema_accepts_real_payloads_and_rejects_empty_objects(self):
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
            *( ("trace_search", {"scope": scope}) for scope in (
                {"workspace": True}, {"sessions": ["S"]}, {"entries": ["a"]},
                {"refs": addresses}, {"result": "r1"})),
        ]
        for name, args in valid:
            with self.subTest(tool=name, payload=args):
                validators[name].validate({"workspace": "study", **args})
        for name, validator in validators.items():
            validator.validate({"workspace": "study"})
            validator.validate({key: "study" if key == "workspace" else None
                                for key in self.parameters(name)["properties"]})
        invalid = [
            ("trace_edit", {"put": [{}]}), ("trace_edit", {"link": [{}]}),
            ("trace_edit", {"put": [{"id": "a", "kind": "finding"}]}),
            ("trace_edit", {"put": [{"id": "a", "title": None}]}),
            ("trace_edit", {"put": [{"id": "a", "typo": "value"}]}),
            ("trace_edit", {"put": [{"id": "a", "anchors": [{"entry": "b", "part": "card"}]}]}),
            ("trace_read", {"targets": [{}]}),
            ("trace_read", {"targets": [{"event": "S/e1", "object": "S/o1"}]}),
            ("trace_read", {"targets": [{"event": "S/e1", "lines": [0, 2]}]}),
            ("trace_read", {"targets": [{"session": "S", "events": [1]}]}),
            ("trace_read", {"targets": [{"result": "r1", "rows": [-1]}]}),
            ("trace_query", {"sample": {}}), ("trace_query", {"sample": {"n": -1}}),
            ("trace_query", {"params": {"not-a-scalar": {"nested": True}}}),
            ("trace_search", {"scope": {"workspace": False}}),
            ("trace_search", {"scope": {"sessions": ["S"], "entries": ["a"]}}),
            ("trace_search", {"mode": "similar"}), ("trace_read", {"output": "file"}),
        ]
        for name, args in invalid:
            with self.subTest(tool=name, invalid=args):
                self.assertFalse(validators[name].is_valid({"workspace": "study", **args}))

if __name__ == "__main__":
    unittest.main()
