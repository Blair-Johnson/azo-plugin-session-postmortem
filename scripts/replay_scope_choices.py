"""Compare effective scopes of frozen investigator searches, not reasoning quality."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile

from azo_session_postmortem.engine import dispatch
from scripts.check_workspace import map_state
from scripts.prepare_cooccur_trials import readonly, fingerprint

SOLVERS = {'3fa02122-d57f-4a33-8927-822cc69f427c': 'G',
           'ac446462-db8e-4d26-a5e8-c2d4aee8b68e': 'H'}


def query(workspace, sql, params=None):
    page = dispatch('trace_query', dict(workspace=str(workspace), sql=sql,
                    params=params, page_size=5000, max_chars=1000000))
    rows = list(page['rows'])
    while page.get('next_cursor'):
        page = dispatch('trace_query', dict(workspace=str(workspace),
                        cursor=page['next_cursor'], page_size=5000, max_chars=1000000))
        rows.extend(page['rows'])
    return rows


def copy_index(source_path, destination):
    destination.mkdir()
    with readonly(source_path / 'trace.sqlite3') as source:
        before = (fingerprint(source), map_state(source))
        with sqlite3.connect(destination / 'trace.sqlite3') as target:
            source.backup(target)
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('investigators', type=Path)
    parser.add_argument('sources', type=Path, help='Round4 directory with G-source and H-source')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'copied_workspaces_only': True, 'cases': []}
    with tempfile.TemporaryDirectory(prefix='scope-choice-replay-') as temporary:
        root = Path(temporary)
        indexes = {case: root / case for case in ('G', 'H')}
        original = {case: copy_index(args.sources / (case + '-source'), index)
                    for case, index in indexes.items()}
        copy_index(args.investigators, root / 'investigators')
        calls = query(root / 'investigators', 'SELECT ref,session,arguments FROM events '
            "WHERE kind='call' AND tool='trace_search' AND session IN (:g,:h) ORDER BY session,seq",
            dict(zip(('g', 'h'), SOLVERS)))
        assert len(calls) == 5, 'Expected five frozen round5 searches'
        for call in calls:
            request = {key: value for key, value in json.loads(call['arguments']).items()
                       if value is not None}  # Match native wrapper's argument forwarding.
            case, variants = SOLVERS[call['session']], {}
            request.update(workspace=str(indexes[case]), save_as=None)
            for policy in (False, True):
                response = dispatch('trace_search', dict(request, include_references=policy))
                summary = dict(response['scope_summary'])
                summary.pop('include_references')
                anchors = query(indexes[case], 'SELECT row,ref,anchor,entry_id '
                    'FROM result_items WHERE result_id=:result ORDER BY row,ref,anchor,entry_id',
                    {'result': response['result']})
                variants[policy] = {'summary': summary, 'anchors': anchors,
                    'matched': response['matched_total'], 'unavailable': response['unavailable_count']}
            no, yes = variants[False], variants[True]
            report['cases'].append(dict(call=call['ref'], task=case,
                scope_kind=next(iter(request['scope'])),
                matched_false=no['matched'], matched_true=yes['matched'],
                objects_false=no['summary']['object_count'], objects_true=yes['summary']['object_count'],
                identical_effective_summary=no['summary'] == yes['summary'],
                identical_retained_anchors=no['anchors'] == yes['anchors'],
                identical_unavailable=no['unavailable'] == yes['unavailable']))
        for case, index in indexes.items():
            with readonly(index / 'trace.sqlite3') as db:
                assert (fingerprint(db), map_state(db)) == original[case]
    report['source_tables_and_maps_unchanged'] = True
    report['interpretation'] = 'Actual-query counterfactual replay; no fresh-agent efficacy claim.'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
