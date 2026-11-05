"""Final cache-level workflows through the real backend and renderer."""
import json
from pathlib import Path
import tempfile
import unittest

from azo_session_postmortem import cache, engine
from azo_session_postmortem.cache_render import render_preview


class CacheHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / 'study'
        self.long_body = 'LONG_SOURCE ' + 'x' * 10000
        bodies = [self.long_body] + [f'event {i}' for i in range(2, 141)]
        bodies[49] = 'LATER_SPECIFIC_PASSAGE'
        source = root / 'source.json'
        source.write_text(json.dumps({'format_version': 2, 'meta': {'session_id': 'S'},
            'entries': [{'index': i, 'messages': [{'role': 'user', 'content': body}]}
                        for i, body in enumerate(bodies)],
            'trace_objects': [{'ref': 'S/o-missing', 'session': 'S', 'kind': 'buffer',
                               'available': False, 'reason': 'snapshot absent'}]}))
        self.call('trace_open', sessions=[str(source)], descendants=False)

    def call(self, tool, **args):
        return cache.dispatch(tool, {'workspace': str(self.workspace), **args})

    def test_sql_membership_pointer_resolves_same_hit(self):
        result = self.call('trace_search', query='LATER_SPECIFIC_PASSAGE', mode='literal')
        sql_result = result['document']['sql_result']
        self.assertIn('SQL membership: ' + sql_result, result['text'])
        membership = engine.dispatch('trace_query', {'workspace': str(self.workspace),
            'sql': 'SELECT ref FROM result_items WHERE result_id=:id', 'params': {'id': sql_result}})
        self.assertEqual([row['ref'] for row in membership['rows']], ['S/e50'])

    def test_relation_excerpts_remain_short_and_alias_reads_full_source(self):
        seed = self.call('trace_query', sql='SELECT ref FROM events WHERE seq=1')['document']['id']
        witness = self.call('trace_query', sql='SELECT ref FROM events WHERE seq=2')['document']['id']
        joined = self.call('trace_cooccur', seed={'result': seed}, against=[{
            'name': 'later', 'selection': {'result': witness}, 'before': 0, 'after': 1}])
        item = next(block for block in joined['document']['blocks'] if block['group'] == 'matched')
        self.assertLessEqual(len(item['text']), 241)
        read = self.call('trace_read', targets=[item['id']], pair=False, line_context=0)
        self.assertEqual(read['document']['blocks'][0]['text'], self.long_body)

    def test_specific_target_inside_range_gets_preview_space(self):
        result = self.call('trace_read', targets=[{'session': 'S', 'events': [1, 100]}, 'S/e50'],
                           pair=False, line_context=0, preview_chars=1200)
        preview = render_preview(result['document'], 'buf_fixture', 1200)
        self.assertIn('LONG_SOURCE', preview)
        self.assertIn('LATER_SPECIFIC_PASSAGE', preview)

    def test_missing_source_reports_specific_fact_without_repeated_advice(self):
        result = self.call('trace_search', query='absent',
                           scope={'refs': [{'object': 'S/o-missing'}]})
        self.assertIn('S/o-missing', result['text'])
        self.assertIn('snapshot absent', result['text'])
        self.assertNotIn('Attach an available source export', result['text'])


if __name__ == '__main__':
    unittest.main()
