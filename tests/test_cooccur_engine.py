"""Synthetic backend contract tests: no session history or private fixtures."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from azo_session_postmortem import cooccur, engine


class CooccurEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = str(Path(self.temp.name) / 'workspace')
        self.call('trace_open')
        with self.db() as db:
            for sid in ('S', 'T', 'U'):
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sid, None, sid, 'synthetic', '[]'))
            bodies = {'S': ['witness\nwitness', 'seed\nseed\nprivate-only', 'witness\nwitness',
                            'neutral', 'seed', 'witness'], 'T': ['seed', 'witness'], 'U': ['seed']}
            for sid, texts in bodies.items():
                for seq, text in enumerate(texts, 1):
                    ref = f'{sid}/e{seq}'
                    db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (ref, sid, seq, None, 'message', 'assistant', None, None, text, 1 if seq == 3 else None))
                    db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', text))
            db.execute('INSERT INTO _content VALUES(?,?,?)', ('S/e2', 'arguments', 'auxiliary token'))
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', ('S/o1', 'buffer', 1, 7, 1, 'S', None))
            db.execute('INSERT INTO _content VALUES(?,?,?)', ('S/o1', 'text', 'witness'))
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', ('S/missing', 'buffer', 0, 0, 0, 'S', 'not retained'))
        self.seed = self.search('seed')['result']
        self.witness = self.search('witness')['result']

    def db(self):
        db = sqlite3.connect(str(Path(self.workspace) / 'trace.sqlite3'))
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def call(self, tool, **args):
        return engine.dispatch(tool, {'workspace': self.workspace, **args})

    def search(self, text, **args):
        return self.call('trace_search', query=text, mode='literal', **args)

    def condition(self, result=None, name='w', before=1, after=0):
        return {'name': name, 'selection': {'result': result or self.witness}, 'before': before, 'after': after}

    def join(self, **args):
        return self.call('trace_cooccur', **({'seed': {'result': self.seed},
                         'against': [self.condition()]} | args))

    def stored(self, result):
        return engine._get_result(self.db(), result)

    def snapshot(self):
        db = self.db()
        return {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)]
                for table in ('_results', '_cursors', 'result_items', 'saved_queries')}

    def selection(self, refs):
        rows = [{'ref': ref} for ref in refs]
        with self.db() as db:
            return engine._store_result(db, 'query', rows, {'columns': ['ref'], 'sampling': None})

    def test_asymmetric_same_session_all_anchors_and_exclusions(self):
        out = self.join()
        self.assertEqual(out['operation'], 'cooccur')
        self.assertEqual(out['counts'], {'seed_events': 4, 'matched_seed_events': 1, 'unmatched_seed_events': 3})
        self.assertEqual(out['row_count'], 1)
        row = out['rows'][0]
        self.assertEqual(row['ref'], 'S/e2')
        self.assertEqual(row['seed_anchor_count'], 2)
        self.assertEqual(row['anchor'], {'event': 'S/e2', 'field': 'text', 'lines': [1, 1]})
        self.assertEqual(row['conditions'][0]['witness_count'], 2)
        self.assertEqual([w['delta_seq'] for w in row['conditions'][0]['witnesses']], [-1, -1])
        self.assertEqual(out['inputs'][1]['excluded_non_event'], 1)
        self.assertEqual(out['inputs'][1]['anchor_count'], 6)
        kind, rows, meta = self.stored(out['result'])
        self.assertEqual(kind, 'query')
        self.assertIsNone(meta['sampling'])
        self.assertEqual(len(rows[0]['_anchors']), 2)
        page = self.call('trace_query', cursor=out['unmatched_cursor'])
        unmatched = {r['ref']: r for r in page['rows']}
        self.assertEqual(unmatched['S/e5']['conditions'][0]['witnesses'][0]['delta_seq'], 1)
        self.assertEqual(unmatched['T/e1']['conditions'][0]['status'], 'outside_window')
        self.assertEqual(unmatched['U/e1']['conditions'][0]['status'], 'no_same_session_match')
        self.assertIsNone(unmatched['U/e1']['conditions'][0]['witnesses_result'])

    def test_full_witness_query_paging_order_and_no_cartesian_product(self):
        out = self.join(seed={'result': self.seed, 'rows': [0]},
                        against=[self.condition(before=8, after=8), self.condition(name='other', before=8, after=8)])
        self.assertEqual(out['row_count'], 1)
        first, second = out['rows'][0]['conditions']
        self.assertEqual(first['witness_count'], 5)
        self.assertEqual(second['witness_count'], 5)
        self.assertEqual(len(first['witnesses']), 2)
        _, rows, meta = self.stored(first['witnesses_result'])
        self.assertEqual([r['ref'] for r in rows], ['S/e1', 'S/e1', 'S/e3', 'S/e3', 'S/e6'])
        self.assertEqual([r['row'] for r in rows], list(range(5)))
        self.assertIsNone(rows[0]['system_generated'])
        self.assertIs(rows[2]['system_generated'], True)
        with self.db() as db:
            cursor = engine._cursor(db, 'query', {'result': first['witnesses_result'], 'offset': 0})
        paged = []
        while cursor:
            page = self.call('trace_query', cursor=cursor, page_size=1)
            paged.extend(page['rows'])
            cursor = page['next_cursor']
        self.assertEqual(paged, rows)
        self.assertEqual(meta['columns'], cooccur.WITNESS_COLUMNS)

    def test_zero_window_whole_event_scope_and_seed_only_composition(self):
        same = self.selection(['S/e2'])
        out = self.join(seed={'result': self.seed, 'rows': [0, 1]}, against=[self.condition(same, before=0, after=0)])
        row = out['rows'][0]
        witness = row['conditions'][0]['witnesses'][0]
        self.assertEqual(witness['anchor'], 'S/e2')
        self.assertEqual(witness['delta_seq'], 0)
        self.assertEqual(self.search('private-only', scope={'result': out['result']})['hits']['source'], [])
        found = self.search('private-only', scope={'result': row['conditions'][0]['witnesses_result']})
        self.assertEqual(len(found['hits']['source']), 1)
        read = self.call('trace_read', targets=[{'result': out['result']}], line_context=0, pair=False)
        self.assertNotIn('private-only', json.dumps(read))
        self.call('trace_edit', put=[{'id': 'seed-map', 'kind': 'episode', 'title': 'Exact seeds',
                                      'anchors': [{'result': out['result']}]}])
        self.assertEqual(self.search('private-only', scope={'entries': ['seed-map']})['hits']['source'], [])
        sql = self.call('trace_query', sql='SELECT ref,anchor FROM result_items WHERE result_id=:id',
                        params={'id': out['result']})
        self.assertEqual(len(sql['rows']), 2)
        again = self.join(seed={'result': sql['result']}, against=[self.condition(same, before=0, after=0)])
        self.assertEqual(again['rows'][0]['seed_anchor_count'], 2)

    def test_collapsed_expansion_original_row_indexes_and_coverage(self):
        collapsed = self.search('seed', collapse='exact', page_size=1)
        self.assertEqual(len(collapsed['hits']['source']), 1)
        out = self.join(seed={'result': collapsed['result'], 'rows': [0, 0]},
                        against=[self.condition(before=0, after=2)])
        self.assertEqual(out['inputs'][0]['selected_rows'], 2)
        self.assertEqual(out['inputs'][0]['event_count'], 4)
        self.assertEqual(out['inputs'][0]['anchor_count'], 5)
        self.assertEqual(out['inputs'][0]['duplicate_anchors'], 5)
        self.assertEqual(out['inputs'][0]['coverage']['scope'], 'input_result')
        self.assertEqual(out['inputs'][0]['coverage']['unavailable_count'], 1)
        occurrence = collapsed['hits']['source'][0]['occurrences_result']
        selected = self.join(seed={'result': occurrence, 'rows': [3]}, against=[self.condition(before=0, after=1)])
        self.assertEqual(selected['rows'][0]['ref'], 'T/e1')
        # Legacy collapsed rows can preserve membership through occurrences_result.
        with self.db() as db:
            kind, rows, meta = engine._get_result(db, collapsed['result'])
            rows[0].pop('_anchors')
            fallback = engine._store_result(db, kind, rows, meta)
        expanded = self.join(seed={'result': fallback})
        self.assertEqual(expanded['inputs'][0]['anchor_count'], 5)

    def test_nearest_ties_and_exact_different_fields(self):
        anchors = [{'event': 'S/e3', 'field': 'text', 'lines': [2, 2]},
                   {'event': 'S/e1', 'field': 'text', 'lines': [2, 2]},
                   {'event': 'S/e1', 'field': 'text', 'lines': [1, 1]}]
        with self.db() as db:
            result = engine._store_result(db, 'query', [{'anchor': a} for a in anchors],
                                          {'columns': ['anchor'], 'sampling': None})
        out = self.join(seed={'result': self.seed, 'rows': [0]}, against=[self.condition(result, before=0, after=0)])
        nearest = self.stored(out['unmatched_result'])[1][0]['conditions'][0]['witnesses'][0]
        self.assertEqual(nearest['anchor'], anchors[2])
        self.assertEqual(nearest['delta_seq'], -1)
        field = {'event': 'S/e2', 'field': 'arguments', 'lines': [1, 1]}
        with self.db() as db:
            result = engine._store_result(db, 'query', [{'anchor': field}], {'columns': ['anchor'], 'sampling': None})
        out = self.join(seed={'result': self.seed, 'rows': [0]}, against=[self.condition(result, before=0, after=0)])
        self.assertEqual(out['rows'][0]['conditions'][0]['witnesses'][0]['anchor'], field)

    def test_empty_inputs_query_unknown_coverage_and_map_aggregate_exclusions(self):
        empty = self.join(seed={'result': self.seed, 'rows': []})
        self.assertEqual(empty['counts']['seed_events'], 0)
        self.assertEqual(empty['rows'], [])
        self.assertIsNone(empty['unmatched_cursor'])
        out = self.join(against=[{**self.condition(), 'selection': {'result': self.witness, 'rows': []}}])
        self.assertEqual(out['counts']['unmatched_seed_events'], 4)
        self.assertTrue(all(r['conditions'][0]['status'] == 'no_same_session_match'
                            for r in self.stored(out['unmatched_result'])[1]))
        aggregate = self.call('trace_query', sql='SELECT count(ref) AS n FROM events')['result']
        out = self.join(seed={'result': aggregate})
        self.assertEqual(out['inputs'][0]['excluded_non_event'], 1)
        self.assertIsNone(out['inputs'][0]['coverage']['unavailable_count'])
        self.call('trace_edit', put=[{'id': 'map', 'kind': 'episode', 'title': 'seed', 'anchors': ['S/e2']}])
        maps = self.search('seed', on='map')['result']
        out = self.join(seed={'result': maps})
        self.assertEqual(out['inputs'][0]['event_count'], 0)
        self.assertEqual(out['inputs'][0]['excluded_non_event'], 1)
        self.assertIsNone(out['inputs'][0]['coverage']['unavailable_count'])
        scoped = self.search('seed', scope={'refs': ['S/e2']})['result']
        self.assertEqual(self.join(seed={'result': scoped})['inputs'][0]['coverage']['unavailable_count'], 0)

    def test_saved_cursor_immutability_and_display_budget(self):
        out = self.join(against=[self.condition(before=8, after=8)], save_as='near', page_size=1, max_chars=1)
        self.assertTrue(out['rows'][0]['_display_truncated'])
        self.assertTrue(out['next_cursor'])
        before = self.stored(out['result'])
        self.search('neutral')
        self.call('trace_edit', put=[{'id': 'note', 'kind': 'note', 'title': 'later'}])
        self.assertEqual(self.stored(out['result']), before)
        following = self.call('trace_cooccur', cursor=out['next_cursor'], page_size=20, max_chars=20000)
        self.assertEqual(following['counts'], out['counts'])
        self.assertEqual([r['row'] for r in following['rows']], [1, 2])
        rerun = self.call('trace_cooccur', saved='near')
        self.assertEqual(rerun['counts'], out['counts'])
        self.assertEqual(rerun['inputs'], out['inputs'])
        recipe = self.db().execute('SELECT tool,last_result FROM saved_queries WHERE name=?', ('near',)).fetchone()
        self.assertEqual(tuple(recipe), ('cooccur', rerun['result']))
        for extra in ({'seed': None}, {'against': []}, {'save_as': 'other'}, {'saved': 'near'}):
            snapshot = self.snapshot()
            with self.assertRaisesRegex(ValueError, 'cursor cannot'):
                self.call('trace_cooccur', cursor=out['next_cursor'], **extra)
            self.assertEqual(self.snapshot(), snapshot)

    def test_invalid_arguments_fail_without_writes(self):
        good = self.condition()
        invalid = [
            {'seed': None}, {'seed': {'result': self.seed, 'rows': None}},
            {'seed': {'result': self.seed, 'rows': [True]}}, {'seed': {'result': self.seed, 'rows': [-1]}},
            {'seed': {'result': self.seed, 'rows': [999]}}, {'seed': {'result': 'r99999'}},
            {'seed': {'result': []}}, {'seed': {'result': self.seed, 'other': 0}},
            {'against': []}, {'against': None}, {'against': [good] * 4}, {'against': [good, good]},
            {'against': [{**good, 'name': 'seed'}]}, {'against': [{**good, 'name': ''}]},
            {'against': [{**good, 'before': True}]}, {'against': [{**good, 'after': -1}]},
            {'against': [{k: v for k, v in good.items() if k != 'after'}]},
            {'against': [{**good, 'extra': 0}]}, {'against': [{**good, 'selection': None}]},
            {'page_size': True}, {'max_chars': 0}, {'save_as': ''}, {'saved': False},
            {'cursor': ''}, {'unknown': 3}, {'saved': 'near'},
        ]
        for args in invalid:
            with self.subTest(args=args):
                snapshot = self.snapshot()
                with self.assertRaises(ValueError):
                    self.join(**args)
                self.assertEqual(self.snapshot(), snapshot)

    def test_limits_rollback_all_results_cursors_and_recipes(self):
        for limit, maximum in [('MAX_INPUT_ANCHORS', 2), ('MAX_SEED_EVENTS', 1),
                               ('MAX_WITNESS_ROWS', 5), ('MAX_RESULT_BYTES', 1)]:
            with self.subTest(limit=limit):
                snapshot = self.snapshot()
                with patch.object(cooccur, limit, maximum), self.assertRaisesRegex(ValueError, limit + '.*narrow selectors'):
                    self.join(against=[self.condition(before=8, after=8)], save_as='must-not-save')
                self.assertEqual(self.snapshot(), snapshot)

    def test_stale_and_malformed_anchors_error_before_writes(self):
        bad_anchors = [{'event': 'S/e2', 'field': 'missing'},
                       {'event': 'S/e2', 'field': 'text', 'lines': [1, 99]},
                       {'event': 'S/e2', 'field': None}, {'event': 'S/e2', 'lines': [True, 1]},
                       {'event': 'no-such-event'}, 'unknown-ref', {'event': 'S/e2', 'extra': 1},
                       {'session': 'S', 'events': [1, 99]}]
        for anchor in bad_anchors:
            with self.subTest(anchor=anchor):
                with self.db() as db:
                    result = engine._store_result(db, 'query', [{'anchor': anchor}], {'columns': ['anchor'], 'sampling': None})
                snapshot = self.snapshot()
                with self.assertRaises(ValueError):
                    self.join(seed={'result': result})
                self.assertEqual(self.snapshot(), snapshot)

    def test_empty_source_anchor_list_is_malformed_not_an_exclusion(self):
        with self.db() as db:
            result = engine._store_result(db, 'search', [{'origin': 'source', '_anchors': []}], {})
        snapshot = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'no source anchor'):
            self.join(seed={'result': result})
        self.assertEqual(self.snapshot(), snapshot)

    def test_ranges_and_source_field_validation_cached_across_inputs(self):
        with self.db() as db:
            result = engine._store_result(db, 'query', [{'anchor': {'session': 'S', 'events': [1, 3]}}],
                                          {'columns': ['anchor'], 'sampling': None})
        out = self.join(seed={'result': result})
        self.assertEqual(out['counts']['seed_events'], 3)
        self.assertEqual(self.stored(out['result'])[1][0]['anchor'], 'S/e1')
        with patch.object(engine, '_field_text', wraps=engine._field_text) as field:
            self.join(against=[self.condition(self.seed, before=0, after=0)])
        self.assertEqual(field.call_count, 4)


if __name__ == '__main__':
    unittest.main()
