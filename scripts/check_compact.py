"""Historical engine-only inline/compact JSON comparison on disposable index copies.

This does not measure native tool responses, which are plain text.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

# Validate the run snapshot rather than an editable package elsewhere on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def normalize(page):
    page = json.loads(json.dumps(page))
    packet_format = page.pop('packet_format')
    assert packet_format in ('compact_json', 'compact_read_json_v1')
    page.pop('packet_chars')
    deliveries = []
    for block in page['blocks']:
        block.setdefault('requested', True)
        if 'card' in block:
            delivery = dict(entry=block['entry_id'], part='card')
        elif 'text' in block:
            block.setdefault('char_offset', 0)
            block.setdefault('char_end', block['char_offset'] + len(block['text']))
            block.setdefault('complete', True)
            delivery = (dict(anchor=block['anchor']) if 'anchor' in block else
                        dict(entry=block['entry_id'], part='card'))
            delivery.update(char_offset=block['char_offset'], char_end=block['char_end'])
        else:
            delivery = dict(anchor=block.get('anchor'), available=False)
        deliveries.append(delivery)
    if packet_format == 'compact_read_json_v1':
        page.setdefault('delivered', deliveries)
    assert len(page['delivered']) == len(deliveries)
    for block, actual, rebuilt in zip(page['blocks'], page['delivered'], deliveries):
        if actual != rebuilt:
            assert 'anchor' not in block and 'text' not in block and 'card' not in block, \
                'Anchored deliveries must be reconstructible'
    return page


def reference_state(root, page):
    state = {}
    with sqlite3.connect((root / 'trace.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        for key, table, columns in (('references_result', '_results', 'kind,rows,meta'),
                                     ('references_cursor', '_cursors', 'kind,payload')):
            if key in page:
                row = db.execute(f'SELECT {columns} FROM {table} WHERE id=?',
                                 (int(page[key][1:]),)).fetchone()
                assert row is not None
                state[key] = row
    return state


def main():
    from azo_session_postmortem import engine
    from scripts.check_workspace import map_state
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    parser.add_argument('--read-json', required=True)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    request = json.loads(args.read_json)
    assert isinstance(request, dict) and not {'workspace', 'inspect', 'output'} & request.keys()
    original = args.workspace.expanduser().resolve() / 'trace.sqlite3'
    with tempfile.TemporaryDirectory(prefix='postmortem-compact-') as temp:
        roots = {}
        for mode in ('inline', 'compact', 'switch'):
            root = Path(temp) / mode
            root.mkdir()
            with sqlite3.connect(original.as_uri() + '?mode=ro', uri=True) as source:
                before = map_state(source)
                with sqlite3.connect(root / 'trace.sqlite3') as target:
                    source.backup(target)
            roots[mode] = root
        def invoke(mode, arguments):
            page = engine.dispatch('trace_read', dict(arguments, workspace=str(roots[mode])))
            # Match the historical wrapper's JSON serialization, not native text.
            serialized = json.dumps(page, ensure_ascii=False)
            return page, len(serialized)
        totals = dict(pages=0, inline_chars=0, compact_chars=0, text_chars=0)
        current = request
        while True:
            inline, full_size = invoke('inline', dict(current, output='inline'))
            compact, small_size = invoke('compact', dict(current, output='compact'))
            switched, _ = invoke('switch', dict(current, output='compact' if totals['pages'] % 2 else 'inline'))
            assert normalize(inline) == normalize(compact) == normalize(switched)
            assert (reference_state(roots['inline'], inline) ==
                    reference_state(roots['compact'], compact) ==
                    reference_state(roots['switch'], switched))
            assert small_size <= full_size
            totals['pages'] += 1
            totals['inline_chars'] += full_size
            totals['compact_chars'] += small_size
            totals['text_chars'] += inline['text_chars']
            assert totals['pages'] <= 1000, 'Replay limit; no complete-run claim'
            if not inline.get('next_cursor'):
                break
            current = dict(cursor=inline['next_cursor'], max_chars=request.get('max_chars', 12000))
        for root in roots.values():
            with sqlite3.connect(root / 'trace.sqlite3') as copied:
                assert before == map_state(copied)
        totals['saved_chars'] = totals['inline_chars'] - totals['compact_chars']
        print(json.dumps(dict(label=args.label, **totals, same_evidence_and_navigation=True,
                              maps_unchanged=True, copied_workspace_only=True,
                              comparison="engine_inline_vs_compact_json",
                              engine_module=engine.__file__, python=sys.executable)))


if __name__ == '__main__':
    main()
