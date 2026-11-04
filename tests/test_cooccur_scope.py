"""Search-only scope isolation for cooccur results; synthetic sources only."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from azo_session_postmortem import engine


class CooccurScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = str(Path(temp.name) / 'workspace')
        self.call('trace_open')
        with self.db() as db:
            for sid in ('S', 'T'):
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                           (sid, None, sid, 'synthetic', '[]'))
            for seq, token in enumerate(('EVENT_ONE', 'EVENT_TWO'), 1):
                ref = f'S/e{seq}'
                text = token + '\nSECOND_LINE_TOKEN'
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (ref, 'S', seq, None, 'message', 'assistant', None, None, text, None))
                for field, value in [('text', text), ('arguments', 'ARGUMENT_TOKEN'),
                                     ('metadata', 'METADATA_TOKEN')]:
                    db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, field, value))
            for ref, sid, token in [('S/o1', 'S', 'LOCAL_OBJECT_TOKEN'),
                                    ('T/o1', 'T', 'CROSS_OBJECT_TOKEN'),
                                    ('S/missing', 'S', None)]:
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                           (ref, 'buffer', int(token is not None), len(token or ''),
                            int(token is not None), sid, None if token else 'MISSING_OBJECT_TOKEN'))
                if token:
                    db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', token))
                for event in ('S/e1', 'S/e2'):
                    db.execute('INSERT INTO links VALUES(?,?,?)', (event, ref, 'references'))
        self.seeds = self.query('SELECT ref FROM events ORDER BY seq')
        self.first = self.query('SELECT ref FROM events WHERE seq=1')
        self.joined = self.join(self.seeds, self.first)
        with self.db() as db:
            unmatched = engine._get_result(db, self.joined['unmatched_result'])[1]
        self.results = {
            'matched': (self.joined['result'], 'S/e1'),
            'unmatched': (self.joined['unmatched_result'], 'S/e2'),
            'witness': (self.joined['rows'][0]['conditions'][0]['witnesses_result'], 'S/e1'),
            'nearest': (unmatched[0]['conditions'][0]['witnesses_result'], 'S/e1'),
        }

    @contextmanager
    def db(self):
        db = sqlite3.connect(Path(self.workspace) / 'trace.sqlite3')
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def call(self, tool, **args):
        return engine.dispatch(tool, {'workspace': self.workspace, **args})

    def query(self, sql, **args):
        return self.call('trace_query', sql=sql, **args)['result']

    def search(self, token, **args):
        return self.call('trace_search', query=token, mode='literal', **args)

    def join(self, seed, against):
        return self.call('trace_cooccur', seed={'result': seed}, against=[{
            'name': 'same', 'selection': {'result': against}, 'before': 0, 'after': 0}])

    def assertNoObjects(self, scope, **args):
        found = self.search('OBJECT_TOKEN', scope=scope, **args)
        self.assertEqual(found['hits']['source'], [])
        self.assertEqual(found['unavailable'], [])
        self.assertEqual(found['unavailable_count'], 0)

    def assertOrdinaryExpansion(self, scope, **args):
        found = self.search('OBJECT_TOKEN', scope=scope, **args)
        self.assertEqual({h['ref'] for h in found['hits']['source']}, {'S/o1', 'T/o1'})
        self.assertEqual(found['unavailable_count'], 1)
        self.assertEqual(found['unavailable'][0]['anchor'], 'S/missing')
        self.assertEqual(found['unavailable'][0]['reason'], 'MISSING_OBJECT_TOKEN')

    def test_all_joined_result_kinds_and_both_scope_forms(self):
        for kind, (result, ref) in self.results.items():
            for scope in ({'result': result}, {'refs': [{'result': result, 'rows': [0]}]}):
                for projection in ('content', 'all'):
                    with self.subTest(kind=kind, scope=scope, projection=projection):
                        self.assertNoObjects(scope, projection=projection)
                        found = self.search('EVENT_', scope=scope, projection=projection)
                        self.assertEqual([h['ref'] for h in found['hits']['source']], [ref])
                        self.assertNoObjects({'refs': [{'result': result, 'rows': []}]},
                                             projection=projection)

    def test_projection_and_explicit_field_line_anchors(self):
        for kind, (result, ref) in self.results.items():
            for projection in ('content', 'all'):
                with self.subTest(kind=kind, projection=projection):
                    scope = {'result': result}
                    for token in ('EVENT_', 'ARGUMENT_TOKEN'):
                        self.assertEqual(len(self.search(token, scope=scope,
                                                        projection=projection)['hits']['source']), 1)
                    meta = self.search('METADATA_TOKEN', scope=scope, projection=projection)
                    self.assertEqual(len(meta['hits']['source']), int(projection == 'all'))
        # Explicit metadata fields bypass content projection; exact slices stay exact.
        anchors = [{'event': 'S/e1', 'field': 'text', 'lines': [1, 1]},
                   {'event': 'S/e1', 'field': 'metadata', 'lines': [1, 1]}]
        exact = self.query('SELECT :anchor AS anchor', params={'anchor': json.dumps(anchors[0])})
        metadata = self.query('SELECT :anchor AS anchor', params={'anchor': json.dumps(anchors[1])})
        for selection, token in ((exact, 'EVENT_ONE'), (metadata, 'METADATA_TOKEN')):
            joined = self.join(selection, self.first)['result']
            scope = {'result': joined}
            self.assertEqual(len(self.search(token, scope=scope)['hits']['source']), 1)
            self.assertEqual(self.search('SECOND_LINE_TOKEN', scope=scope)['hits']['source'], [])
            self.assertNoObjects(scope)
            with self.db() as db:
                self.assertEqual(engine._get_result(db, joined)[1][0]['_anchors'],
                                 engine._resolve(db, [{'result': selection}]))

    def test_legacy_queries_and_mixed_explicit_references_keep_expansion(self):
        for projection in ('content', 'all'):
            self.assertOrdinaryExpansion({'result': self.first}, projection=projection)
            self.assertOrdinaryExpansion({'refs': [{'result': self.first, 'rows': [0]}]},
                                         projection=projection)
            for result, ref in self.results.values():
                # Even the same event supplied explicitly retains ordinary behavior.
                for explicit in (ref, {'result': self.first}):
                    for refs in ([{'result': result}, explicit], [explicit, {'result': result}]):
                        self.assertOrdinaryExpansion({'refs': refs}, projection=projection)
                # Explicitly selected objects are still searchable beside joined anchors.
                found = self.search('CROSS_OBJECT_TOKEN', projection=projection,
                                    scope={'refs': [{'result': result}, {'object': 'T/o1'}]})
                self.assertEqual([h['ref'] for h in found['hits']['source']], ['T/o1'])

    def test_nested_result_resolution_and_row_selection(self):
        # An ordinary result can retain selectors, not just rematerialized anchors.
        with self.db() as db:
            nested = engine._store_result(db, 'query', [
                {'anchor': {'result': self.joined['result'], 'rows': [0]}},
                {'ref': 'S/e2'}], {'columns': ['anchor', 'ref'], 'sampling': None})
        self.assertNoObjects({'refs': [{'result': nested, 'rows': [0]}]})
        self.assertOrdinaryExpansion({'refs': [{'result': nested, 'rows': [1]}]})
        self.assertOrdinaryExpansion({'result': nested})

    def test_saved_search_preserves_scope_and_projection(self):
        for kind, (result, _) in self.results.items():
            for scope in ({'result': result}, {'refs': [{'result': result, 'rows': [0]}]}):
                for projection in ('content', 'all'):
                    with self.subTest(kind=kind, scope=scope, projection=projection):
                        original = self.search('METADATA_TOKEN', scope=scope,
                                               projection=projection, save_as='joined-search')
                        replay = self.call('trace_search', saved='joined-search')
                        self.assertEqual(replay['hits'], original['hits'])
                        self.assertEqual(len(replay['hits']['source']), int(projection == 'all'))
                        self.assertNoObjects(scope, projection=projection, save_as='objects-search')
                        replay = self.call('trace_search', saved='objects-search')
                        self.assertEqual(replay['hits']['source'], [])
                        self.assertEqual(replay['unavailable_count'], 0)
        self.assertOrdinaryExpansion({'result': self.first}, save_as='ordinary-search')
        replay = self.call('trace_search', saved='ordinary-search')
        self.assertEqual({h['ref'] for h in replay['hits']['source']}, {'S/o1', 'T/o1'})
        self.assertEqual(replay['unavailable_count'], 1)

    def test_stored_anchors_read_and_map_rematerialization_unchanged(self):
        with self.db() as db:
            before = {result: (tuple(db.execute('SELECT rows,meta FROM _results WHERE id=?',
                              (int(result[1:]),)).fetchone()),
                              [tuple(row) for row in db.execute(
                                  'SELECT * FROM result_items WHERE result_id=?', (result,))])
                      for result, _ in self.results.values()}
        for kind, (result, ref) in self.results.items():
            self.assertNoObjects({'result': result})
            with self.db() as db:
                _, rows, meta = engine._get_result(db, result)
                self.assertTrue(meta['_search_no_implicit_objects'])
                self.assertEqual(rows[0]['anchor'], ref)
                if kind in ('matched', 'unmatched'):
                    self.assertEqual(rows[0]['_anchors'], [ref])
                self.assertEqual(engine._resolve(db, [{'result': result}]), [ref])
                self.assertEqual(engine._read_plan(db, [{'result': result}], 0, 0, False, 0),
                                 engine._read_plan(db, [ref], 0, 0, False, 0))
            self.call('trace_edit', put=[{'id': kind, 'kind': 'episode', 'title': kind,
                                        'anchors': [{'result': result}]}])
            self.assertOrdinaryExpansion({'entries': [kind]})
            requery = self.query('SELECT anchor FROM result_items WHERE result_id=:r',
                                 params={'r': result})
            self.assertOrdinaryExpansion({'result': requery})
        with self.db() as db:
            for result, _ in self.results.values():
                after = (tuple(db.execute('SELECT rows,meta FROM _results WHERE id=?',
                               (int(result[1:]),)).fetchone()),
                         [tuple(row) for row in db.execute(
                             'SELECT * FROM result_items WHERE result_id=?', (result,))])
                self.assertEqual(after, before[result])

    def test_combined_search_does_not_change_map_intersections(self):
        self.call('trace_edit', put=[{'id': 'metadata-map', 'kind': 'episode',
            'title': 'MAP_TOKEN', 'anchors': [{'event': 'S/e1', 'field': 'metadata'}]}])
        for on in ('map', 'both'):
            for projection in ('content', 'all'):
                found = self.search('MAP_TOKEN', scope={'result': self.joined['result']},
                                    on=on, projection=projection)
                self.assertEqual([h['entry_id'] for h in found['hits']['map']], ['metadata-map'])
        self.assertNoObjects({'result': self.joined['result']}, on='both')


if __name__ == '__main__':
    unittest.main()
