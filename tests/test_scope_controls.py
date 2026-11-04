"""Reference policy and frozen scope accounting; synthetic workspaces only."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from azo_session_postmortem import engine


class ScopeFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = str(Path(temp.name) / 'workspace')
        self.call('trace_open')
        with self.db() as db:
            for sid in ('S', 'T'):
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                           (sid, None, sid, 'synthetic', '[]'))
            for seq in (1, 2, 10):
                ref = f'S/e{seq}'
                db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (ref, 'S', seq, None, 'message', 'assistant', None, None, '', None))
                for field, text in [('text', 'TOKEN\nsecond TOKEN'),
                                    ('arguments', 'ARGUMENT'), ('metadata', 'META')]:
                    db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, field, text))
            for ref, sid, text in [('S/later', 'S', 'LATER_SNAPSHOT TOKEN'),
                                   ('T/foreign', 'T', 'FOREIGN TOKEN'),
                                   ('S/missing', 'S', None)]:
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                           (ref, 'buffer', int(text is not None), len(text or ''),
                            int(text is not None), sid, 'not captured' if text is None else None))
                if text is not None:
                    db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', text))
                db.execute('INSERT INTO links VALUES(?,?,?)', ('S/e1', ref, 'references'))
        self.first = self.query("SELECT ref FROM events WHERE seq=1")

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

    def search(self, query='TOKEN', **args):
        return self.call('trace_search', query=query, mode='literal', **args)


class ScopeControlTests(ScopeFixture):
    def test_ordinary_result_default_expands_later_snapshot_false_does_not(self):
        for scope in ({'result': self.first}, {'refs': [{'result': self.first, 'rows': [0]}]}):
            default = self.search(scope=scope)
            explicit = self.search(scope=scope, include_references=True)
            narrow = self.search(scope=scope, include_references=False)
            self.assertEqual(default['hits'], explicit['hits'])
            self.assertEqual({h['ref'] for h in default['hits']['source']},
                             {'S/e1', 'S/later', 'T/foreign'})
            self.assertEqual({h['ref'] for h in narrow['hits']['source']}, {'S/e1'})
            summary = default['scope_summary']
            self.assertTrue(summary['include_references'])
            self.assertEqual((summary['event_count'], summary['object_count'], summary['field_count']),
                             (1, 3, 4))
            self.assertEqual(summary['unavailable_object_count'], 1)
            self.assertEqual(default['unavailable_count'], 1)
            self.assertEqual(narrow['unavailable_count'], 0)
            self.assertEqual(narrow['scope_summary']['object_count'], 0)
            self.assertFalse(narrow['scope_summary']['include_references'])

    def test_explicit_objects_and_broad_scopes_still_include_objects(self):
        for object_anchor in ('S/later', {'object': 'S/later'}):
            page = self.search(scope={'refs': ['S/e1', object_anchor, {'object': 'S/missing'}]},
                               include_references=False)
            self.assertEqual({h['ref'] for h in page['hits']['source']}, {'S/e1', 'S/later'})
            self.assertEqual(page['scope_summary']['object_count'], 2)
            self.assertEqual(page['scope_summary']['unavailable_object_count'], 1)
        for scope in ({'workspace': True}, {'sessions': ['S']}):
            yes = self.search(scope=scope)
            no = self.search(scope=scope, include_references=False)
            self.assertEqual(yes['hits'], no['hits'])
            self.assertEqual(no['scope_summary']['object_count'], 3 if 'workspace' in scope else 2)
            self.assertEqual(no['unavailable_count'], 1)
            if 'sessions' in scope:
                self.assertNotIn('T/foreign', {h['ref'] for h in no['hits']['source']})

    def test_projection_and_sparse_envelopes_count_selection_not_hits(self):
        scope = {'refs': ['S/e1', 'S/e10', 'S/e1']}
        page = self.search('NO_MATCH', scope=scope, include_references=False)
        summary = page['scope_summary']
        self.assertEqual(page['matched_total']['source'], 0)
        self.assertEqual((summary['event_count'], summary['field_count']), (2, 4))
        self.assertEqual(summary['projection'], 'content')
        self.assertEqual(summary['sequence_coverage'], 'envelopes_not_contiguous')
        self.assertEqual(summary['sessions'][0]['seq_min'], 1)
        self.assertEqual(summary['sessions'][0]['seq_max'], 10)
        self.assertEqual(summary['sessions'][0]['event_count'], 2)
        all_fields = self.search(scope=scope, projection='all', include_references=False)
        self.assertEqual(all_fields['scope_summary']['field_count'], 6)
        explicit = self.search('META', scope={'refs': [{'event': 'S/e1', 'field': 'metadata'}]})
        self.assertEqual(explicit['scope_summary']['field_count'], 1)
        self.assertEqual(explicit['scope_summary']['object_count'], 0)

    def test_saved_policy_legacy_default_and_validation(self):
        original = self.search(scope={'result': self.first}, include_references=False, save_as='narrow')
        replay = self.call('trace_search', saved='narrow')
        self.assertEqual(replay['scope_summary'], original['scope_summary'])
        with self.db() as db:
            recipe = json.loads(db.execute('SELECT arguments FROM saved_queries WHERE name=?',
                                           ('narrow',)).fetchone()[0])
            self.assertIs(recipe.pop('include_references'), False)
            db.execute('UPDATE saved_queries SET arguments=? WHERE name=?',
                       (json.dumps(recipe), 'narrow'))
        legacy = self.call('trace_search', saved='narrow')
        self.assertTrue(legacy['scope_summary']['include_references'])
        self.assertEqual(legacy['scope_summary']['object_count'], 3)
        for value in (0, 1, 'false', None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'boolean'):
                self.search(include_references=value)
        with self.assertRaisesRegex(ValueError, 'saved cannot'):
            self.call('trace_search', saved='narrow', include_references=False)
        with self.db() as db:
            recipe['include_references'] = 'false'
            db.execute('UPDATE saved_queries SET arguments=? WHERE name=?', (json.dumps(recipe), 'narrow'))
        with self.assertRaisesRegex(ValueError, 'boolean'):
            self.call('trace_search', saved='narrow')

    def test_cursor_summary_frozen_and_old_results_unknown(self):
        page = self.search(scope={'result': self.first}, page_size=1, include_references=False)
        original = page['scope_summary']
        cursor = page['next_cursor']
        with self.assertRaisesRegex(ValueError, 'cursor cannot'):
            self.call('trace_search', cursor=cursor, include_references=False)
        with self.db() as db:
            db.execute('DELETE FROM _content')
            db.execute('DELETE FROM events')
            db.execute('DELETE FROM objects')
        resumed = self.call('trace_search', cursor=cursor)
        self.assertEqual(resumed['scope_summary'], original)
        with self.db() as db:
            kind, rows, meta = engine._get_result(db, page['result'])
            meta.pop('scope_summary')
            db.execute('UPDATE _results SET meta=? WHERE id=?', (json.dumps(meta), int(page['result'][1:])))
        self.assertIsNone(self.call('trace_search', cursor=cursor)['scope_summary'])

    def test_collapsed_occurrences_preserve_scope_metadata(self):
        page = self.search(scope={'refs': ['S/e1', 'S/e2']}, collapse='exact', include_references=False)
        occurrence = page['hits']['source'][0]['occurrences_result']
        with self.db() as db:
            result = engine._search_page(db, occurrence, 0, 1, 6000)
        self.assertEqual(result['scope_summary'], page['scope_summary'])
        resumed = self.call('trace_search', cursor=result['next_cursor'])
        self.assertEqual(resumed['scope_summary'], page['scope_summary'])

    def test_map_only_has_no_source_counts_and_both_keeps_map_membership(self):
        self.call('trace_edit', put=[{'id': 'note', 'kind': 'episode', 'title': 'TOKEN',
            'anchors': [{'event': 'S/e1', 'field': 'metadata'}]}])
        for include in (True, False):
            page = self.search(scope={'result': self.first}, on='map', include_references=include)
            self.assertEqual([h['entry_id'] for h in page['hits']['map']], ['note'])
            summary = page['scope_summary']
            for key in ('event_count', 'object_count', 'field_count', 'session_count',
                        'unavailable_object_count'):
                self.assertIsNone(summary[key])
            self.assertEqual(summary['sessions'], [])
            both = self.search(scope={'result': self.first}, on='both', include_references=include)
            self.assertEqual([h['entry_id'] for h in both['hits']['map']], ['note'])
            self.assertEqual(both['scope_summary']['object_count'], 3 if include else 0)

    def test_cooccur_never_implicitly_expands_but_explicit_objects_survive(self):
        joined = self.call('trace_cooccur', seed={'result': self.first}, against=[{
            'name': 'same', 'selection': {'result': self.first}, 'before': 0, 'after': 0}])['result']
        for include in (True, False):
            page = self.search(scope={'result': joined}, include_references=include)
            self.assertEqual(page['scope_summary']['object_count'], 0)
            self.assertEqual(page['scope_summary']['include_references'], include)
            explicit = self.search(scope={'refs': [{'result': joined}, {'object': 'S/later'}]},
                                   include_references=include)
            self.assertEqual(explicit['scope_summary']['object_count'], 1)
            self.assertIn('S/later', {h['ref'] for h in explicit['hits']['source']})

    def test_session_preview_is_bounded_with_complete_totals(self):
        with self.db() as db:
            for n in range(20):
                sid = f'extra{n:02}'
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sid, None, sid, 'synthetic', '[]'))
                ref = sid + '/o'
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', (ref, 'buffer', 1, 5, 1, sid, None))
                db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', 'TOKEN'))
        summary = self.search()['scope_summary']
        self.assertEqual(summary['session_count'], 22)
        self.assertEqual(len(summary['sessions']), engine.READ_PREVIEW_LIMIT)
        self.assertTrue(summary['sessions_truncated'])
        self.assertEqual(summary['object_count'], 23)


if __name__ == '__main__':
    unittest.main()
