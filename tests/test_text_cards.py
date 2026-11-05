"""Native map-card pagination must not expose internal JSON fragments."""
from pathlib import Path
import tempfile
import unittest

from azo_session_postmortem.engine import dispatch
from azo_session_postmortem.render import render


class TextCardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = str(Path(temp.name) / 'study')
        self.call('trace_open')
        self.note = 'α\r\nKept exact note.\n' * 20
        self.call('trace_edit', put=[dict(id='next', kind='note', title='Continue',
                                        note=self.note, fields={'rival': 'Still possible'})])

    def call(self, tool, **arguments):
        return dispatch(tool, dict(workspace=self.workspace, **arguments))

    def test_text_card_reassembles_from_paged_source_bodies(self):
        target = [{'entry': 'next', 'part': 'card'}]
        full = self.call('trace_read', targets=target, output='text')
        expected = full['blocks'][0]['text']
        self.assertIn(self.note, expected)
        self.assertTrue(expected.startswith('Map next — Continue'))
        self.assertNotIn('"anchors":', expected)
        page = self.call('trace_read', targets=target, output='text', max_chars=17)
        bodies = []
        for _ in range(100):
            for block in page['blocks']:
                self.assertEqual(block['format'], 'map_card_text')
                self.assertNotIn('card', block)
                bodies.append(block['text'])
                self.assertIn(block['text'], render('trace_read', page))
            if not page['next_cursor']:
                break
            page = self.call('trace_read', cursor=page['next_cursor'], output='text', max_chars=17)
        else:
            self.fail('Pagination did not terminate')
        self.assertEqual(''.join(bodies), expected)

    def test_text_card_inspection_keeps_readable_entry_address(self):
        plan = self.call('trace_read', targets=[{'entry': 'next', 'part': 'card'}],
                         output='text', inspect=True, max_chars=17)
        self.assertEqual(plan['rows'][0]['kind'], 'map_card')
        self.assertEqual(plan['rows'][0]['anchor'], {'entry': 'next', 'part': 'card'})
        self.assertEqual(plan['predicted_text_chars'], 17)
        read = self.call('trace_read', targets=[{'result': plan['plan_result'], 'rows': [0]}],
                         output='text')
        self.assertIn(self.note, read['blocks'][0]['text'])

    def test_old_structured_card_and_cursor_layout_are_unchanged(self):
        target = [{'entry': 'next', 'part': 'card'}]
        full = self.call('trace_read', targets=target)
        self.assertEqual(full['blocks'][0]['card']['note'], self.note)
        legacy = self.call('trace_read', targets=target, max_chars=17)
        self.assertEqual(legacy['blocks'][0]['format'], 'map_card_json')
        rest = self.call('trace_read', cursor=legacy['next_cursor'], output='text')
        self.assertEqual(rest['blocks'][0]['format'], 'map_card_json')


if __name__ == '__main__':
    unittest.main()
