"""Keep new retrieval calls visible in future native CTF accounting."""
import json
import unittest

from scripts.measure_trace_trials import TOOLS, account, metrics


class CooccurAccountingTests(unittest.TestCase):
    def test_join_metadata_is_counted_but_not_read_evidence(self):
        self.assertIn('trace_cooccur', TOOLS)
        text = json.dumps({'operation': 'cooccur', 'status': 'complete', 'rows': [
            {'anchor': {'event': 'S/e1'}, 'conditions': [{'witnesses_result': 'r2'}]}]})
        result = metrics('trace_cooccur', text)
        self.assertEqual(result['results'], 1)
        self.assertEqual(result['response_chars'], len(text))
        self.assertEqual(result.get('unknown', 0), 0)
        self.assertEqual(result['read_text_chars'], 0)
        self.assertEqual(result['inline_source_chars'], 0)

    def test_errors_and_malformed_results_remain_visible(self):
        self.assertEqual(metrics('trace_cooccur', 'ERROR: exceeded limit')['errors'], 1)
        self.assertEqual(metrics('trace_cooccur', '{')['malformed'], 1)
        self.assertEqual(metrics('trace_cooccur', '{}')['unknown'], 1)

    def test_calls_and_duplicate_result_links_count_once(self):
        row = dict(ref='S/e1', session='S', tool='trace_cooccur', arguments='{}', result_ref='S/e2')
        result = account([row, row], lambda _: '{"rows":[]}')['sessions']['S']['totals']
        self.assertEqual((result['calls'], result['results'], result['duplicate_result_links']), (1, 1, 1))
        self.assertEqual(result.get('unknown', 0), 0)


if __name__ == '__main__':
    unittest.main()
