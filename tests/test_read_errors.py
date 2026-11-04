"""Invalid event fields are actionable and fail whole read batches atomically."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from azo_session_postmortem import engine
from test_scope_controls import ScopeFixture


class ReadFieldErrorTests(ScopeFixture):
    def persisted(self):
        with self.db() as db:
            tables = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                      for table in ('_results', '_cursors', 'result_items', 'saved_queries')}
        packet = Path(self.workspace) / 'packets'
        return tables, {str(p): p.read_bytes() for p in packet.glob('*')}

    def test_mixed_batch_names_failing_member_and_valid_field_retry(self):
        before = self.persisted()
        targets = [{'event': 'S/e1', 'field': 'text'},
                   {'event': 'S/e2', 'field': 'raw_text'}]
        with self.assertRaises(ValueError) as caught:
            self.call('trace_read', targets=targets, pair=False, line_context=0)
        message = str(caught.exception)
        self.assertIn("ref='S/e2'", message)
        self.assertIn("requested field='raw_text'", message)
        self.assertIn('available fields=["arguments","metadata","text"]', message)
        example = json.loads(message.split('valid target example: ', 1)[1])
        self.assertEqual(example, {'event': 'S/e2', 'field': 'arguments'})
        self.assertEqual(self.persisted(), before)
        targets[1] = example
        valid = self.call('trace_read', targets=targets, pair=False, line_context=0)
        self.assertEqual([block['text'] for block in valid['blocks']], ['TOKEN\nsecond TOKEN', 'ARGUMENT'])
        self.assertEqual({block['anchor']['event'] for block in valid['blocks']}, {'S/e1', 'S/e2'})

    def test_all_render_modes_fail_before_plan_results_packets_or_navigation(self):
        for output in ('inline', 'compact', 'buffer'):
            with self.subTest(output=output):
                before = self.persisted()
                with patch.object(engine, '_store_result', side_effect=AssertionError('result persisted')), \
                     patch.object(engine, '_cursor', side_effect=AssertionError('cursor persisted')), \
                     patch.object(engine, 'navigation_rows', side_effect=AssertionError('navigation queried')):
                    with self.assertRaisesRegex(ValueError, 'S/e10'):
                        self.call('trace_read', targets=['S/e1', {'event': 'S/e10', 'field': 'text_raw'}],
                                  output=output, max_chars=1)
                self.assertEqual(self.persisted(), before)
                self.assertFalse((Path(self.workspace) / 'packets').exists())

    def test_inspection_invalid_field_has_no_persisted_effects(self):
        before = self.persisted()
        with patch.object(engine, '_inspect_read', side_effect=AssertionError('inspection ran')):
            with self.assertRaisesRegex(ValueError, "requested field='missing'"):
                self.call('trace_read', targets=['S/e1', {'event': 'S/e2', 'field': 'missing'}], inspect=True)
        self.assertEqual(self.persisted(), before)
        valid = self.call('trace_read', targets=[{'event': 'S/e2', 'field': 'metadata'}],
                          pair=False, line_context=0, inspect=True)
        self.assertEqual(valid['known_text_chars'], 4)
        self.assertNotIn('session_navigation', valid)

    def test_field_error_through_retained_selector_and_stale_map_is_atomic(self):
        selector = self.query('SELECT :anchor AS anchor', params={
            'anchor': json.dumps({'event': 'S/e2', 'field': 'absent'})})
        self.call('trace_edit', put=[{'id': 'stale', 'kind': 'episode', 'title': 'stale',
                                    'anchors': [{'event': 'S/e2', 'field': 'metadata'}]}])
        with self.db() as db:
            db.execute('DELETE FROM _content WHERE ref=? AND field=?', ('S/e2', 'metadata'))
        for bad in ({'result': selector}, {'entry': 'stale', 'part': 'anchors'}):
            before = self.persisted()
            with self.assertRaisesRegex(ValueError, 'S/e2'):
                self.call('trace_read', targets=['S/e1', bad], inspect=True)
            self.assertEqual(self.persisted(), before)

    def test_fields_and_lines_remain_strict(self):
        before = self.persisted()
        with self.assertRaisesRegex(ValueError, 'unknown event field'):
            self.call('trace_read', targets=[{'event': 'S/e1', 'field': 'Text'}])
        with self.assertRaisesRegex(ValueError, 'line range exceeds'):
            self.call('trace_read', targets=[{'event': 'S/e1', 'field': 'text', 'lines': [1, 99]}])
        self.assertEqual(self.persisted(), before)


if __name__ == '__main__':
    unittest.main()
