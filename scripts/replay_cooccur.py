"""Historical engine co-occurrence compatibility replay on a disposable workspace.

Known-case retrieval checks, not a fresh-agent speed/quality experiment. Output contains
counts and reference IDs only, not private source bodies. No historical tools execute.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

R = 'e5578fdc-436f-426b-ba71-2e69720103ed'
P = 'd38088ce-5be9-4dff-80ca-0ed127cf39fa'
B = 'b953da4a-2cc9-47dd-8bcd-4eccf7665c35'
S = 'debe37ef-56e1-401e-a5a4-e4c96aa52981'
N = '5958ae78-06d2-4c31-82b2-f7fa59e31aeb'
CASES = [
    dict(name='runtime-five', seed='r7', against=[('status', 'r8')], radius=5,
         seeds=1523, matched=1084, positives=[f'{B}/e2', f'{S}/e75', f'{S}/e28', f'{R}/e1743', f'{R}/e1744'], kept=5),
    dict(name='workdir-five', seed='r9', against=[('path', 'r10'), ('outcome', 'r11')], radius=5,
         seeds=535, matched=13, positives=[f'{N}/e23', f'{R}/e4092', f'{R}/e4018', f'{R}/e4024', f'{R}/e4232'], kept=0),
    dict(name='prompt-zero', seed='r12', against=[('configuration', 'r13'), ('outcome', 'r14')], radius=0,
         seeds=319, matched=0, positives=[f'{P}/e28', f'{P}/e126', f'{P}/e128'], kept=0),
    dict(name='prompt-five', seed='r12', against=[('configuration', 'r13'), ('outcome', 'r14')], radius=5,
         seeds=319, matched=15, positives=[f'{P}/e28', f'{P}/e126', f'{P}/e128'], kept=2),
    dict(name='prompt-hundred', seed='r12', against=[('configuration', 'r13'), ('outcome', 'r14')], radius=100,
         seeds=319, matched=189, positives=[f'{P}/e28', f'{P}/e126', f'{P}/e128'], kept=3),
]


class EngineClient:
    """Historical structured engine client; never invokes native tools.

    response_chars measures serialized successful engine dictionaries only,
    not model-visible native text or exception diagnostics.
    """
    def __init__(self, root):
        self.root = str(Path(root).resolve())
        self.calls = self.response_chars = 0

    def call(self, name, **arguments):
        from azo_session_postmortem import engine
        self.calls += 1
        try:
            result = engine.dispatch(name, dict(arguments, workspace=self.root))
        except Exception as error:
            raise RuntimeError(str(error)) from error
        self.response_chars += len(json.dumps(result, ensure_ascii=False))
        return result

    def rows(self, first, tool):
        rows, page = [], first
        for _ in range(100):
            rows.extend(page['rows'])
            if not page.get('next_cursor'):
                return rows
            page = self.call(tool, cursor=page['next_cursor'], page_size=5000, max_chars=8000000)
        raise AssertionError('Paging limit exceeded; no complete-run claim')


def check_case(client, case):
    calls, chars = client.calls, client.response_chars
    radius = case['radius']
    first = client.call('trace_cooccur', seed={'result': case['seed']}, against=[
        dict(name=name, selection={'result': result}, before=radius, after=radius)
        for name, result in case['against']], page_size=3, max_chars=6000)
    assert first['status'] == 'complete'
    matched = client.rows(first, 'trace_cooccur')
    unmatched = client.rows(client.call('trace_query', cursor=first['unmatched_cursor'],
                            page_size=5000, max_chars=8000000), 'trace_query') if first['unmatched_cursor'] else []
    yes, no = {r['ref'] for r in matched}, {r['ref'] for r in unmatched}
    assert len(yes) == len(matched) == case['matched'] == first['counts']['matched_seed_events']
    assert len(no) == len(unmatched) == first['counts']['unmatched_seed_events']
    assert not yes & no and len(yes | no) == case['seeds'] == first['counts']['seed_events']
    assert sum(ref in yes for ref in case['positives']) == case['kept']
    for row in matched:
        assert not row['failed_conditions']
        for condition in row['conditions']:
            assert condition['status'] == 'within_window' and condition['witness_count'] > 0
            assert all(-radius <= w['delta_seq'] <= radius for w in condition['witnesses'])
    assert all(row['failed_conditions'] for row in unmatched)
    nearest = None
    if case['name'] == 'prompt-five':
        distant = next(row for row in unmatched if row['ref'] == f'{P}/e28')
        outcome = next(c for c in distant['conditions'] if c['name'] == 'outcome')
        witness = outcome['witnesses'][0]
        assert outcome['status'] == 'outside_window' and witness['delta_seq'] == 98
        assert witness['ref'] == f'{P}/e126'
        receipt = client.call('trace_read', targets=[{'result': outcome['witnesses_result'],
                              'rows': [witness['row']]}], pair=False, line_context=0, max_chars=1000)
        assert any(b.get('anchor', {}).get('event') == witness['ref'] for b in receipt['blocks'])
        nearest = dict(seed=distant['ref'], witness=witness['ref'], delta_seq=98, readable=True)
    summary = dict(case=case['name'], seed_events=len(yes | no), matched_events=len(yes),
                   unmatched_events=len(no), positive_targets=len(case['positives']),
                   positives_kept=case['kept'], nearest_outside=nearest,
                   calls=client.calls-calls, engine_response_chars=client.response_chars-chars)
    return summary, yes


def main():
    from azo_session_postmortem import engine
    from scripts.check_workspace import map_state
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    original = (args.workspace / 'trace.sqlite3').resolve()
    with tempfile.TemporaryDirectory(prefix='cooccur-replay-') as temporary:
        root = Path(temporary)
        with sqlite3.connect(original.as_uri() + '?mode=ro', uri=True) as source:
            before = map_state(source)
            assert source.execute('SELECT count(*) FROM events').fetchone()[0] == 5842
            with sqlite3.connect(root / 'trace.sqlite3') as target:
                source.backup(target)
        client = EngineClient(root)
        summaries, membership = [], {}
        for case in CASES:
            summary, refs = check_case(client, case)
            summaries.append(summary)
            membership[case['name']] = refs
        assert membership['prompt-zero'] <= membership['prompt-five'] <= membership['prompt-hundred']
        with sqlite3.connect(root / 'trace.sqlite3') as copy:
            assert before == map_state(copy)
        report = dict(status='passed', copied_workspace_only=True, map_unchanged=True,
                      cases=summaries, comparison='engine_cooccur_compatibility', response_format='engine_json',
                       engine_module=engine.__file__, python=sys.executable,
                      interpretation='Historical engine compatibility only; not native validation or investigator efficacy.')
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
