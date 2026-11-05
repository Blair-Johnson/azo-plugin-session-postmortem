"""Structured API compatibility supporting concise native text navigation."""
import unittest

from test_scope_controls import ScopeFixture


class TextAPICompatibilityTests(ScopeFixture):
    def test_new_spelling_preserves_old_behavior_and_does_not_mutate_arguments(self):
        from azo_session_postmortem import engine
        for policy in (True, False):
            args = dict(workspace=self.workspace, scope={'refs': ['S/e1']},
                        query='TOKEN', mode='literal', follow_object_links=policy)
            original = dict(args)
            new = engine.dispatch('trace_search', args)
            old = self.search(scope=args['scope'], include_references=policy)
            self.assertEqual(args, original)
            self.assertEqual(new['hits'], old['hits'])
            self.assertEqual(new['scope_summary'], old['scope_summary'])
        with self.assertRaisesRegex(ValueError, 'not both'):
            self.search(follow_object_links=True, include_references=True)
        with self.assertRaisesRegex(ValueError, 'boolean'):
            self.search(follow_object_links='false')

    def test_saved_legacy_policy_and_new_saved_policy_are_preserved(self):
        scope = {'refs': ['S/e1']}
        for name, kwargs in [('old', {'include_references': False}),
                             ('new', {'follow_object_links': False})]:
            first = self.search(scope=scope, save_as=name, page_size=1, **kwargs)
            saved = self.call('trace_search', saved=name)
            self.assertEqual({h['ref'] for h in saved['hits']['source']}, {'S/e1'})
            with self.assertRaisesRegex(ValueError, 'saved cannot'):
                self.call('trace_search', saved=name, follow_object_links=True)
            with self.assertRaisesRegex(ValueError, 'cursor cannot'):
                self.call('trace_search', cursor=first['next_cursor'], follow_object_links=False)

    def test_query_position_is_not_a_user_selected_row_column(self):
        first = self.call('trace_query', sql='SELECT 99 AS row,ref FROM events ORDER BY seq',
                          page_size=1)
        second = self.call('trace_query', cursor=first['next_cursor'], page_size=1)
        self.assertEqual(first['page_offset'], 0)
        self.assertEqual(second['page_offset'], 1)
        self.assertEqual(second['rows'][0]['row'], 99)
        read = self.call('trace_read', targets=[{'result': first['result'], 'rows': [1]}],
                         pair=False, line_context=0)
        self.assertEqual(read['blocks'][0]['anchor']['event'], second['rows'][0]['ref'])

    def test_collapsed_counts_and_search_offsets_are_distinct(self):
        first = self.search(scope={'sessions': ['S']}, follow_object_links=False,
                            collapse='exact', page_size=1)
        self.assertGreater(first['matched_total']['source'], first['row_count'])
        self.assertEqual(first['page_offset'], 0)
        second = self.call('trace_search', cursor=first['next_cursor'], page_size=1)
        self.assertEqual(second['page_offset'], 1)
        self.assertEqual(second['hits']['source'][0]['row'], 1)
        self.assertEqual(second['row_count'], first['row_count'])


if __name__ == '__main__':
    unittest.main()
