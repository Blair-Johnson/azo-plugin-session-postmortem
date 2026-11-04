"""CLI transport for the five session-investigation tools."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TOOLS = ("trace_open", "trace_query", "trace_search", "trace_read", "trace_edit")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "trace_edit kinds: episode (requires anchors), note (anchors optional). "
        "Use note + pinned=true for an open question or continuation. "
        "trace_read/trace_search projection: content (default) or all. "
        "Read budgets count content characters, not total response size. See docs/api.md."))
    parser.add_argument("tool", choices=TOOLS)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--json", help="Tool arguments as a JSON object")
    inputs.add_argument("--args-file", type=Path, help="File containing tool arguments")
    parser.add_argument("--workspace", help="Shortcut for the workspace argument")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--record", type=Path, help="Append requests and responses as JSONL (may contain private text)")
    parser.add_argument("--debug", action="store_true", help="Show unexpected tracebacks")
    opts = parser.parse_args(argv)
    started = time.monotonic()
    record = {"tool": opts.tool}
    try:
        arguments = json.loads(opts.args_file.read_text(encoding="utf-8") if opts.args_file else opts.json)
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        if opts.workspace:
            if arguments.get("workspace", opts.workspace) != opts.workspace:
                raise ValueError("Conflicting workspace arguments")
            arguments["workspace"] = opts.workspace
        record["arguments"] = arguments
        from .engine import dispatch
        result = dispatch(opts.tool, arguments)
        record["response"] = result
        encoded = json.dumps(result, ensure_ascii=False, indent=2 if opts.pretty else None)
        record["output_chars"] = len(encoded)
        print(encoded)
        return 0
    except Exception as exc:
        record["error"] = {"error": type(exc).__name__, "message": str(exc)}
        if opts.debug:
            raise
        print(json.dumps(record["error"], ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if opts.record:
            record["elapsed_s"] = round(time.monotonic() - started, 6)
            opts.record.parent.mkdir(parents=True, exist_ok=True)
            with opts.record.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    sys.exit(main())
