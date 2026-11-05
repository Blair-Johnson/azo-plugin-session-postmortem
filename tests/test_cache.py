"""Indexed backend contracts, independent of preview/host renderer policy."""
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from azo_session_postmortem import cache, engine


@pytest.fixture
def study(tmp_path, monkeypatch):
    # Backend tests assert complete materialization, not another agent's renderer.
    def render(document):
        return document['id'] + '\n' + '\n\n'.join(
            block['id'] + '\n' + block['text'] for block in document['blocks'])
    monkeypatch.setitem(sys.modules, 'azo_session_postmortem.cache_render',
                        SimpleNamespace(render_full=render))
    source = tmp_path / 'source.json'
    source.write_text(json.dumps({
        'format_version': 2,
        'meta': {'session_id': 'T', 'created_at': '2026-01-01T00:00:00Z'},
        'attrs': {'session_title': 'Cache fixture'}, 'components': {}, 'core': {},
        'entries': [{'index': i, 'step': i, 'messages': [
            {'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'needle event {i + 1} Ω\r\nsecond line {i + 1}\r\n'}]}
            for i in range(140)],
        'trace_objects': [{'ref': 'T/o1', 'kind': 'buffer', 'session': 'T',
                           'text': 'before α\r\nobject-only needle\r\nafter Ω\r\n', 'available': True}],
        'trace_links': [{'src': 'T/e1', 'dst': 'T/o1', 'kind': 'references'}],
    }), encoding='utf-8')
    workspace = tmp_path / 'workspace'
    def call(tool, **args):
        return cache.dispatch(tool, {'workspace': str(workspace), **args})
    opened = call('trace_open', sessions=[str(source)], descendants=False)
    return SimpleNamespace(workspace=workspace, source=source, call=call, opened=opened)


def blocks(answer, group=None):
    return [b for b in answer['document']['blocks'] if group is None or b['group'] == group]


def query(study, where='1', **args):
    return study.call('trace_query', sql='SELECT ref,seq FROM events WHERE ' + where + ' ORDER BY seq', **args)


def join(study, seed, witness, **args):
    return study.call('trace_cooccur', seed=seed, against=[
        {'name': 'near', 'selection': witness, 'before': 0, 'after': 1}], **args)


def test_search_and_query_complete_without_public_pages(study, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('display pages must not be called')
    monkeypatch.setattr(engine, '_search_page', forbidden)
    monkeypatch.setattr(engine, '_query_page', forbidden)
    result = study.call('trace_search', query='needle', mode='literal', preview_chars=0)
    assert len(blocks(result, 'hit')) == 141
    assert result['preview_chars'] == 0
    assert 'needle event 140' in result['text']
    assert Path(result['path']).read_bytes().decode() == result['text']
    result = query(study)
    assert len(blocks(result)) == 140
    assert 'seq: 140' in result['text']
    huge = study.call('trace_query', sql="SELECT ref, printf('%.*c',20000,'x') AS huge FROM events LIMIT 1")
    assert 'x' * 20000 in huge['text']


def test_read_over_64_blocks_exact_and_unpaged(study, monkeypatch):
    monkeypatch.setattr(engine, '_read_page', lambda *a, **k: pytest.fail('read paging used'))
    answer = study.call('trace_read', targets=[{'session': 'T', 'events': [1, 140]}], pair=False)
    assert len(blocks(answer, 'evidence')) == 140
    assert 'needle event 140 Ω\r\nsecond line 140\r\n' in answer['text']
    assert Path(answer['path']).read_bytes().decode() == answer['text']
    assert len({b['id'] for b in blocks(answer)}) == len(blocks(answer))


def test_search_context_and_exact_alias_membership(study):
    answer = study.call('trace_search', query='object-only', mode='literal', context_lines=1)
    hit = blocks(answer, 'hit')[0]
    assert hit['text'] == 'before α\r\nobject-only needle\r\nafter Ω\r\n'
    exact = study.call('trace_read', targets=[hit['id']], line_context=0)
    assert blocks(exact, 'evidence')[0]['text'] == 'object-only needle\r\n'
    assert not blocks(study.call('trace_search', query='before', mode='literal',
                                scope={'items': [hit['id']]}), 'hit')
    edit = study.call('trace_edit', put=[{'id': 'hit-note', 'kind': 'episode', 'title': 'Hit',
                                       'anchors': [hit['id']]}])
    assert set(edit) == {'document', 'preview_chars'}
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        anchors = json.loads(db.execute("SELECT anchors FROM map_entries WHERE id='hit-note'").fetchone()[0])
    assert anchors == [{'object': 'T/o1', 'lines': [2, 2]}]


def test_alias_rows_items_copy_and_missing(study, tmp_path):
    answer = query(study, 'seq <= 3')
    public = answer['document']['id']
    first = blocks(answer)[0]['id']
    assert 'needle event 1' in study.call('trace_read', targets=[first])['text']
    assert 'needle event 3' in study.call('trace_read', targets=[{'result': public, 'rows': [2]}])['text']
    assert len(blocks(study.call('trace_search', query='needle', mode='literal',
                                scope={'items': [first]}, follow_object_links=False), 'hit')) == 1
    copy = tmp_path / 'copy'
    shutil.copytree(study.workspace, copy)
    answer = cache.dispatch('trace_read', {'workspace': str(copy), 'targets': [first]})
    assert 'needle event 1' in answer['text']
    assert Path(answer['path']).parent == copy / 'results'
    for missing in ('S999999-H001', 'Q999999', 'foreign/ref'):
        with pytest.raises(ValueError, match='unknown'):
            study.call('trace_read', targets=[missing])
    with pytest.raises(ValueError, match='out of range'):
        study.call('trace_read', targets=[{'result': public, 'rows': [3]}])


def test_aggregate_aliases_rejected_as_source(study):
    answer = study.call('trace_query', sql='SELECT count(*) AS n FROM events')
    item = blocks(answer)[0]['id']
    for invoke in (
        lambda: study.call('trace_read', targets=[item]),
        lambda: study.call('trace_search', query='needle', scope={'items': [item]}),
        lambda: join(study, {'items': [item]}, {'items': [item]}),
        lambda: study.call('trace_edit', put=[{'id': 'bad', 'kind': 'episode', 'title': 'Bad', 'anchors': [item]}]),
    ):
        with pytest.raises(ValueError, match='non-navigable'):
            invoke()


def test_cooccur_all_groups_witness_links_and_alias_joins(study, monkeypatch):
    from azo_session_postmortem import cooccur
    monkeypatch.setattr(cooccur, '_page', lambda *a, **k: pytest.fail('join paging used'))
    seeds = query(study, 'seq IN (1,10)')['document']['id']
    witnesses = query(study, 'seq=2')['document']['id']
    answer = join(study, {'result': seeds}, {'result': witnesses}, save_as='join')
    public = answer['document']['id']
    assert len(blocks(answer, 'matched')) == 1
    assert len(blocks(answer, 'unmatched')) == 1
    assert len(blocks(answer, 'witness')) == 2
    assert 'nearest outside window' in blocks(answer, 'unmatched')[0]['links'][0]['label']
    assert '-8' in blocks(answer, 'unmatched')[0]['links'][0]['label']
    for group, expected in [('matched', 'event 1'), ('unmatched', 'event 10'), ('witnesses', 'event 2')]:
        selected = study.call('trace_read', targets=[{'result': public, 'group': group}], pair=False)
        assert expected in selected['text']
    witness = blocks(answer, 'witness')[1]['id']
    assert 'event 2' in study.call('trace_read', targets=[witness])['text']
    repeated = join(study, {'result': public, 'group': 'unmatched'}, {'items': [witness]})
    assert len(blocks(repeated, 'unmatched')) == 1
    assert len(blocks(study.call('trace_cooccur', saved='join'), 'unmatched')) == 1


def test_join_search_no_implicit_objects_survives_aliases(study):
    seed = query(study, 'seq=1')['document']['id']
    witness = query(study, 'seq=2')['document']['id']
    answer = join(study, {'result': seed}, {'result': witness})
    item = blocks(answer, 'matched')[0]['id']
    for scope in ({'items': [item]}, {'result': answer['document']['id']},
                  {'result': answer['document']['id'], 'group': 'matched', 'rows': [0]}):
        found = study.call('trace_search', query='object-only', mode='literal', scope=scope)
        assert not blocks(found, 'hit')
    # Exact anchor membership stays whole-event for map edits and ordinary reads.
    study.call('trace_edit', put=[{'id': 'seed', 'kind': 'episode', 'title': 'Seed', 'anchors': [item]}])
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        assert json.loads(db.execute("SELECT anchors FROM map_entries WHERE id='seed'").fetchone()[0]) == ['T/e1']


def test_collapse_all_occurrences_and_saved_item_scope(study):
    answer = study.call('trace_search', query='needle', scope={'refs': ['T/e1']}, mode='literal',
                        follow_object_links=False, context_lines=0)
    item = blocks(answer, 'hit')[0]['id']
    scoped = study.call('trace_search', query='needle', scope={'items': [item]}, mode='literal',
                        context_lines=0, save_as='scoped')
    repeated = study.call('trace_search', saved='scoped')
    assert [b['text'] for b in blocks(scoped)] == [b['text'] for b in blocks(repeated)]
    collapsed = study.call('trace_search', query='^', mode='regex', scope={'refs': ['T/e1', 'T/e2']},
                          follow_object_links=False, collapse='exact')
    assert blocks(collapsed, 'occurrence')
    for block in blocks(collapsed, 'occurrence'):
        assert study.call('trace_read', targets=[block['id']], line_context=0)['document']['blocks']


def test_open_complete_catalog_map_recipes_prior_paths(study):
    study.call('trace_edit', put=[{'id': f'n{i}', 'kind': 'note', 'title': f'Note {i}',
                                'note': 'long note ' + 'z' * 500, 'pinned': i == 19} for i in range(20)])
    prior = query(study, 'seq=1', save_as='one')
    answer = study.call('trace_open')
    assert blocks(answer)[0]['group'] == 'pinned'
    assert len(blocks(answer, 'map')) == 19
    assert 'z' * 500 in blocks(answer, 'pinned')[0]['text']
    assert blocks(answer, 'recipe')
    matching = [b for b in blocks(answer, 'result') if prior['document']['id'] in b['title']][0]
    assert prior['path'] in matching['text']
    assert 'needle event 1' in study.call('trace_read', targets=[matching['id']])['text']
    maps = study.call('trace_search', query='long note', mode='literal', on='map')
    assert len(blocks(maps, 'map')) == 20
    assert 'z' * 500 in blocks(maps, 'map')[0]['text']


@pytest.mark.parametrize('legacy', [False, True])
def test_refresh_refuses_stale_alias_and_saved_selection(study, legacy):
    query_result = query(study, 'seq=1')
    item = blocks(query_result)[0]['id']
    study.call('trace_search', query='needle', scope={'items': [item]}, save_as='old')
    payload = json.loads(study.source.read_text())
    payload['entries'][0]['messages'][0]['content'] = 'different evidence'
    study.source.write_text(json.dumps(payload))
    if legacy:
        engine.dispatch('trace_open', {'workspace': str(study.workspace), 'refresh': True})
    else:
        study.call('trace_open', refresh=True)
    for invoke in (lambda: study.call('trace_read', targets=[item]),
                   lambda: study.call('trace_search', saved='old')):
        with pytest.raises(ValueError, match='stale'):
            invoke()
    fresh = query(study, 'seq=1')
    assert fresh['document']['id'] != query_result['document']['id']
    assert 'different evidence' in study.call('trace_read', targets=[blocks(fresh)[0]['id']])['text']


@pytest.mark.parametrize('budget', [-1, True, 1.5, '12'])
def test_invalid_budget_before_side_effects(tmp_path, budget):
    workspace = tmp_path / 'never-created'
    with pytest.raises(ValueError, match='preview_chars'):
        cache.dispatch('trace_open', {'workspace': str(workspace), 'preview_chars': budget})
    assert not workspace.exists()


def test_nullable_defaults_and_legacy_structured_results(study):
    answer = study.call('trace_search', query='needle', mode=None, context_lines=None,
                        on=None, collapse=None, projection=None, follow_object_links=None)
    assert len(blocks(answer, 'hit')) == 141
    old = engine.dispatch('trace_query', {'workspace': str(study.workspace),
                          'sql': 'SELECT ref FROM events ORDER BY seq', 'page_size': 2})
    assert len(old['rows']) == 2 and old['next_cursor']
    selected = study.call('trace_read', targets=[{'result': old['result'], 'rows': [139]}])
    assert 'event 140' in selected['text']


def test_large_join_keeps_every_unmatched_seed_and_witness(study):
    seeds = query(study, 'seq < 140')['document']['id']
    witnesses = query(study, 'seq=140')['document']['id']
    answer = join(study, {'result': seeds}, {'result': witnesses})
    assert len(blocks(answer, 'matched')) == 1
    assert len(blocks(answer, 'unmatched')) == 138
    assert len(blocks(answer, 'witness')) == 139
    assert sum(len(b.get('links', [])) for b in blocks(answer)) == 139
    assert len(blocks(study.call('trace_read', targets=[{
        'result': answer['document']['id'], 'group': 'unmatched'}], pair=False), 'evidence')) == 138


def test_open_all_sessions_and_saved_query_nullable_defaults(study):
    payload = json.loads(study.source.read_text())
    payload['entries'] = payload['entries'][:1]
    payload['trace_objects'] = []
    payload['trace_links'] = []
    paths = []
    for index in range(12):
        payload['meta']['session_id'] = 'extra-' + str(index)
        path = study.source.parent / f'extra-{index}.json'
        path.write_text(json.dumps(payload))
        paths.append(str(path))
    opened = study.call('trace_open', sessions=paths, descendants=False)
    assert len(blocks(opened, 'session')) == 13
    initial = query(study, 'seq=1', save_as='firsts')
    repeated = study.call('trace_query', saved='firsts', sql=None, params=None, sample=None)
    assert [b['text'] for b in blocks(initial)] == [b['text'] for b in blocks(repeated)]


def test_read_context_and_all_relation_memberships(study):
    hit = study.call('trace_search', query='second line 2', mode='literal',
                     scope={'refs': ['T/e2']})['document']['blocks'][0]['id']
    answer = study.call('trace_read', targets=[hit], before=1, after=1, line_context=1)
    assert blocks(answer, 'evidence')[0]['text'] == 'second line 2\r\n'
    assert len(blocks(answer, 'context')) >= 3
    assert blocks(answer, 'navigation')
    selected = blocks(answer, 'context')[0]['id']
    assert study.call('trace_read', targets=[selected], line_context=0)['document']['blocks']


def test_nested_read_result_rows_keep_join_search_restriction(study):
    seeds = query(study, 'seq=1')['document']['id']
    witnesses = query(study, 'seq=2')['document']['id']
    joined = join(study, {'result': seeds}, {'result': witnesses})
    read = study.call('trace_read', targets=[{'result': joined['document']['id']}])
    result = study.call('trace_search', query='object-only', mode='literal',
                        scope={'result': read['document']['id'], 'rows': [0]})
    assert not blocks(result, 'hit')
    # A separate ordinary-event selector retains its ordinary object expansion.
    mixed = study.call('trace_read', targets=[{'result': joined['document']['id']}, 'T/e1'])
    result = study.call('trace_search', query='object-only', mode='literal',
                        scope={'result': mixed['document']['id'], 'rows': [1]})
    assert len(blocks(result, 'hit')) == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_disjoint_child_attachment_preserves_parent_aliases(study, legacy):
    selected = query(study, 'seq=1')
    item = blocks(selected)[0]['id']
    study.call('trace_search', query='needle', scope={'items': [item]},
               follow_object_links=False, save_as='parent-only')
    payload = json.loads(study.source.read_text())
    payload['meta']['session_id'] = 'child'
    payload['entries'] = payload['entries'][:1]
    payload['trace_objects'] = []
    payload['trace_links'] = []
    child = study.source.parent / 'child.json'
    child.write_text(json.dumps(payload))
    operation = engine.dispatch if legacy else cache.dispatch
    operation('trace_open', {'workspace': str(study.workspace),
                            'sessions': [str(child)], 'descendants': False})
    answer = study.call('trace_read', targets=[item], pair=False)
    assert [b['anchor']['event'] for b in blocks(answer, 'evidence')] == ['T/e1']
    repeated = study.call('trace_search', saved='parent-only')
    assert [b['anchor']['event'] for b in blocks(repeated, 'hit')] == ['T/e1']
    # Reloading an existing session remains conservative, even if unchanged.
    operation('trace_open', {'workspace': str(study.workspace),
                            'sessions': [str(study.source)], 'descendants': False})
    with pytest.raises(ValueError, match='stale'):
        study.call('trace_read', targets=[item])


def test_generated_presentation_is_readable_and_notes_come_first(study):
    note = 'Continue at the exact evidence below.\r\nKeep this authored line.\r\n'
    study.call('trace_edit', put=[{'id': 'continuation', 'kind': 'episode', 'title': 'Next step',
        'note': note, 'anchors': ['T/e1'], 'pinned': True, 'tags': ['review'],
        'fields': {'owner': 'investigator'}}])
    query(study, 'seq=1', save_as='first')
    opened = study.call('trace_open')
    assert blocks(opened, 'pinned')[0]['text'].startswith(note)
    assert 'Session: T' in blocks(opened, 'session')[0]['text']
    assert 'sql: SELECT ref,seq' in blocks(opened, 'recipe')[0]['text']
    assert 'Full text: ' in blocks(opened, 'result')[0]['text']
    for block in blocks(opened):
        assert not block['text'].startswith('{')
        assert '"anchors":' not in block['text']
    for answer in (study.call('trace_read', targets=[{'entry': 'continuation'}]),
                   study.call('trace_search', query='Continue', on='map')):
        assert blocks(answer)[0]['text'].startswith(note)
    # JSON that was actually selected is payload, not generated metadata.
    answer = study.call('trace_query', sql='SELECT ref, \'{"a":1}\' AS payload FROM events LIMIT 1')
    assert blocks(answer)[0]['text'] == 'ref: T/e1\npayload: {"a":1}'


def test_cooccur_bodies_are_literal_and_ranges_explicit(study):
    seeds = query(study, 'seq IN (1,10)')['document']['id']
    witnesses = query(study, 'seq=2')['document']['id']
    answer = join(study, {'result': seeds}, {'result': witnesses})
    assert blocks(answer, 'matched')[0]['text'] == 'needle event 1 Ω\r\nsecond line 1\r\n'
    assert blocks(answer, 'unmatched')[0]['text'] == 'needle event 10 Ω\r\nsecond line 10\r\n'
    assert all(b['text'] == 'needle event 2 Ω\r\nsecond line 2\r\n' for b in blocks(answer, 'witness'))
    assert 'near · +1 · within window [-0, +1]' in blocks(answer, 'matched')[0]['links'][0]['label']
    assert 'near · -8 · nearest outside window [-0, +1]' in blocks(answer, 'unmatched')[0]['links'][0]['label']
    assert '_anchors' not in answer['text'] and '"conditions"' not in answer['text']
    empty = query(study, '0')['document']['id']
    missing = join(study, {'result': seeds}, {'result': empty})
    assert 'near: no witness in session' in blocks(missing, 'unmatched')[0]['title']


def test_prior_result_alias_preserves_exact_retained_membership(study):
    seed = query(study, 'seq=1')['document']['id']
    witness = query(study, 'seq=2')['document']['id']
    joined = join(study, {'result': seed}, {'result': witness})
    opened = study.call('trace_open')
    prior = next(b for b in blocks(opened, 'result') if b['title'] == 'Prior result ' + joined['document']['id'])
    for target in (prior['id'], {'items': [prior['id']]}, {'result': prior['id']}):
        answer = study.call('trace_read', targets=[target], pair=False)
        assert [b['anchor']['event'] for b in blocks(answer, 'evidence')] == ['T/e1']
    found = study.call('trace_search', query='object-only', scope={'items': [prior['id']]})
    assert not blocks(found, 'hit')
    study.call('trace_edit', put=[{'id': 'prior', 'kind': 'episode', 'title': 'Prior',
                                 'anchors': [prior['id']]}])
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        assert json.loads(db.execute("SELECT anchors FROM map_entries WHERE id='prior'").fetchone()[0]) == ['T/e1']


@pytest.mark.parametrize('container', ['string', 'items', 'result'])
def test_recursive_alias_cycle_rejected(study, container):
    answer = query(study, 'seq=1')
    item = blocks(answer)[0]['id']
    target = {'string': item, 'items': {'items': [item]}, 'result': {'result': item}}[container]
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.execute('UPDATE _cache_items SET targets=? WHERE id=?', (json.dumps([target]), item))
    with pytest.raises(ValueError, match='cyclic cache alias'):
        study.call('trace_read', targets=[item])


def test_long_handoff_has_inline_child_launch_alias(study, monkeypatch):
    # Use the actual renderer for this cross-boundary preview regression only.
    monkeypatch.undo()
    child = 'ef6adac4-4da5-4517-a171-9403cec73d5e'
    job = 'rlm_job_1792099753654470000'
    receipt = (f'Submitted RLM task {job} with profile simple (Simple).\n'
               f'Child live session: {child}\n'
               'Output buffers reserved:\n- handoff -> buf_handoff [reserved]\n'
               f"Use rlm_status(job_id='{job}') to inspect it or cancel_rlm(job_id='{job}') to kill it.")
    body = 'Handoff start\r\n' + 'long source body\r\n' * 2000 + 'Handoff end\r\n'
    with sqlite3.connect(study.workspace / 'trace.sqlite3') as db:
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (child, 'T', 'Child investigation', '/not-read', '[]'))
        db.execute("UPDATE events SET kind='result',role='tool',tool='submit_rlm' WHERE ref='T/e2'")
        db.execute("UPDATE _content SET text=? WHERE ref='T/e2' AND field='text'", (receipt,))
        db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                   ('T/o_buf_handoff', 'buffer', len(body.splitlines()), len(body), 1, 'T', None))
        db.execute('INSERT INTO _content VALUES(?,?,?)', ('T/o_buf_handoff', 'text', body))
        db.execute('INSERT INTO links VALUES(?,?,?)', ('T/e1', 'T/o_buf_handoff', 'references'))
        db.row_factory = sqlite3.Row
        engine.ensure_session_links(db, rebuild=True)
    answer = study.call('trace_read', targets=['T/o_buf_handoff'], pair=False)
    evidence = blocks(answer, 'evidence')[0]
    assert evidence['text'] == body
    link = evidence['links'][0]
    assert child in link['text'] and 'Child investigation' in link['text']
    from azo_session_postmortem.cache_render import render_preview
    preview = render_preview(answer['document'], 'buf_test', 1200)
    assert child in preview and 'Child investigation' in preview and link['id'] in preview
    launched = study.call('trace_read', targets=[link['id']], line_context=0, pair=False)
    assert receipt.splitlines()[0] in blocks(launched, 'evidence')[0]['text']
    # The referencing event has the same compact navigation attached directly.
    indirect = study.call('trace_read', targets=['T/e1'], pair=False)
    assert child in blocks(indirect, 'evidence')[0]['links'][0]['text']
