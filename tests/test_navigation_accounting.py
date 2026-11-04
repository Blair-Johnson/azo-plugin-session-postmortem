"""Synthetic saved responses only: exposure is not unique evidence or use."""
import json
import unittest

from scripts.measure_trace_trials import account, metrics


def search(summary):
    return {'hits': {'source': [], 'map': []}, 'scope_summary': summary}


def read(**metadata):
    return dict(blocks=[{'anchor': {'event': 'S/e1'}, 'text': 'BODY'}],
                text_chars=4, packet_chars=40, **metadata)


def measure(tool, body):
    return metrics(tool, json.dumps(body))


def call(n, tool, arguments='{}', result=None):
    return dict(ref=f'S/c{n}', session='S', tool=tool, arguments=arguments,
                result_ref=result or f'S/r{n}')


class NavigationAccountingTests(unittest.TestCase):
    def test_effective_scope_policy_and_counts_are_not_hits(self):
        for policy in (False, True):
            m = measure('trace_search', search(dict(include_references=policy,
                        event_count=7, object_count=0, field_count=12)))
            self.assertEqual(m['search_scope_summary_known_pages'], 1)
            self.assertEqual(m[f'search_scope_include_references_{str(policy).lower()}_pages'], 1)
            for field, count in (('event', 7), ('object', 0), ('field', 12)):
                self.assertEqual(m[f'search_scope_{field}_count_page_total'], count)
                self.assertIn(f'search_scope_{field}_count_page_total', m)
                self.assertEqual(m[f'search_scope_{field}_count_known_pages'], 1)
            self.assertEqual(m['inline_source_chars'], 0)

    def test_legacy_null_and_invalid_scope_metadata_stays_unknown(self):
        for body in ({'hits': []}, search(None), search('bad'), search({}),
                     search(dict(include_references='false', event_count=None,
                                 object_count=-1, field_count=True))):
            with self.subTest(body=body):
                m = measure('trace_search', body)
                self.assertEqual(m['search_scope_include_references_unknown_pages'], 1)
                for field in ('event', 'object', 'field'):
                    self.assertNotIn(f'search_scope_{field}_count_page_total', m)
                    self.assertEqual(m[f'search_scope_{field}_count_unknown_pages'], 1)
        self.assertEqual(measure('trace_search', search(None))['search_scope_summary_unknown_pages'], 1)
        # Map-only summaries may report policy while all source counts are null.
        m = measure('trace_search', search(dict(include_references=False,
                    event_count=None, object_count=None, field_count=None)))
        self.assertEqual(m['search_scope_include_references_false_pages'], 1)
        self.assertNotIn('search_scope_event_count_page_total', m)

    def test_navigation_visible_and_reported_exposure_are_independent(self):
        rows = [dict(indexed=True, ambiguous=False, title='PRIVATE_TITLE'),
                dict(indexed=False, ambiguous=True), dict(indexed=True, ambiguous=True)]
        for body in (read(session_navigation=rows, session_navigation_count=12),
                     dict(buffer='buf_x', text_chars=4, packet_chars=40,
                          session_navigation=rows, session_navigation_count=12)):
            m = measure('trace_read', body)
            self.assertEqual(m['read_navigation_known_pages'], 1)
            self.assertEqual(m['read_navigation_pages_with_rows'], 1)
            self.assertEqual(m['read_navigation_visible_rows'], 3)
            self.assertEqual(m['read_navigation_reported_rows_page_total'], 12)
            self.assertEqual(m['read_navigation_reported_rows_known_pages'], 1)
            self.assertEqual(m['read_navigation_indexed_rows'], 2)
            self.assertEqual(m['read_navigation_missing_rows'], 1)
            self.assertEqual(m['read_navigation_ambiguous_rows'], 2)
            self.assertEqual(m['read_text_chars'], 4)
            self.assertEqual(m['read_packet_chars'], 40)
            self.assertEqual(m['inline_source_chars'], 0 if 'buffer' in body else 4)

    def test_navigation_absent_null_empty_and_malformed(self):
        for body in (read(), read(session_navigation=None),
                     read(session_navigation='bad', session_navigation_count=True)):
            m = measure('trace_read', body)
            self.assertEqual(m['read_navigation_unknown_pages'], 1)
            self.assertEqual(m['read_navigation_reported_rows_unknown_pages'], 1)
            self.assertNotIn('read_navigation_visible_rows', m)
            self.assertNotIn('read_navigation_reported_rows_page_total', m)
        m = measure('trace_read', read(session_navigation=[], session_navigation_count=0))
        for key in ('pages_with_rows', 'visible_rows', 'reported_rows_page_total',
                    'indexed_rows', 'missing_rows', 'ambiguous_rows'):
            self.assertIn('read_navigation_' + key, m)
            self.assertEqual(m['read_navigation_' + key], 0)
        m = measure('trace_read', read(session_navigation=[None, {},
                    {'indexed': 0, 'ambiguous': 'true'}], session_navigation_count=-1))
        self.assertEqual(m['read_navigation_visible_rows'], 3)
        self.assertEqual(m['read_navigation_malformed_rows'], 1)
        self.assertEqual(m['read_navigation_unknown_indexed_rows'], 3)
        self.assertEqual(m['read_navigation_unknown_ambiguous_rows'], 3)
        self.assertEqual(m['read_navigation_reported_rows_unknown_pages'], 1)

    def test_errors_inspection_and_unrecognized_responses_do_not_imply_exposure(self):
        for tool in ('trace_search', 'trace_read', 'trace_cooccur'):
            for text, metric in [('ERROR: failure', 'errors'), ('{', 'malformed'),
                                 ('[]', 'unknown'), ('{}', 'unknown'),
                                 ('{"error":"failure"}', 'errors'),
                                 ('{"isError":true}', 'errors')]:
                m = metrics(tool, text)
                self.assertEqual(m[metric], 1)
                self.assertFalse(any(k.startswith(('search_scope_', 'read_navigation_')) for k in m))
        m = measure('trace_read', {'plan_result': 'r1', 'session_navigation': [],
                                  'known_text_chars': 900})
        self.assertEqual(m['inspection_results'], 1)
        self.assertFalse(any(k.startswith('read_navigation_') for k in m))
        self.assertNotIn('read_text_chars', m)

    def test_repeated_pages_are_exposure_but_duplicate_result_links_are_not(self):
        rows = [call(1, 'trace_search'), call(2, 'trace_search'),
                call(3, 'trace_read'), call(4, 'trace_read')]
        bodies = {r['result_ref']: json.dumps(search(dict(include_references=False,
                    event_count=2, object_count=1, field_count=4)) if i < 2 else
                    read(session_navigation=[dict(indexed=True, ambiguous=False)],
                         session_navigation_count=5)) for i, r in enumerate(rows)}
        report = account(rows + [rows[0], rows[2]], bodies.__getitem__)
        total = report['sessions']['S']['totals']
        self.assertEqual((total['calls'], total['results'], total['duplicate_result_links']), (4, 4, 2))
        self.assertEqual(total['search_scope_event_count_page_total'], 4)
        self.assertEqual(total['search_scope_object_count_page_total'], 2)
        self.assertEqual(total['search_scope_field_count_page_total'], 8)
        self.assertEqual(total['read_navigation_pages_with_rows'], 2)
        self.assertEqual(total['read_navigation_visible_rows'], 2)
        self.assertEqual(total['read_navigation_reported_rows_page_total'], 10)
        self.assertEqual(total['read_navigation_indexed_rows'], 2)
        self.assertEqual(report['calls'][0]['results'][1]['counted'], False)
        self.assertFalse(any('unique' in key for key in total))

    def test_explicit_flags_are_per_call_not_inferred_effective_policy(self):
        rows = [call(1, 'trace_search', json.dumps(dict(include_references=False,
                    query='PRIVATE_QUERY', scope={'refs': ['PRIVATE_REF']}))),
                call(2, 'trace_search', '{"cursor":"PRIVATE_CURSOR"}'),
                call(3, 'trace_read', '{"output":"compact","inspect":true}'),
                call(4, 'trace_search', '{"include_references":true}'),
                call(5, 'trace_read', '{"output":"compact"}')]
        bodies = {r['result_ref']: 'ERROR: failure' for r in rows}
        bodies['S/r1'] = json.dumps(search(dict(include_references=True,
                      event_count=1, object_count=0, field_count=1)))
        bodies['S/r5'] = json.dumps(read(session_navigation=[
            dict(indexed=True, ambiguous=False, title='PRIVATE_TITLE')], session_navigation_count=1))
        report = account(rows + [rows[0]], bodies.__getitem__)
        total = report['sessions']['S']['totals']
        self.assertEqual(total['include_references_false'], 1)
        self.assertEqual(total['include_references_true'], 1)
        self.assertEqual(total['search_scope_include_references_true_pages'], 1)
        self.assertEqual(total['output_compact'], 2)
        self.assertEqual(total['inspect_true'], 1)
        self.assertEqual(total['errors'], 3)
        self.assertIs(report['calls'][0]['include_references'], False)
        self.assertIsNone(report['calls'][1]['include_references'])
        self.assertEqual(report['calls'][2]['output'], 'compact')
        self.assertEqual(report['calls'][4]['results'][0]['inline_source_chars'], 4)
        self.assertNotIn('PRIVATE_', json.dumps(report))
        self.assertNotIn('BODY', json.dumps(report))

    def test_malformed_arguments_missing_results_and_invalid_flags(self):
        invalid = ('{', 'null', '[]', 'true', '1', '"string"', None)
        rows = [call(i, 'trace_read', args) for i, args in enumerate(invalid)]
        rows.append(call(20, 'trace_search', '{"include_references":"false","output":{}}'))
        rows.append(call(21, 'trace_read', '{"output":"compact"}'))
        rows[-1]['result_ref'] = None
        report = account(rows, lambda _: 'ERROR: failure')
        total = report['sessions']['S']['totals']
        self.assertEqual(total['malformed_arguments'], len(invalid))
        self.assertEqual(total['calls'], len(rows))
        self.assertEqual(total['missing_results'], 1)
        self.assertEqual(total['output_compact'], 1)
        self.assertNotIn('include_references_false', total)
        for entry in report['calls'][:-1]:
            self.assertIsNone(entry['include_references'])
            self.assertIsNone(entry['output'])
            self.assertFalse(entry['inspect'])

    def test_cooccur_metadata_does_not_become_navigation_or_read_evidence(self):
        text = json.dumps({'rows': [{'anchor': {'event': 'S/e1'}}],
                           'scope_summary': {'include_references': False},
                           'session_navigation': [{'indexed': True}],
                           'session_navigation_count': 1})
        m = metrics('trace_cooccur', text)
        self.assertEqual(dict(m), dict(results=1, response_chars=len(text)))


if __name__ == '__main__':
    unittest.main()
