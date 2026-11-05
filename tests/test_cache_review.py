"""Final cache correctness regressions through the real dispatcher."""
import json
from pathlib import Path
import sqlite3

import pytest

from azo_session_postmortem import engine
from test_cache import blocks, join, query, study  # shared source fixture


def put(study, name, anchors):
    study.call('trace_edit', put=[{'id': name, 'kind': 'episode',
                                  'title': name, 'anchors': anchors}])


def anchors(study, name):
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        return json.loads(db.execute('SELECT anchors FROM map_entries WHERE id=?', (name,)).fetchone()[0])


@pytest.mark.parametrize('selector', [
    'T/e1', {'session': 'T', 'events': [1, 2]},
    {'event': 'T/e1', 'field': 'text', 'lines': [2, 2]},
])
def test_read_alias_freezes_map_source_membership(study, selector):
    put(study, 'original', [selector])
    original = study.call('trace_read', targets=[{'entry': 'original', 'part': 'anchors'}],
                          pair=False, line_context=0)
    alias = original['document']['id']
    expected = [b['anchor'] for b in blocks(original, 'evidence')]
    put(study, 'original', ['T/e140'])
    for target in (alias, {'items': [alias]}, {'result': alias}):
        reread = study.call('trace_read', targets=[target], pair=False, line_context=0)
        assert [b['anchor'] for b in blocks(reread, 'evidence')] == expected
    assert not blocks(study.call('trace_search', scope={'items': [alias]},
                                 query='event 140', mode='literal'), 'hit')
    put(study, 'copy', [alias])
    assert anchors(study, 'copy') == (['T/e1', 'T/e2'] if isinstance(selector, dict) and 'session' in selector else [selector])
    assert Path(original['path']).read_bytes().decode() == original['text']


def test_nested_result_map_anchors_freeze_and_restrictions_remain_local(study):
    put(study, 'original', ['T/e2'])
    # The canonical anchor column can point at a live map-anchor selector.
    selected = study.call('trace_query', sql="SELECT '{\"entry\":\"original\",\"part\":\"anchors\"}' AS anchor")
    seed = query(study, 'seq=1')['document']['id']
    witness = query(study, 'seq=2')['document']['id']
    cooccur = join(study, {'result': seed}, {'result': witness})['document']['id']
    original = study.call('trace_read', targets=[selected['document']['id'], cooccur], pair=False)
    nested = study.call('trace_read', targets=[original['document']['id']], pair=False)
    put(study, 'original', ['T/e140'])
    alias = nested['document']['id']
    read = study.call('trace_read', targets=[alias], pair=False)
    assert {b['ref'] for b in blocks(read, 'evidence')} == {'T/e1', 'T/e2'}
    assert not blocks(study.call('trace_search', scope={'items': [alias]}, query='object-only'), 'hit')
    mixed = study.call('trace_read', targets=[alias, 'T/e1'], pair=False)
    assert len(blocks(study.call('trace_search', scope={'items': [mixed['document']['id']]},
                                 query='object-only'), 'hit')) == 1
    put(study, 'copy', [alias])
    assert set(anchors(study, 'copy')) == {'T/e1', 'T/e2'}
    joined = join(study, {'result': alias}, {'result': witness})
    assert blocks(joined, 'matched')


def test_catalog_session_retains_objects_and_excludes_cross_session_links(study):
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('U', None, 'Other', '/not-read', '[]'))
        for ref, sid, text in [('T/orphan', 'T', 'ORPHAN_TOKEN'), ('U/foreign', 'U', 'FOREIGN_TOKEN')]:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', (ref, 'buffer', 1, len(text), 1, sid, None))
            db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', text))
        db.execute('INSERT INTO links VALUES(?,?,?)', ('T/e1', 'U/foreign', 'references'))
    opened = study.call('trace_open')
    item = next(b['id'] for b in blocks(opened, 'session') if b['title'].startswith('Session T '))
    for scope in ({'sessions': ['T']}, {'items': [item]}, {'refs': [item]}):
        found = study.call('trace_search', scope=scope, query='ORPHAN_TOKEN', mode='literal')
        assert [b['anchor']['object'] for b in blocks(found, 'hit')] == ['T/orphan']
        assert not blocks(study.call('trace_search', scope=scope, query='FOREIGN_TOKEN'), 'hit')
    read = study.call('trace_read', targets=[item], pair=False)
    refs = {b['ref'] for b in blocks(read, 'evidence')}
    assert {'T/orphan', 'T/o1'} <= refs and 'U/foreign' not in refs
    assert not blocks(study.call('trace_search', scope={'items': [read['document']['id']]},
                                 query='FOREIGN_TOKEN'), 'hit')
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', ('T/later', 'buffer', 1, 5, 1, 'T', None))
        db.execute('INSERT INTO _content VALUES(?,?,?)', ('T/later', 'text', 'LATER'))
    assert not blocks(study.call('trace_search', scope={'items': [item]}, query='LATER'), 'hit')


def test_unavailable_search_evidence_survives_complete_real_delivery(study, monkeypatch):
    monkeypatch.undo()  # Exercise the actual full renderer and durable text.
    missing = [f'T/missing{i}' for i in range(engine.READ_PREVIEW_LIMIT + 1)]
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.executemany('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                       [(ref, 'buffer', 0, 0, 0, 'T', 'Export omitted its body') for ref in missing])
    answer = study.call('trace_search', scope={'refs': [{'object': ref} for ref in missing]},
                        query='ABSENT', mode='literal')
    assert not blocks(answer, 'hit')
    coverage = blocks(answer, 'coverage')
    assert len(coverage) == len(missing)
    for ref in missing:
        assert ref in answer['text']
        assert any(ref in block['title'] and 'Not searched: Export omitted its body' in block['text']
                   for block in coverage)
    assert Path(answer['path']).read_bytes().decode() == answer['text']
    read = study.call('trace_read', targets=[coverage[-1]['id']], pair=False)
    assert blocks(read, 'evidence')[0]['available'] is False
    empty = study.call('trace_search', scope={'refs': ['T/e1']}, query='ABSENT')
    assert not blocks(empty, 'coverage') and 'was not searched' not in empty['text']
    success = study.call('trace_search', scope={'refs': ['T/e1']}, query='needle')
    assert not blocks(success, 'coverage')


def test_read_groups_follow_original_requests_not_expanded_blocks(study):
    answer = study.call('trace_read', targets=[{'session': 'T', 'events': [1, 100]}, 'T/e140'],
                        pair=False, preview_chars=1200)
    evidence = blocks(answer, 'evidence')
    assert [b['request_group'] for b in evidence] == [0] * 100 + [1]
    assert [b['ref'] for b in evidence] == [f'T/e{i}' for i in range(1, 101)] + ['T/e140']


def test_mixed_explicit_requests_keep_plan_order_and_original_groups(study):
    put(study, 'note', ['T/e2'])
    targets = ['T/e140', {'event': 'T/e2', 'field': 'text', 'lines': [2, 2]},
               {'entry': 'note', 'part': 'card'}, {'session': 'T', 'events': [4, 5]}]
    answer = study.call('trace_read', targets=targets, pair=False, line_context=0)
    evidence = blocks(answer, 'evidence')
    assert [b['request_group'] for b in evidence] == [2, 1, 0, 3, 3]
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.row_factory = sqlite3.Row
        expected = engine._read_plan(db, targets, 0, 0, False, 0)
    assert [b['text'] for b in evidence if 'ref' in b] == [b['text'] for b in expected if 'ref' in b]


@pytest.mark.parametrize('key', ['rows', 'group'])
@pytest.mark.parametrize('operation', ['trace_read', 'trace_search', 'trace_cooccur', 'trace_edit'])
def test_present_nested_null_is_rejected(study, key, operation):
    alias = query(study, 'seq=1')['document']['id']
    selector = {'result': alias, key: None}
    args = {'trace_read': {'targets': [selector]},
            'trace_search': {'scope': selector, 'query': 'needle'},
            'trace_cooccur': {'seed': selector, 'against': [
                {'name': 'near', 'selection': {'result': alias}, 'before': 0, 'after': 0}]},
            'trace_edit': {'put': [{'id': 'bad', 'kind': 'episode', 'title': 'bad', 'anchors': [selector]}]}}
    with pytest.raises(ValueError, match=key):
        study.call(operation, **args[operation])


def test_source_titles_distinguish_user_and_tool_without_changing_bodies(study):
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.execute("UPDATE events SET role='tool',tool='terminal_run',kind='result' WHERE ref='T/e2'")
    read = study.call('trace_read', targets=['T/e1', 'T/e2', 'T/o1'], pair=False)
    evidence = blocks(read, 'evidence')
    assert 'user' in evidence[0]['title']
    assert all(label in evidence[1]['title'] for label in ['T/e2', 'tool', 'terminal_run', 'result'])
    assert 'buffer' in evidence[2]['title']
    assert evidence[1]['text'] == 'needle event 2 Ω\r\nsecond line 2\r\n'
    hits = blocks(study.call('trace_search', scope={'refs': ['T/e2']}, query='needle'), 'hit')
    assert all(label in hits[0]['title'] for label in ['T/e2', 'tool', 'terminal_run', 'result'])


@pytest.mark.parametrize('targets,groups', [
    ([{'event': 'T/e1', 'field': 'text', 'lines': [2, 2]},
      {'event': 'T/e1', 'field': 'text', 'lines': [1, 1]}], [1, 0]),
    (['T/e1', {'event': 'T/e1', 'field': 'text', 'lines': [2, 2]}], [1, 0]),
])
def test_merged_and_overlapping_windows_keep_original_request_ownership(study, targets, groups):
    answer = study.call('trace_read', targets=targets, pair=False, line_context=0)
    evidence = blocks(answer, 'evidence')
    assert [b['request_group'] for b in evidence] == groups
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.row_factory = sqlite3.Row
        canonical = engine._read_plan(db, targets, 0, 0, False, 0)
    assert ''.join(b['text'] for b in evidence) == ''.join(b['text'] for b in canonical)
    assert all('\r\n' in b['text'] for b in evidence)
