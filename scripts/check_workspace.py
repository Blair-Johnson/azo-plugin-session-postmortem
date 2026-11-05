"""Check a copied workspace through cache-native tools; emit counts, not source text."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile


def map_state(db):
    return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("map_entries", "map_members", "map_links")}


def main():
    from agent_utils import State
    from agent_utils.files.buffer_manager import BufferManager
    from scripts.native_probe import invoke as probe_native
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    original = args.workspace.expanduser().resolve() / "trace.sqlite3"
    with tempfile.TemporaryDirectory(prefix="postmortem-cache-workspace-") as temporary:
        workspace = Path(temporary) / "study"
        workspace.mkdir()
        copied = workspace / "trace.sqlite3"
        with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as source:
            before = map_state(source)
            with sqlite3.connect(copied) as target:
                source.backup(target)
        state = State()
        state.session_cwd = temporary
        state.buffer_manager = BufferManager()
        state.tool_schemas = {}
        response_chars = buffer_chars = calls = 0

        def invoke(name, **arguments):
            nonlocal response_chars, buffer_chars, calls
            response = probe_native(state, name, dict(arguments, workspace="study"),
                                    call_id="cache-workspace")
            calls += 1
            response_chars += len(response.text)
            envelope = response.require_body()
            assert response.buffer_id.startswith("buf_")
            view = state.buffer_manager.resolve_for_read(state, response.buffer_id)
            assert view.text == envelope["text"]
            with open(envelope["path"], encoding="utf-8", newline="") as saved:
                assert saved.read() == view.text
            buffer_chars += len(view.text)
            return envelope["document"]

        opened = invoke("trace_open")
        queried = invoke("trace_query", sql="SELECT ref FROM events ORDER BY session,seq LIMIT 1")
        assert queried["blocks"], "Compatibility source contains no events"
        # Read the retained row alias, rather than parsing human-facing row text.
        invoke("trace_read", targets=[queried["blocks"][0]["id"]], preview_chars=400)
        with sqlite3.connect(copied) as target:
            assert before == map_state(target), "Native retrieval changed authored map state"
        print(json.dumps({"comparison": "cache_native_workspace", "copied_workspace_only": True,
                          "native_managed_buffer": True, "map_unchanged": True,
                          "native_response_chars": response_chars, "buffer_chars": buffer_chars,
                          "calls": calls, "counts": opened.get("counts", {})}, sort_keys=True))


if __name__ == "__main__":
    main()
