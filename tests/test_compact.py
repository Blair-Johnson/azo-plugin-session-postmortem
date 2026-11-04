"""Compact is a lossless display transform, never a different read selection."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_contract as fixtures
import test_native as native_fixtures
from azo_session_postmortem import engine


COMPACT_FORMAT = "compact_read_json_v1"


def normalize_read(page):
    """Decode documented defaults/deliveries; leave navigation handles untouched.

    Only packet representation/count is excluded. Legacy text defaults are also
    made explicit so older cursor blocks lacking char_offset compare semantically.
    No source bodies, anchors, attribution, coverage or previews are discarded.
    """
    result = deepcopy(page)
    compact = result.pop("packet_format") == COMPACT_FORMAT
    result.pop("packet_chars")
    deliveries = []
    for block in result["blocks"]:
        block.setdefault("requested", True)
        if "card" in block:
            delivery = {"entry": block["entry_id"], "part": "card"}
        elif "text" in block:
            block.setdefault("char_offset", 0)
            block.setdefault("char_end", block["char_offset"] + len(block["text"]))
            block.setdefault("complete", True)
            delivery = ({"anchor": block["anchor"]} if "anchor" in block else
                        {"entry": block["entry_id"], "part": "card"})
            delivery.update(char_offset=block["char_offset"], char_end=block["char_end"])
        else:
            delivery = {"anchor": block.get("anchor"), "available": False}
        deliveries.append(delivery)
    if "delivered" not in result:
        if not compact:
            raise AssertionError("legacy inline must retain delivered")
        result["delivered"] = deliveries
    # Retained delivered can carry otherwise unrecoverable legacy navigation.
    return result


class CompactTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    call = fixtures.ContractTests.call
    cli = fixtures.ContractTests.cli

    @contextmanager
    def db(self):
        db = sqlite3.connect(Path(self.workspace) / "trace.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def assert_parity(self, inline, compact):
        self.assertEqual(inline["packet_format"], "compact_json")
        self.assertEqual(compact["packet_format"], COMPACT_FORMAT)
        for page in (inline, compact):
            self.assertEqual(page["packet_chars"], len(engine._json(page)))
            self.assertLessEqual(page["text_chars"], page["max_chars"])
            self.assertEqual(page["block_count"], len(page["blocks"]))
        decoded = [normalize_read(page) for page in (inline, compact)]
        # Independent reads allocate different opaque handles. Compare their full
        # immutable payloads/results instead of hiding or simply dropping them.
        with self.db() as db:
            for page in decoded:
                if page["next_cursor"]:
                    page["next_cursor"] = engine._uncursor(db, page["next_cursor"], "read")
                if "references_result" in page:
                    page["references_result"] = engine._get_result(db, page["references_result"])
                if "references_cursor" in page:
                    payload = engine._uncursor(db, page["references_cursor"], "query")
                    payload["result"] = engine._get_result(db, payload["result"])
                    page["references_cursor"] = payload
        self.assertEqual(*decoded)

    def paired(self, **arguments):
        inline = self.call("trace_read", output="inline", **arguments)
        compact = self.call("trace_read", output="compact", **arguments)
        self.assert_parity(inline, compact)
        return inline, compact

    def add_objects(self, texts):
        refs = []
        with self.db() as db:
            for i, text in enumerate(texts):
                ref = f"S/compact-{i}"
                refs.append(ref)
                db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
                           (ref, "buffer", 0 if text is None else len(text.splitlines()),
                            0 if text is None else len(text), int(text is not None), "S", "Unavailable fixture"))
                if text is not None:
                    db.execute("INSERT INTO _content VALUES(?,?,?)", (ref, "text", text))
        return refs

    def table_snapshot(self):
        with self.db() as db:
            return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                    for table in ("_cursors", "_results", "result_items", "map_entries")}

    def test_minimal_omissions_pure_renderer_and_exact_packet_size(self):
        inline, compact = self.paired(targets=["S/e1"], pair=False)
        self.assertEqual(set(inline) - set(compact), {"delivered"})
        self.assertEqual(set(inline["blocks"][0]) - set(compact["blocks"][0]),
                         {"requested", "char_offset", "char_end", "complete"})
        self.assertEqual(set(compact["blocks"][0]), {"anchor", "text"})
        self.assertLess(compact["packet_chars"], inline["packet_chars"])
        self.assertEqual(self.call("trace_read", targets=["S/e1"], pair=False), inline)
        before = deepcopy(inline)
        self.assertEqual(engine._compact_read(inline), compact)
        self.assertEqual(inline, before)
        self.assertFalse((Path(self.workspace) / "packets").exists())

    def test_non_derivable_end_and_false_values_are_preserved(self):
        inline = self.call("trace_read", targets=["S/e1"], pair=False)
        block = inline["blocks"][0]
        block.update(char_offset=7, char_end=999, complete=False, requested=False)
        inline["delivered"][0].update(char_offset=7, char_end=999)
        before = deepcopy(inline)
        compact = engine._compact_read(inline)
        self.assertEqual(compact["blocks"][0], block)
        self.assertEqual(normalize_read(compact), normalize_read(inline))
        self.assertEqual(inline, before)

    def test_attribution_pairing_projection_and_context_order(self):
        with self.db() as db:
            db.execute("UPDATE events SET system_generated=0 WHERE ref='S/e2'")
            db.execute("UPDATE events SET system_generated=1 WHERE ref='S/e3'")
            db.execute("INSERT INTO _content VALUES(?,?,?)", ("S/e3", "provider_replay", "METADATA_BODY"))
        for projection in ("content", "all"):
            with self.subTest(projection=projection):
                _, compact = self.paired(targets=["S/e3"], pair=True, before=1, after=2,
                                         projection=projection, max_chars=100000)
                sources = compact["sources"]
                self.assertIs(sources["S/e2"]["system_generated"], False)
                self.assertIs(sources["S/e3"]["system_generated"], True)
                self.assertIsNone(sources["S/e4"]["system_generated"])
                self.assertEqual(sources["S/e3"]["kind"], "call")
                self.assertEqual(sources["S/e3"]["tool"], "terminal_run")
                self.assertEqual(sources["S/e2"]["role"], "assistant")
                self.assertTrue(any(b.get("requested") is False for b in compact["blocks"]))
                self.assertEqual("METADATA_BODY" in json.dumps(compact), projection == "all")
        with self.db() as db:
            db.execute("UPDATE _content SET text='a\nbb\nccc\ndddd\neeeee\nf' WHERE ref='S/o-log'")
        _, compact = self.paired(targets=[{"object": "S/o-log", "lines": [2, 3]}],
                                 line_context=1, pair=False)
        self.assertEqual([b["anchor"]["lines"] for b in compact["blocks"]], [[2, 3], [1, 1], [4, 4]])
        self.assertEqual([b.get("requested", True) for b in compact["blocks"]], [True, False, False])

    def test_long_lines_unicode_escaping_offsets_and_mixed_pages(self):
        text = 'αβ🙂"\\' * 17 + '\r\nlast line\n'
        with self.db() as db:
            db.execute("UPDATE _content SET text=? WHERE ref='S/o-log'", (text,))
        inline, compact = self.paired(targets=["S/o-log"], max_chars=7)
        pieces, offset = [], 0
        while True:
            block, = compact["blocks"]
            self.assertEqual(block.get("char_offset", 0), offset)
            self.assertNotIn("char_end", block)
            self.assertEqual(block.get("complete", True), not bool(compact["next_cursor"]))
            pieces.append(block["text"])
            offset += len(block["text"])
            if not compact["next_cursor"]:
                break
            cursor = compact["next_cursor"]
            # A compact cursor's default response is still legacy inline.
            default = self.call("trace_read", cursor=cursor, max_chars=7)
            self.assertEqual(default["packet_format"], "compact_json")
            inline, compact = self.paired(cursor=cursor, max_chars=7)
            self.assertEqual(default["blocks"], inline["blocks"])
        self.assertEqual("".join(pieces), text)
        self.assertEqual(offset, len(text))

    def test_empty_bodies_and_block_limit_keep_deferred_coverage(self):
        refs = self.add_objects([""] * 70 + ["tail"])
        inline, compact = self.paired(targets=refs, max_chars=5)
        self.assertEqual(compact["block_count"], 64)
        self.assertEqual(compact["text_chars"], 0)
        self.assertEqual(compact["remaining_count"], 7)
        self.assertTrue(all(b["text"] == "" for b in compact["blocks"]))
        _, rest = self.paired(cursor=compact["next_cursor"], max_chars=5)
        self.assertEqual(rest["block_count"], 7)
        self.assertEqual(rest["blocks"][-1]["text"], "tail")
        self.assertIsNone(rest["next_cursor"])
        self.assertEqual(len(normalize_read(inline)["delivered"]), 64)

    def test_unavailable_unknown_attribution_and_bounded_previews(self):
        refs = self.add_objects(["long body" * 20] + [None] * 12)
        # A link-only reference is known but has neither a body nor attribution.
        with self.db() as db:
            db.execute("INSERT INTO links VALUES(?,?,?)", (refs[0], "unknown-body", "references"))
        _, first = self.paired(targets=refs + ["unknown-body"], max_chars=1)
        self.assertEqual(first["unavailable_count"], 13)
        self.assertEqual(len(first["unavailable"]), 8)
        self.assertEqual(len(first["remaining"]), 8)
        self.assertEqual(first["remaining_count"], 14)
        _, rest = self.paired(cursor=first["next_cursor"], max_chars=1000)
        unavailable = [b for b in rest["blocks"] if b.get("available") is False]
        self.assertEqual(len(unavailable), 13)
        self.assertEqual(unavailable[-1]["anchor"], "unknown-body")
        self.assertEqual(rest["sources"]["unknown-body"], {})
        for block in unavailable:
            self.assertNotIn("text", block)
            self.assertNotIn("complete", block)
            self.assertNotIn("char_offset", block)
            self.assertIn("reason", block)
        self.assertIsNone(rest["next_cursor"])
        for output in ("inline", "compact"):
            for target in ("S/not-indexed", {"event": "S/e1", "field": "absent"}):
                with self.subTest(output=output, target=target), self.assertRaisesRegex(ValueError, "unknown"):
                    self.call("trace_read", targets=[target], output=output)

    def test_whole_and_split_map_cards_reconstruct_and_remain_navigable(self):
        anchor = {"object": "S/o-log", "lines": [949, 951]}
        self.call("trace_edit", put=[
            {"id": "small", "kind": "note", "title": "Small"},
            {"id": "card", "kind": "episode", "title": "Card", "anchors": [anchor],
             "note": '🙂"\\\n' * 50, "fields": {"hypothesis": "not source"}, "pinned": True}])
        _, whole = self.paired(targets=[{"entry": "card"}], max_chars=100000)
        card = whole["blocks"][0]["card"]
        self.assertEqual(card["anchors"], [anchor])
        self.assertNotIn("complete", whole["blocks"][0])
        self.assertNotIn("char_offset", whole["blocks"][0])
        _, split = self.paired(targets=[{"entry": "card"}, "S/e1"], pair=False, max_chars=31)
        pieces = []
        while True:
            for block in split["blocks"]:
                if block.get("entry_id") == "card":
                    self.assertEqual(block["format"], "map_card_json")
                    self.assertEqual(block.get("char_offset", 0), sum(map(len, pieces)))
                    pieces.append(block["text"])
            if not split["next_cursor"]:
                break
            _, split = self.paired(cursor=split["next_cursor"], max_chars=31)
        self.assertEqual("".join(pieces), engine._json(card))
        self.assertEqual(json.loads("".join(pieces)), card)
        small = self.call("trace_read", targets=[{"entry": "small"}])["text_chars"]
        _, deferred = self.paired(targets=[{"entry": "small"}, {"entry": "card"}], max_chars=small + 1)
        self.assertEqual(deferred["block_count"], 1)
        self.assertEqual(deferred["text_chars"], small)
        self.assertEqual(deferred["remaining"], [{"entry": "card", "part": "card"}])
        _, expanded = self.paired(targets=[{"entry": "card", "part": "anchors"}], pair=False, line_context=0)
        self.assertEqual(expanded["blocks"][0]["anchor"], anchor)

    def test_legacy_unavailable_navigation_is_not_omitted_when_underivable(self):
        with self.db() as db:
            cursor = engine._cursor(db, "read", {"blocks": [
                {"ref": "legacy/missing", "available": False,
                 "reason": "Legacy unavailable block without anchor", "requested": True}]})
        inline, compact = self.paired(cursor=cursor)
        self.assertEqual(compact["delivered"], [{"anchor": "legacy/missing", "available": False}])
        self.assertEqual(compact["unavailable"], inline["unavailable"])
        self.assertEqual(compact["sources"], {"legacy/missing": {}})
        self.assertNotIn("text", compact["blocks"][0])
        self.assertEqual(compact["unavailable_count"], 1)
        self.assertEqual(compact["remaining_count"], 0)

    def test_stale_and_legacy_cursors_keep_original_bodies_and_defaults(self):
        self.call("trace_edit", put=[{"id": "saved", "kind": "note", "title": "Original card"}])
        first = self.call("trace_read", targets=[{"entry": "saved"}, "S/o-log"], max_chars=3)
        with self.db() as db:
            payload = engine._uncursor(db, first["next_cursor"], "read")
            # Exercise pre-attribution cursor metadata and private legacy headers.
            for block in payload["blocks"]:
                block.pop("system_generated", None)
                block["targets"] = ["legacy-only"]
                block["arguments"] = {"legacy": "not for duplicate display"}
            legacy = engine._cursor(db, "read", payload)
            db.execute("UPDATE _content SET text='Replaced live source' WHERE ref='S/o-log'")
        self.call("trace_edit", put=[{"id": "saved", "title": "Changed live card"}])
        _, compact = self.paired(cursor=legacy, max_chars=100000)
        self.assertEqual(compact["blocks"][0]["char_offset"], 3)
        self.assertIn("Original card", compact["blocks"][0]["text"])
        self.assertNotIn("Changed live card", json.dumps(compact))
        self.assertIn("routine output 1", json.dumps(compact))
        self.assertNotIn("Replaced live source", json.dumps(compact))
        self.assertNotIn("legacy-only", json.dumps(compact))
        self.assertIsNone(compact["next_cursor"])
        self.assertEqual(self.call("trace_read", cursor=legacy)["packet_format"], "compact_json")
        with self.db() as db:
            self.assertEqual(engine._uncursor(db, legacy, "read"), payload)
        for invalid in ("not-a-cursor", "c999999"):
            for output in ("inline", "compact"):
                with self.subTest(cursor=invalid, output=output), self.assertRaisesRegex(ValueError, "cursor"):
                    self.call("trace_read", cursor=invalid, output=output)

    def test_reference_overflow_keeps_visible_links_and_full_navigation(self):
        refs = self.add_objects(["linked body"] * 12)
        with self.db() as db:
            db.executemany("INSERT INTO links VALUES(?,?,?)", [("S/e1", ref, "references") for ref in refs])
        inline, compact = self.paired(targets=["S/e1"], pair=False)
        self.assertEqual(compact["references"], inline["references"])
        self.assertEqual(len(compact["references"]), 8)
        self.assertEqual(compact["reference_count"], 12)
        self.assertIn("references_result", compact)
        self.assertIn("references_cursor", compact)
        seen = list(compact["references"])
        cursor = compact["references_cursor"]
        while cursor:
            page = self.call("trace_query", cursor=cursor, page_size=2)
            seen.extend({k: v for k, v in row.items() if k != "row"} for row in page["rows"])
            cursor = page["next_cursor"]
        self.assertEqual({row["dst"] for row in seen}, set(refs))
        self.assertEqual(len(seen), 12)
        _, selected = self.paired(targets=[{"result": compact["references_result"], "rows": [11]}], pair=False)
        self.assertIn("linked body", json.dumps(selected))
        self.assertFalse((Path(self.workspace) / "packets").exists())

    def test_inspection_and_cursor_selection_rejected_before_writes(self):
        first = self.call("trace_read", targets=["S/o-log"], output="compact", max_chars=1)
        snapshot = self.table_snapshot()
        for targets in (["S/e1"], ["S/unknown"]):
            with patch.object(engine, "_read_plan", wraps=engine._read_plan) as plan:
                with self.assertRaisesRegex(ValueError, 'requires output="inline"'):
                    self.call("trace_read", targets=targets, inspect=True, output="compact")
                plan.assert_not_called()
        for key, value in (("pair", True), ("before", 0), ("after", 0),
                           ("line_context", 10), ("projection", "content")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "selection or expansion"):
                self.call("trace_read", cursor=first["next_cursor"], output="compact", **{key: value})
        self.assertEqual(self.table_snapshot(), snapshot)
        self.assertFalse((Path(self.workspace) / "packets").exists())
        self.paired(cursor=first["next_cursor"], inspect=False, max_chars=100000)

    def test_cli_compact_initial_and_cursor_pages(self):
        inline = self.call("trace_read", targets=["S/e1"], pair=False, max_chars=5)
        compact = self.cli("trace_read", targets=["S/e1"], pair=False, max_chars=5, output="compact")
        self.assert_parity(inline, compact)
        rest = self.cli("trace_read", cursor=compact["next_cursor"], output="compact")
        self.assertEqual(rest["packet_chars"], len(engine._json(rest)))
        self.assertEqual(compact["blocks"][0]["text"] + rest["blocks"][0]["text"],
                         "Implement a parser for complete records only.")


@unittest.skipUnless(native_fixtures.HOST_AVAILABLE, "agent_utils is not installed; run in the host environment")
class NativeCompactTests(unittest.TestCase):
    setUp = native_fixtures.NativeTests.setUp
    invoke = native_fixtures.NativeTests.invoke
    open_source = native_fixtures.NativeTests.open_source
    event_refs = native_fixtures.NativeTests.event_refs

    def test_schema_and_compact_without_buffer_manager(self):
        self.open_source()
        props = native_fixtures.native.trace_read.schema["function"]["parameters"]["properties"]
        self.assertEqual(props["output"]["anyOf"][0]["enum"], ["inline", "compact", "buffer"])
        self.assertIn("compact", props["output"]["description"])
        self.assertEqual(set(props), {"workspace", "targets", "before", "after", "pair", "line_context",
                                     "max_chars", "output", "cursor", "projection", "inspect"})
        ref = self.event_refs()[0]
        del self.state.buffer_manager
        compact = self.invoke("trace_read", targets=[ref], output="compact", pair=False)
        inline = self.invoke("trace_read", targets=[ref], output=None, pair=False)
        self.assertEqual(normalize_read(compact), normalize_read(inline))
        self.assertEqual(compact["packet_chars"], len(engine._json(compact)))
        self.assertFalse(hasattr(self.state, "buffer_manager"))
        self.assertFalse((self.root / "study" / "packets").exists())
        self.assertFalse(self.state.entries)

    def test_native_compact_inspect_rejected_and_continuation_defaults_inline(self):
        self.open_source()
        ref = self.event_refs()[0]
        before = (self.root / "study" / "trace.sqlite3").read_bytes()
        error = self.invoke("trace_read", targets=[ref], output="compact", inspect=True, expect_error=True)
        self.assertIn('requires output="inline"', error)
        self.assertEqual((self.root / "study" / "trace.sqlite3").read_bytes(), before)
        first = self.invoke("trace_read", targets=[ref], output="compact", max_chars=5)
        rest = self.invoke("trace_read", cursor=first["next_cursor"], targets=None, pair=None, inspect=None)
        self.assertEqual(rest["packet_format"], "compact_json")
        same = self.invoke("trace_read", cursor=first["next_cursor"], output="compact", inspect=False)
        self.assertEqual(normalize_read(rest), normalize_read(same))


if __name__ == "__main__":
    unittest.main()
