"""Check a copied existing workspace through native tools; emit counts, not source text."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile


def map_state(db):
    return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("map_entries", "map_members", "map_links")}


def main():
    from agent_utils import State, ToolCall
    from agent_utils.files.buffer_manager import BufferManager
    from azo_session_postmortem import native
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    original = (args.workspace.expanduser().resolve() / "trace.sqlite3")
    with tempfile.TemporaryDirectory(prefix="postmortem-compatibility-") as temporary:
        workspace = Path(temporary) / "study"
        workspace.mkdir()
        copied = workspace / "trace.sqlite3"
        source = sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(copied)
        try:
            before = map_state(source)
            source.backup(target)
        finally:
            target.close()
            source.close()
        state = State()
        state.session_cwd = temporary
        state.buffer_manager = BufferManager()
        state.tool_schemas = {}

        def invoke(name, **arguments):
            arguments["workspace"] = "study"
            call = ToolCall(id="compatibility", name=name, raw_args=json.dumps(arguments), parsed_args=arguments)
            state.pending_tool_calls = [call]
            getattr(native, name)(state)
            if call.error:
                raise RuntimeError(call.result)
            return json.loads(call.result)

        opened = invoke("trace_open")
        rows = invoke("trace_query", sql="SELECT ref FROM events ORDER BY session,seq LIMIT 1")["rows"]
        assert rows, "Compatibility source contains no events"
        packet = invoke("trace_read", targets=[rows[0]["ref"]], output="buffer", max_chars=400)
        assert packet["buffer"].startswith("buf_")
        view = state.buffer_manager.resolve_for_read(state, packet["buffer"])
        assert view.text
        if opened["pinned_entries"]:
            entry = opened["pinned_entries"][0]["id"]
            invoke("trace_read", targets=[{"entry": entry, "part": "card"}], max_chars=500)
        db = sqlite3.connect(copied)
        try:
            cursor = db.execute("SELECT id FROM _cursors WHERE kind='read' ORDER BY id LIMIT 1").fetchone()
            if cursor:
                invoke("trace_read", cursor=f"c{cursor[0]}", max_chars=100)
            assert before == map_state(db), "Native retrieval changed authored map state"
        finally:
            db.close()
        print(json.dumps({"copied_workspace_only": True, "native_managed_buffer": True,
                          "map_unchanged": True, "old_read_cursor_checked": bool(cursor),
                          "counts": opened["counts"]}, sort_keys=True))


if __name__ == "__main__":
    main()
