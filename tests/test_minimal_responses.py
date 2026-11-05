"""Default responses are reading surfaces, not repeated workflow instruction."""
import unittest

from azo_session_postmortem.render import render


class MinimalResponseTests(unittest.TestCase):
    def test_search_omits_routine_missing_coverage_and_echoed_arguments(self):
        result = dict(result='r12', hits={'source': [], 'map': []}, row_count=0,
                      matched_total={'source': 0, 'map': 0}, next_cursor=None,
                      unavailable_count=1244,
                      unavailable=[{'anchor': 'S/missing', 'reason': 'not captured'}],
                      scope_summary={'event_count': 15688, 'field_count': 25497,
                                     'object_count': 2758, 'include_references': False})
        text = render('trace_search', result)
        self.assertIn('r12', text)
        self.assertNotIn('1244', text.replace(',', ''))
        self.assertNotIn('S/missing', text)
        for phrase in ('warning', 'incomplete', 'scope_summary', 'include_references',
                       'no evidence', 'cannot establish', 'follow_object_links'):
            self.assertNotIn(phrase, text.lower())
        result['unavailable_count'] = None
        result['scope_summary'] = None
        self.assertEqual(render('trace_search', result), text)

    def test_open_omits_capability_and_recipe_argument_dumps(self):
        result = dict(workspace='/tmp/study', counts={'sessions': 1, 'events': 4},
                      sources=[{'id': 'S', 'title': 'Test', 'warnings': []}],
                      pinned_entries=[], recent_entries=[], next_cursor=None,
                      capabilities={'similar': False, 'query_schema': '/docs/api.md',
                                    'read_budget_unit': 'text_chars'},
                      saved_queries=[{'name': 'bugs', 'tool': 'trace_search',
                                      'arguments': {'query': 'secret-query-body'},
                                      'last_result': 'r4'}])
        text = render('trace_open', result)
        self.assertIn('bugs', text)
        for phrase in ('secret-query-body', 'read_budget_unit', 'similar', 'capabilities',
                       'no model calls', 'cannot establish'):
            self.assertNotIn(phrase, text)

    def test_specific_unavailable_read_is_identified_without_lecture(self):
        result = dict(blocks=[{'anchor': {'object': 'S/missing'}, 'available': False,
                              'reason': 'not captured'}], sources={}, remaining_count=0,
                      next_cursor=None, unavailable_count=1,
                      unavailable=[{'anchor': 'S/missing', 'reason': 'not captured'}])
        text = render('trace_read', result)
        self.assertIn('S/missing', text)
        self.assertIn('unavailable', text.lower())
        self.assertNotIn('warning', text.lower())
        self.assertLessEqual(text.count('S/missing'), 1)

    def test_edit_does_not_echo_authored_content(self):
        text = render('trace_edit', dict(created=['episode-a'], updated=[], deleted=[],
                      linked=[], unlinked=[], entry_previews=[{'id': 'episode-a',
                      'title': 'The entire report', 'note': 'unnecessary repeated note'}]))
        self.assertIn('episode-a', text)
        self.assertNotIn('unnecessary repeated note', text)
        self.assertLess(len(text), 100)


if __name__ == '__main__':
    unittest.main()
