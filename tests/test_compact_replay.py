"""Guard against false-positive parity claims in the offline replay checker."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.check_compact import normalize, reference_state


class CompactReplayTests(unittest.TestCase):
    def test_delivery_is_reconstructed_and_bad_legacy_anchor_rejected(self):
        anchor = {'event': 'S/e1', 'field': 'text', 'lines': [1, 1]}
        compact = dict(packet_format='compact_read_json_v1', packet_chars=0,
                       blocks=[dict(anchor=anchor, text='abc', complete=False)])
        full = deepcopy(compact)
        full['packet_format'] = 'compact_json'
        full['delivered'] = [dict(anchor=anchor, char_offset=0, char_end=3)]
        self.assertEqual(normalize(full), normalize(compact))
        full['delivered'][0]['anchor'] = {'event': 'S/e2'}
        with self.assertRaises(AssertionError):
            normalize(full)

    def test_cards_and_unavailable_deliveries_are_not_discarded(self):
        compact = dict(packet_format='compact_read_json_v1', packet_chars=0,
                       blocks=[dict(entry_id='note', card={'title': 'Same'}),
                               dict(anchor='S/o-missing', available=False, reason='Missing')])
        result = normalize(compact)
        self.assertEqual(result['delivered'], [dict(entry='note', part='card'),
                                              dict(anchor='S/o-missing', available=False)])
        self.assertEqual(result['blocks'][1]['reason'], 'Missing')

    def test_legacy_unanchored_navigation_survives_normalization(self):
        compact = dict(packet_format='compact_read_json_v1', packet_chars=0,
                       blocks=[dict(available=False, reason='Old cursor')],
                       delivered=[dict(anchor='S/o-old', available=False)])
        full = deepcopy(compact)
        full['packet_format'] = 'compact_json'
        self.assertEqual(normalize(full), normalize(compact))
        self.assertEqual(normalize(compact)['delivered'][0]['anchor'], 'S/o-old')

    def test_equal_handles_do_not_hide_different_reference_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = [Path(tmp) / name for name in ('a', 'b')]
            for root in roots:
                root.mkdir()
                with sqlite3.connect(root / 'trace.sqlite3') as db:
                    db.execute('CREATE TABLE _results(id INTEGER, kind TEXT, rows TEXT, meta TEXT)')
                    db.execute('CREATE TABLE _cursors(id INTEGER, kind TEXT, payload TEXT)')
                    db.execute("INSERT INTO _results VALUES(1,'query','[]','{}')")
                    db.execute("INSERT INTO _cursors VALUES(1,'query','{}')")
            page = dict(references_result='r1', references_cursor='c1')
            self.assertEqual(reference_state(roots[0], page), reference_state(roots[1], page))
            with sqlite3.connect(roots[1] / 'trace.sqlite3') as db:
                db.execute("UPDATE _results SET rows='[{\"dst\":\"different\"}]'")
            self.assertNotEqual(reference_state(roots[0], page), reference_state(roots[1], page))
            with self.assertRaises(AssertionError):
                reference_state(roots[0], dict(references_result='r2'))


if __name__ == '__main__':
    unittest.main()
