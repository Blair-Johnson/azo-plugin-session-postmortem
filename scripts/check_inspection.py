"""Historical engine inspection compatibility on a disposable workspace copy."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import tempfile


def anchor_key(anchor):
    if isinstance(anchor, str):
        return anchor, "text"
    return (anchor.get("event") or anchor.get("object") or anchor.get("entry"),
            anchor.get("field", anchor.get("part", "text")))


def delivered_costs(blocks):
    costs = defaultdict(int)
    for block in blocks:
        anchor = block.get("anchor", {"entry": block.get("entry_id"), "part": "card"})
        if "text" in block:
            size = len(block["text"])
        elif "card" in block:
            size = len(json.dumps(block["card"], ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        else:
            size = 0
        if size:
            costs[anchor_key(anchor)] += size
    return dict(costs)

def main():
    from scripts.replay_cooccur import EngineClient
    from scripts.check_workspace import map_state

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--read-json", required=True, help="Initial trace_read arguments, without workspace")
    args = parser.parse_args()
    request = json.loads(args.read_json)
    assert isinstance(request, dict)
    assert not {"workspace", "cursor", "inspect", "output"} & request.keys()
    original = args.workspace.expanduser().resolve() / "trace.sqlite3"
    with tempfile.TemporaryDirectory(prefix="postmortem-inspection-") as temporary:
        study = Path(temporary) / "study"
        study.mkdir()
        with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(study / "trace.sqlite3") as target:
                before = map_state(source)
                source.backup(target)
        client = EngineClient(study)
        invoke = client.call

        opened = invoke("trace_open")
        plan = invoke("trace_read", **request, inspect=True)
        assert not (study / "packets").exists()
        rows = list(plan["rows"])
        cursor = plan.get("plan_cursor")
        while cursor:
            page = invoke("trace_query", cursor=cursor, max_chars=12000)
            rows.extend(page["rows"])
            cursor = page["next_cursor"]
        assert len(rows) == plan["total_rows"]
        actual = invoke("trace_read", **request)
        predicted = defaultdict(int)
        for row in rows:
            predicted[anchor_key(row["anchor"])] += row.get("would_deliver_chars") or 0
        delivered = delivered_costs(actual["blocks"])
        assert sum(predicted.values()) == actual["text_chars"]
        assert {k: v for k, v in predicted.items() if v} == {k: v for k, v in delivered.items() if v}
        packet = invoke("trace_read", **request, output="buffer")
        assert Path(packet["buffer"]).is_file()
        with open(packet["buffer_path"], encoding="utf-8", newline="") as saved:
            packet_text = saved.read()
        assert packet_text and len(packet_text) == packet["packet_chars"]
        assert "workflow_guide" not in opened["capabilities"]
        with sqlite3.connect(study / "trace.sqlite3") as target:
            assert before == map_state(target)
        print(json.dumps({"copied_workspace_only": True, "map_unchanged": True,
            "comparison": "engine_inspection_compatibility",
            "prediction_matches_engine_delivery": True, "plan_rows": len(rows),
            "engine_response_chars": client.response_chars, "engine_packet_file": True,
            "predicted_text_chars": sum(predicted.values()), "actual_text_chars": actual["text_chars"],
            "fully_deferred_rows": plan["fully_deferred_rows"],
            "partially_delivered_rows": plan["partially_delivered_rows"],
            "inspection_preview_rows": len(plan["rows"]), "counts": opened["counts"]}))


if __name__ == "__main__":
    main()
