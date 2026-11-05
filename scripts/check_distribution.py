"""Build and smoke-test the dependency-free wheel without installing it anywhere."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def main():
    from setuptools.build_meta import build_wheel
    repo = Path(__file__).resolve().parents[1]
    wheel = (repo / "dist" / build_wheel(str(repo / "dist"))).resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "azo_session_postmortem/cli.py" in names
        assert "azo_session_postmortem/engine.py" in names
        skill = "azo_session_postmortem/skills/session-postmortem/SKILL.md"
        assert skill in names
        skill_text = archive.read(skill).decode("utf-8")
        assert "name: session-postmortem" in skill_text
        assert "preview_chars" in skill_text
        assert "azo_session_postmortem/cache.py" in names
        assert "azo_session_postmortem/cache_render.py" in names
        assert "trace_cooccur" in skill_text
        assert "azo_session_postmortem/navigation.py" in names
        assert "azo_session_postmortem/render.py" in names
        assert "follow_object_links" in skill_text
        assert "session_links" in skill_text
        assert "azo_session_postmortem/skills/session-postmortem/references/recipes.md" in names
        entries = next(name for name in names if name.endswith("entry_points.txt"))
        assert "azo-postmortem = azo_session_postmortem.cli:main" in archive.read(entries).decode()
        assert not any(name.startswith(("tests/", ".work/", "examples/")) for name in names)
    with tempfile.TemporaryDirectory(prefix="postmortem-wheel-") as temporary:
        args = {"workspace": str(Path(temporary) / "study"),
                "sessions": [str(repo / "examples/session.json")], "descendants": False}
        program = ("import sys; sys.path.insert(0, sys.argv[1]); "
                   "from azo_session_postmortem.cli import main, TOOLS; "
                   "assert len(TOOLS) == 6 and 'trace_cooccur' in TOOLS; "
                   "raise SystemExit(main(sys.argv[2:]))")
        def call(operation, arguments):
            proc = subprocess.run([sys.executable, "-I", "-c", program, str(wheel),
                                   operation, "--json", json.dumps(arguments)],
                                  cwd=temporary, capture_output=True, text=True, check=True)
            return json.loads(proc.stdout)
        result = call("trace_open", args)
        assert result["sources"][0]["id"] == "example-parser"
        assert result["counts"]["events"] > 0
        assert "workflow_guide" not in result["capabilities"]
        inspected = call("trace_read", {"workspace": args["workspace"], "inspect": True,
            "targets": [{"session": "example-parser", "events": [1, 2]}],
            "pair": False, "max_chars": 10})
        assert inspected["plan_result"]
        assert "blocks" not in inspected and "buffer" not in inspected
        assert not (Path(args["workspace"]) / "packets").exists()
        selected = call("trace_query", {"workspace": args["workspace"],
            "sql": "SELECT ref FROM events ORDER BY seq"})
        witnesses = call("trace_search", {"workspace": args["workspace"],
            "query": "header and payload", "mode": "literal"})
        joined = call("trace_cooccur", {"workspace": args["workspace"],
            "seed": {"result": selected["result"], "rows": [0, selected["row_count"] - 1]},
            "against": [{"name": "receipt", "selection": {"result": witnesses["result"]},
                         "before": 0, "after": 1}]})
        assert joined["counts"] == {
            "seed_events": 2, "matched_seed_events": 1, "unmatched_seed_events": 1}
        unmatched = call("trace_query", {"workspace": args["workspace"],
            "cursor": joined["unmatched_cursor"]})
        assert unmatched["rows"][0]["failed_conditions"] == ["receipt"]
        witness_result = joined["rows"][0]["conditions"][0]["witnesses_result"]
        witness_text = call("trace_read", {"workspace": args["workspace"],
            "targets": [{"result": witness_result}], "pair": False, "line_context": 0})
        assert "header and payload" in witness_text["blocks"][0]["text"]
        edited = call("trace_edit", {"workspace": args["workspace"], "put": [{
            "id": "synthetic-witness", "kind": "note", "title": "Synthetic witness",
            "anchors": [{"result": witness_result}]}]})
        assert edited["created"] == ["synthetic-witness"]
        child = '11111111-2222-4333-8444-555555555555'
        launch = (f'Submitted RLM task rlm_job_123 with profile simple (Simple).\n'
                  f'Child live session: {child}\nTask name (/attach label): worker\n'
                  'Working directory: /not-read\nOutput buffers reserved:\n'
                  '- answer -> buf_aaaaaa [reserved]\n'
                  "Use rlm_status(job_id='rlm_job_123') to inspect it or "
                  "cancel_rlm(job_id='rlm_job_123') to kill it.")
        fixture = Path(temporary) / 'navigation.json'
        fixture.write_text(json.dumps({'meta': {'session_id': 'P'}, 'events': [
            {'kind': 'result', 'role': 'tool', 'tool': 'submit_rlm', 'text': launch},
            {'kind': 'message', 'role': 'assistant', 'text': 'Read buf_aaaaaa.'}],
            'components': {'buffer_manager': {'buffers': {
                'buf_aaaaaa': {'text': 'object_only_marker'}}}}}))
        ws = str(Path(temporary) / 'navigation')
        call('trace_open', {'workspace': ws, 'sessions': [str(fixture)], 'descendants': False})
        search = {'workspace': ws, 'scope': {'refs': ['P/e2']},
                  'query': 'object_only_marker', 'mode': 'literal'}
        assert call('trace_search', search)['matched_total']['source'] == 1
        assert call('trace_search', {**search, 'follow_object_links': False})['matched_total']['source'] == 0
        assert call('trace_search', {**search, 'include_references': False})['matched_total']['source'] == 0
        nav = call('trace_read', {'workspace': ws, 'targets': [{'event': 'P/e2', 'field': 'text'}],
                                  'pair': False, 'line_context': 0})['session_navigation']
        assert any(row['dst'] == child and not row['indexed'] for row in nav)
        assert call('trace_open', {'workspace': ws})['counts']['sessions'] == 1
        cache_program = ("import sys,json; sys.path.insert(0,sys.argv[1]); "
                         "from azo_session_postmortem.cache import dispatch; "
                         "print(json.dumps(dispatch(sys.argv[2],json.loads(sys.argv[3]))))")
        def cache_call(operation, arguments):
            proc = subprocess.run([sys.executable, '-I', '-c', cache_program, str(wheel),
                                   operation, json.dumps(arguments)], cwd=temporary,
                                  capture_output=True, text=True, check=True)
            return json.loads(proc.stdout)
        cached = cache_call('trace_search', {'workspace': args['workspace'],
            'query': 'header and payload', 'mode': 'literal', 'preview_chars': 0})
        hits = [b for b in cached['document']['blocks'] if b['group'] == 'hit']
        assert hits and '## INDEX' in cached['text']
        assert Path(cached['path']).read_bytes() == cached['text'].encode('utf-8')
        expanded = cache_call('trace_read', {'workspace': args['workspace'],
            'targets': [hits[0]['id']], 'pair': False, 'line_context': 0})
        assert 'header and payload' in expanded['text']
        reopened = cache_call('trace_open', {'workspace': args['workspace']})
        assert cached['document']['id'] in reopened['text']
    print(json.dumps({"wheel": str(wheel), "isolated_cli_smoke": "passed",
                      "bundled_skill": "session-postmortem", "inspection_smoke": "passed",
                      "console_entrypoint": "azo-postmortem", "tool_count": 6,
                      "cooccur_smoke": "passed", "installed": False}))


if __name__ == "__main__":
    main()
