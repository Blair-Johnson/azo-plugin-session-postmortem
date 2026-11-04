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

    def invoke(self, name, *, expect_error=False, **arguments):
        arguments = {"workspace": "study", **arguments}
        call = ToolCall(id="cooccur-native", name=name,
                        raw_args=json.dumps(arguments), parsed_args=arguments)
        self.state.pending_tool_calls = [call]
        self.assertIs(getattr(native, name)(self.state), self.state)
        self.assertEqual(call.error, expect_error, call.result)
        return call.result if expect_error else json.loads(call.result)

    def selections(self):
        self.invoke("trace_open", sessions=["source.json"], descendants=False)
        seed = self.invoke("trace_search", query="candidate", mode="literal")
        witness = self.invoke("trace_search", query="receipt", mode="literal")
        return seed, witness

    def join_args(self):
        seed, witness = self.selections()
        return {"seed": {"result": seed["result"]}, "against": [{
            "name": "receipt", "selection": {"result": witness["result"]},
            "before": 0, "after": 3,
        }]}

    def test_top_level_nulls_are_omitted_without_coercing_nested_values(self):
        seed = {"result": "r1", "rows": []}
        condition = {"name": "receipt", "selection": {"result": "r2"}, "before": 0, "after": 1}
        with patch.object(native.engine, "dispatch", return_value={"ok": True}) as dispatch:
            self.invoke("trace_cooccur", seed=seed, against=[condition], page_size=None,
                        max_chars=None, cursor=None, saved=None, save_as=None)
        dispatch.assert_called_once_with("trace_cooccur", {
            "workspace": str(self.root / "study"), "seed": seed, "against": [condition]})
        with patch.object(native.engine, "dispatch", return_value={}) as dispatch:
            self.invoke("trace_cooccur", seed={"result": "r1", "rows": None}, against=[condition])
        self.assertIsNone(dispatch.call_args.args[1]["seed"]["rows"])

    def test_matched_unmatched_witness_navigation_and_seed_only_reads(self):
        args = self.join_args()
        joined = self.invoke("trace_cooccur", **args, page_size=1, save_as="fixed-inputs")
        self.assertEqual(joined["operation"], "cooccur")
        self.assertEqual(joined["status"], "complete")
        self.assertEqual(joined["counts"], {
            "seed_events": 3, "matched_seed_events": 2, "unmatched_seed_events": 1})
        self.assertTrue((self.root / "study" / "trace.sqlite3").is_file())
        self.assertEqual(joined["rows"][0]["row"], 0)
        self.assertEqual(joined["rows"][0]["conditions"][0]["witness_count"], 2)
        next_page = self.invoke("trace_cooccur", cursor=joined["next_cursor"],
                                seed=None, against=None, saved=None, save_as=None,
                                page_size=2, max_chars=10000)
        self.assertEqual(next_page["result"], joined["result"])
        self.assertEqual(next_page["counts"], joined["counts"])
        self.assertEqual(next_page["rows"][0]["row"], 1)
        self.assertIsNone(next_page["next_cursor"])
        unmatched = self.invoke("trace_query", cursor=joined["unmatched_cursor"])
        self.assertEqual(unmatched["result"], joined["unmatched_result"])
        self.assertEqual(unmatched["rows"][0]["failed_conditions"], ["receipt"])
        self.assertEqual(unmatched["rows"][0]["conditions"][0]["status"], "outside_window")
        seed_read = self.invoke("trace_read", targets=[{"result": joined["result"], "rows": [0]}],
                                pair=False, line_context=0)
        self.assertEqual([block["text"] for block in seed_read["blocks"]], ["candidate one"])
        witnesses = joined["rows"][0]["conditions"][0]["witnesses_result"]
        witness_read = self.invoke("trace_read", targets=[{"result": witnesses}],
                                   pair=False, line_context=0)
        self.assertEqual([block["text"] for block in witness_read["blocks"]],
                         ["receipt alpha", "receipt beta"])
        witness_search = self.invoke("trace_search", scope={"result": witnesses},
                                     query="receipt", mode="literal")
        self.assertEqual(witness_search["matched_total"]["source"], 2)
        edited = self.invoke("trace_edit", put=[{
            "id": "receipts", "kind": "note", "title": "Selected witnesses",
            "anchors": [{"result": witnesses}],
        }])
        self.assertEqual(edited["created"], ["receipts"])
        rerun = self.invoke("trace_cooccur", saved="fixed-inputs", seed=None, against=None)
        self.assertEqual(rerun["counts"], joined["counts"])
        self.assertEqual(rerun["inputs"], joined["inputs"])
        self.assertNotEqual(rerun["result"], joined["result"])

    def test_invalid_nested_and_cursor_arguments_use_normal_tool_error_path(self):
        args = self.join_args()
        for invalid in (
            {"seed": {**args["seed"], "rows": None}},
            {"seed": {**args["seed"], "rows": [True]}},
            {"seed": {**args["seed"], "extra": 1}},
            {"against": [{**args["against"][0], "before": True}]},
            {"against": [{**args["against"][0], "name": "seed"}]},
            {"against": [{**args["against"][0], "selection": None}]},
        ):
            with self.subTest(invalid=invalid):
                self.invoke("trace_cooccur", **{**args, **invalid}, expect_error=True)
        joined = self.invoke("trace_cooccur", **args, page_size=1)
        for extra in ({"seed": args["seed"]}, {"against": args["against"]},
                      {"saved": "missing"}, {"save_as": "no-write"}):
            with self.subTest(extra=extra):
                self.invoke("trace_cooccur", cursor=joined["next_cursor"],
                            **extra, expect_error=True)
        continued = self.invoke("trace_cooccur", cursor=joined["next_cursor"])
        self.assertEqual(continued["counts"], joined["counts"])

    def test_explicit_empty_rows_does_not_select_all(self):
        args = self.join_args()
        args["seed"]["rows"] = []
        result = self.invoke("trace_cooccur", **args)
        self.assertEqual(result["counts"]["seed_events"], 0)
        self.assertEqual(result["rows"], [])
        self.assertIsNone(result["next_cursor"])
        self.assertIsNone(result["unmatched_cursor"])

    def test_nested_schema_is_explicit_and_closed(self):
        params = native.trace_cooccur.schema["function"]["parameters"]
        self.assertEqual(params["required"], ["workspace"])
        self.assertEqual(set(params["properties"]), {
            "workspace", "seed", "against", "page_size", "max_chars", "cursor", "saved", "save_as"})
        props = params["properties"]
        seed = props["seed"]["anyOf"][0]
        self.assertEqual(props["seed"]["anyOf"][1], {"type": "null"})
        variants = seed["anyOf"]
        self.assertEqual([set(v["properties"]) for v in variants], [{"result"}, {"result", "rows"}])
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
        base = dict(workspace="study", page_size=None, max_chars=None,
                    cursor=None, saved=None, save_as=None)
        for selector in ({"result": "r1"}, {"result": "r1", "rows": []},
                         {"result": "r1", "rows": [0, 2]}):
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
