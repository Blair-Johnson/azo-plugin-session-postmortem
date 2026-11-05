"""Synthetic CLI/native integration for co-occurrence; no private traces."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


HOST_AVAILABLE = importlib.util.find_spec("agent_utils") is not None
ZOO_AVAILABLE = importlib.util.find_spec("agent_zoo") is not None
if HOST_AVAILABLE:
    from agent_utils import State, ToolCall
    from agent_utils.files.buffer_manager import BufferManager
    import test_native as native_fixtures
    from azo_session_postmortem import native


class SyntheticFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps({
            "format_version": 2, "meta": {"session_id": "S"},
            "entries": [{"index": i, "messages": [{"role": "assistant", "content": text}]}
                        for i, text in enumerate([
                            "candidate one", "receipt alpha", "candidate two",
                            "receipt beta", "candidate three",
                        ])],
        }), encoding="utf-8")


@unittest.skipUnless(HOST_AVAILABLE, "run in the Agent Zoo host environment")
class CooccurNativeTests(SyntheticFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.state = State()
        self.state.session_cwd = str(self.root)
        self.state.tool_schemas = {}
        self.state.buffer_manager = BufferManager()

    invoke = native_fixtures.NativeTests.invoke if HOST_AVAILABLE else None

    def selections(self):
        self.invoke("trace_open", sessions=["source.json"], descendants=False)
        seed = self.invoke("trace_search", query="candidate", mode="literal")
        witness = self.invoke("trace_search", query="receipt", mode="literal")
        return seed, witness

    def join_args(self):
        seed, witness = self.selections()
        return {"seed": {"result": seed["id"]}, "against": [{
            "name": "receipt", "selection": {"result": witness["id"]},
            "before": 0, "after": 3,
        }]}

    def test_top_level_nulls_are_omitted_without_coercing_nested_values(self):
        seed = {"result": "r1", "rows": []}
        condition = {"name": "receipt", "selection": {"result": "r2"}, "before": 0, "after": 1}
        envelope = {"document": {"operation": "trace_cooccur", "id": "C1",
                    "workspace": str(self.root / "study"), "title": "Join", "blocks": []},
                    "text": "Complete", "path": str(self.root / "result.txt"), "preview_chars": 6000}
        Path(envelope["path"]).write_text(envelope["text"])
        with patch.object(native.cache, "dispatch", return_value=envelope) as dispatch:
            self.invoke("trace_cooccur", seed=seed, against=[condition],
                        preview_chars=None, saved=None, save_as=None)
        dispatch.assert_called_once_with("trace_cooccur", {
            "workspace": str(self.root / "study"), "seed": seed, "against": [condition]})
        with patch.object(native.cache, "dispatch", return_value=envelope) as dispatch:
            self.invoke("trace_cooccur", seed={"result": "r1", "rows": None}, against=[condition])
        self.assertIsNone(dispatch.call_args.args[1]["seed"]["rows"])

    def test_matched_unmatched_witness_navigation_and_seed_only_reads(self):
        args = self.join_args()
        joined = self.invoke("trace_cooccur", **args, save_as="fixed-inputs")
        self.assertEqual(joined["operation"], "trace_cooccur")
        self.assertEqual(joined["counts"], {
            "seed_events": 3, "matched_seed_events": 2, "unmatched_seed_events": 1})
        self.assertTrue((self.root / "study" / "trace.sqlite3").is_file())
        self.assertEqual(sum(b["group"] == "matched" for b in joined["blocks"]), 2)
        self.assertEqual(sum(b["group"] == "unmatched" for b in joined["blocks"]), 1)
        matched_blocks = [b for b in joined["blocks"] if b["group"] == "matched"]
        unmatched_blocks = [b for b in joined["blocks"] if b["group"] == "unmatched"]
        self.assertEqual(len(matched_blocks[0]["links"]), 2)
        self.assertIn("candidate three", unmatched_blocks[0]["text"])
        self.assertTrue(any("nearest outside window" in link["label"] and "receipt beta" in link["text"]
                            for link in unmatched_blocks[0]["links"]))
        seed_read = self.invoke("trace_read", targets=[{"result": joined["id"], "group": "matched", "rows": [0]}],
                                pair=False, line_context=0)
        self.assertEqual([block["text"] for block in seed_read["blocks"]], ["candidate one"])
        unmatched = self.invoke("trace_read", targets=[{"result": joined["id"], "group": "unmatched"}],
                                pair=False, line_context=0)
        self.assertEqual([block["text"] for block in unmatched["blocks"]], ["candidate three"])
        witnesses = {"result": joined["id"], "group": "witnesses"}
        witness_read = self.invoke("trace_read", targets=[witnesses], pair=False, line_context=0)
        self.assertEqual({block["text"] for block in witness_read["blocks"]},
                         {"receipt alpha", "receipt beta"})
        witness_search = self.invoke("trace_search", scope=witnesses, query="receipt", mode="literal")
        self.assertEqual(len(witness_search["blocks"]), 2)
        edited = self.invoke("trace_edit", put=[{
            "id": "receipts", "kind": "note", "title": "Selected witnesses", "anchors": [witnesses],
        }])
        self.assertEqual(edited["created"], ["receipts"])
        rerun = self.invoke("trace_cooccur", saved="fixed-inputs", seed=None, against=None)
        self.assertEqual(rerun["counts"], joined["counts"])
        self.assertNotEqual(rerun["id"], joined["id"])

    def test_invalid_nested_and_removed_cursor_use_normal_tool_error_path(self):
        args = self.join_args()
        for invalid in (
            {"seed": {**args["seed"], "rows": None}},
            {"seed": {**args["seed"], "rows": [True]}},
            {"seed": {**args["seed"], "extra": 1}},
            {"against": [{**args["against"][0], "before": True}]},
            {"against": [{**args["against"][0], "name": "seed"}]},
            {"against": [{**args["against"][0], "selection": None}]},
            {"seed": {"items": ["missing-item"]}},
            {"seed": {"result": args["seed"]["result"], "group": "bogus"}},
        ):
            with self.subTest(invalid=invalid):
                self.invoke("trace_cooccur", **{**args, **invalid}, expect_error=True)
        self.invoke("trace_cooccur", **args, cursor="c1", expect_error=True)
        self.invoke("trace_cooccur", **args, saved="missing", expect_error=True)

    def test_explicit_empty_rows_does_not_select_all(self):
        args = self.join_args()
        args["seed"]["rows"] = []
        result = self.invoke("trace_cooccur", **args)
        self.assertEqual(result["counts"]["seed_events"], 0)
        self.assertEqual(result["blocks"], [])

    def test_nested_schema_is_explicit_and_closed(self):
        params = native.trace_cooccur.schema["function"]["parameters"]
        self.assertEqual(params["required"], ["workspace"])
        self.assertEqual(set(params["properties"]), {
            "workspace", "seed", "against", "saved", "save_as", "preview_chars"})
        props = params["properties"]
        seed = props["seed"]["anyOf"][0]
        self.assertEqual(props["seed"]["anyOf"][1], {"type": "null"})
        variants = seed["anyOf"]
        self.assertEqual([set(v["properties"]) for v in variants], [{"result"}, {"result", "rows"},
                          {"result", "group"}, {"result", "group", "rows"}, {"items"}])
        for variant in variants:
            self.assertEqual(variant["required"], list(variant["properties"]))
            self.assertFalse(variant["additionalProperties"])
        against = props["against"]["anyOf"][0]
        self.assertEqual((against["minItems"], against["maxItems"]), (1, 3))
        condition = against["items"]
        self.assertEqual(condition["required"], ["name", "selection", "before", "after"])
        self.assertFalse(condition["additionalProperties"])
        self.assertEqual(condition["properties"]["selection"], seed)
        for key in ("before", "after"):
            self.assertEqual(condition["properties"][key], {"type": "integer", "minimum": 0})

    def test_strict_provider_required_keys_keep_both_selector_variants(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema unavailable; no dependency installation performed")
        schema = copy.deepcopy(native.trace_cooccur.schema["function"]["parameters"])

        def require_all(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    node["required"] = list(node.get("properties", {}))
                for child in node.values():
                    require_all(child)
            elif isinstance(node, list):
                for child in node:
                    require_all(child)

        require_all(schema)
        validator = Draft202012Validator(schema)
        base = dict(workspace="study", preview_chars=None, saved=None, save_as=None)
        for selector in ({"result": "r1"}, {"result": "r1", "rows": []},
                         {"result": "r1", "rows": [0, 2]}, {"items": ["S1-H001"]},
                         {"result": "C1", "group": "unmatched"},
                         {"result": "C1", "group": "witnesses", "rows": [0]}):
            payload = {**base, "seed": selector, "against": [{"name": "receipt",
                "selection": selector, "before": 0, "after": 2}]}
            validator.validate(payload)
        for bad in ({}, {"result": "r1", "rows": None}, {"result": "r1", "rows": [True]},
                    {"result": "r1", "extra": 1}):
            payload = {**base, "seed": bad, "against": [{"name": "receipt",
                "selection": {"result": "r2"}, "before": 0, "after": 2}]}
            self.assertTrue(list(validator.iter_errors(payload)), bad)

    @unittest.skipUnless(ZOO_AVAILABLE, "provider conversion requires Agent Zoo")
    def test_host_provider_conversion_retains_nonnull_nested_schemas(self):
        from agents.llm import _scrub_fn_tool_schemas, _tools_for_responses
        original = copy.deepcopy(native.trace_cooccur.schema)
        converted = _tools_for_responses(_scrub_fn_tool_schemas([original]))[0]
        self.assertEqual(converted["parameters"], native.trace_cooccur.schema["function"]["parameters"])
        self.assertEqual(original, native.trace_cooccur.schema)


class CooccurCliTests(SyntheticFixture, unittest.TestCase):
    def invoke(self, operation, arguments, expect_error=False):
        proc = subprocess.run([sys.executable, "-m", "azo_session_postmortem", operation,
                               "--workspace", str(self.root / "study"),
                               "--json", json.dumps(arguments)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2 if expect_error else 0, proc.stderr)
        return json.loads(proc.stderr if expect_error else proc.stdout)

    def test_sixth_cli_choice_dispatches_and_rejects_unknown_arguments(self):
        from azo_session_postmortem.cli import TOOLS
        self.assertEqual(len(TOOLS), 6)
        self.assertIn("trace_cooccur", TOOLS)
        self.invoke("trace_open", {"sessions": [str(self.source)], "descendants": False})
        seed = self.invoke("trace_search", {"query": "candidate", "mode": "literal"})
        witnesses = self.invoke("trace_search", {"query": "receipt", "mode": "literal"})
        args = {"seed": {"result": seed["result"]}, "against": [{"name": "receipt",
            "selection": {"result": witnesses["result"]}, "before": 0, "after": 1}]}
        joined = self.invoke("trace_cooccur", args)
        self.assertEqual(joined["counts"], {
            "seed_events": 3, "matched_seed_events": 2, "unmatched_seed_events": 1})
        error = self.invoke("trace_cooccur", {**args, "not_an_argument": True}, expect_error=True)
        self.assertIn("not_an_argument", error["message"])


if __name__ == "__main__":
    unittest.main()
