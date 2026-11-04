"""Known-case producer navigation regression, not an investigator efficacy trial."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.replay_cooccur import NativeClient
from scripts.check_workspace import map_state

PARENT = '0ca0982e-7080-472a-8c28-67f3ad2c16f0'
CHILD = 'ef6adac4-4da5-4517-a171-9403cec73d5e'


def source_digest(db):
    digest = hashlib.sha256()
    for table in ('sessions', 'events', 'objects', 'links', '_content', '_sources'):
        digest.update(table.encode())
        columns = len(db.execute(f'PRAGMA table_info({table})').fetchall())
        order = ','.join(str(n) for n in range(1, columns + 1))
        for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
            digest.update(b'\n')
    return digest.hexdigest()


def main():
    from azo_session_postmortem import native
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    original = (args.workspace / 'trace.sqlite3').resolve()
    with tempfile.TemporaryDirectory(prefix='navigation-replay-') as temporary:
        root = Path(temporary)
        with sqlite3.connect(original.as_uri() + '?mode=ro', uri=True) as source:
            before, raw_before = map_state(source), source_digest(source)
            with sqlite3.connect(root / 'trace.sqlite3') as target:
                source.backup(target)
        client = NativeClient(root)
        opened = client.call('trace_open')
        assert opened['counts']['sessions'] == 58
        handoff = client.call('trace_read', targets=[{'event': f'{PARENT}/e2712',
            'field': 'text'}], pair=False, line_context=0, output='compact', max_chars=8000)
        links = [row for row in handoff['session_navigation']
                 if row['dst'] == CHILD and row['kind'] == 'rlm_output']
        assert links, 'Recorded handoff producer not exposed'
        link = next(row for row in links if row.get('via') == f'{PARENT}/o_buf_f58d78')
        assert link['indexed'] and link['anchor']['event'] == f'{PARENT}/e2648'
        receipt = client.call('trace_read', targets=[link['anchor']], pair=False,
                              line_context=0, max_chars=8000)
        text = '\n'.join(block.get('text', '') for block in receipt['blocks'])
        assert CHILD in text and 'buf_f58d78' in text
        children = client.call('trace_query', sql='SELECT ref,seq,kind,tool FROM events '
            'WHERE session=:child AND seq IN (79,80,86) ORDER BY seq', params={'child': CHILD})
        assert [row['seq'] for row in children['rows']] == [79, 80, 86]
        client.call('trace_read', targets=[{'event': f'{CHILD}/e80', 'field': 'text'}],
                    pair=False, line_context=0, max_chars=1000)
        try:
            client.call('trace_read', targets=[{'event': f'{PARENT}/e2757', 'field': 'arguments'}])
        except RuntimeError as error:
            assert all(part in str(error) for part in (f'{PARENT}/e2757', 'arguments', 'text'))
        else:
            raise AssertionError('Known invalid field unexpectedly succeeded')
        with sqlite3.connect(root / 'trace.sqlite3') as copy:
            assert before == map_state(copy)
            assert raw_before == source_digest(copy)
        report = dict(status='passed', copied_workspace_only=True, map_unchanged=True,
            source_tables_unchanged=True, handoff=f'{PARENT}/e2712', child=CHILD,
            launch_evidence=link['anchor'], child_receipts=[row['ref'] for row in children['rows']],
            missing_field_diagnostic=True, calls=client.calls, response_chars=client.response_chars,
            native_module=native.__file__, interpretation='Known-case retrieval regression, not efficacy.')
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))


if __name__ == '__main__':
    main()
