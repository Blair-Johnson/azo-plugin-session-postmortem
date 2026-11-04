"""Additive session navigation in engine reads; no external source loading."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from azo_session_postmortem import engine
from test_scope_controls import ScopeFixture


def receipt(job='rlm_job_1', child='rlm-42', outputs=1):
    lines = [f'Submitted RLM task {job} with profile default (test).',
             f'Child live session: {child}']
    if outputs:
        lines += ['Output buffers reserved:']
        lines += [f'- output_{n} -> buf_out{n} [reserved]' for n in range(outputs)]
    lines += [f"Use rlm_status(job_id='{job}') to inspect it or cancel_rlm(job_id='{job}') to kill it."]
    return '\n'.join(lines)


class NavigationIntegrationTests(ScopeFixture):
    def install_receipt(self, outputs=1):
        text = receipt(outputs=outputs)
        with self.db() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('rlm-42', 'S', 'child title', 'synthetic', '[]'))
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('S/launch', 'S', 20, None, 'result', 'tool', 'submit_rlm', None, '', None))
            db.execute('INSERT INTO _content VALUES(?,?,?)', ('S/launch', 'text', text))
            for n in range(outputs):
                ref = f'S/o_buf_out{n}'
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', (ref, 'buffer', 1, 4, 1, 'S', None))
                db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', 'BODY'))
                db.execute('INSERT INTO links VALUES(?,?,?)', ('S/e1', ref, 'references'))
            engine.ensure_session_links(db, rebuild=True)
        return text

    def test_navigation_is_additive_across_modes_and_inspection_unchanged(self):
        text = self.install_receipt()
        for output in ('inline', 'compact', 'buffer'):
            page = self.call('trace_read', targets=['S/launch'], pair=False, line_context=0, output=output)
            self.assertEqual(page['session_navigation_count'], 1)
            row = page['session_navigation'][0]
            self.assertEqual((row['src'], row['dst'], row['job_id']), ('S/launch', 'rlm-42', 'rlm_job_1'))
            self.assertTrue(row['indexed'])
            self.assertEqual(row['title'], 'child title')
            self.assertEqual(row['anchor'], {'event': 'S/launch', 'field': 'text', 'lines': [1, 2]})
            self.assertEqual(set(page['sources']), {'S/launch'})
            self.assertEqual(page['references'], [])
            if output == 'buffer':
                packet = Path(page['buffer_path']).read_text()
                self.assertTrue(packet.endswith(text))
                self.assertNotIn('session_navigation', packet)
            else:
                self.assertEqual(page['blocks'][0]['text'], text)
        with patch.object(engine, 'navigation_rows', side_effect=AssertionError('inspection queried navigation')):
            cost = self.call('trace_read', targets=['S/launch'], pair=False, inspect=True)
        self.assertEqual(cost['known_text_chars'], len(text))
        self.assertNotIn('session_navigation', cost)

    def test_navigation_uses_delivered_refs_not_pending_plan_or_destination(self):
        self.install_receipt()
        first = self.call('trace_read', targets=['S/e2', 'S/launch'], pair=False,
                          line_context=0, max_chars=1)
        self.assertEqual(first['session_navigation_count'], 0)
        self.assertEqual(set(first['sources']), {'S/e2'})
        cursor = first['next_cursor']
        with self.db() as db:
            # A source update must not replace the already frozen read body.
            db.execute('UPDATE _content SET text=? WHERE ref=?', ('CHANGED', 'S/launch'))
        rest = self.call('trace_read', cursor=cursor, max_chars=10000)
        self.assertEqual(rest['session_navigation_count'], 1)
        self.assertNotIn('rlm-42', rest['sources'])
        self.assertTrue(any('Submitted RLM task' in block['text'] for block in rest['blocks']))
        with self.assertRaisesRegex(ValueError, 'unknown source reference'):
            self.call('trace_read', targets=['rlm-42'])

    def test_overflow_preserves_rows_and_only_evidence_is_navigable(self):
        self.install_receipt(outputs=12)
        page = self.call('trace_read', targets=['S/e1'], pair=False, line_context=0)
        self.assertEqual(page['session_navigation_count'], 12)
        self.assertEqual(len(page['session_navigation']), engine.READ_PREVIEW_LIMIT)
        self.assertIn('session_navigation_result', page)
        result = page['session_navigation_result']
        tail = self.call('trace_query', cursor=page['session_navigation_cursor'])
        self.assertEqual(tail['row_count'], 12)
        self.assertEqual(len(tail['rows']), 4)
        with self.db() as db:
            _, rows, _ = engine._get_result(db, result)
            self.assertEqual(len(rows), 12)
            for row in rows:
                self.assertEqual(row['_anchors'], [row['anchor']])
                self.assertNotEqual(row['anchor']['event'], row['dst'])
            resolved = engine._resolve(db, [{'result': result}])
            self.assertTrue(all(anchor['event'] == 'S/launch' for anchor in resolved))
        read = self.call('trace_read', targets=[{'result': result, 'rows': [0]}], pair=False, line_context=0)
        self.assertEqual(set(read['sources']), {'S/launch'})
        self.assertTrue(read['blocks'][0]['text'].startswith('Submitted RLM task'))
        self.assertEqual(page['reference_count'], 15)  # legacy references stay independent

    def test_dispatch_backfills_existing_index_and_public_sql(self):
        self.install_receipt()
        with self.db() as db:
            db.execute('DROP TABLE session_links')
            db.execute('DROP TABLE _session_links_meta')
        opened = self.call('trace_open')
        self.assertEqual(opened['counts']['session_links'], 2)
        self.assertTrue(opened['capabilities']['session_navigation'])
        self.assertIn('session_links', opened['capabilities']['sql_tables'])
        rows = self.call('trace_query', sql='SELECT src,dst,kind,job_id,output_name,anchor FROM session_links')
        self.assertEqual(rows['row_count'], 2)
        for row in rows['rows']:
            self.assertEqual(json.loads(row['anchor'])['event'], 'S/launch')

    def test_trace_open_refresh_rebuilds_removed_relations(self):
        self.install_receipt()
        normalized = {'id': 'S', 'title': 'replacement', 'events': [], 'objects': [], 'links': []}
        with patch('azo_session_postmortem.source.load_sources', return_value=[normalized]) as loader:
            opened = self.call('trace_open', sessions=['synthetic-source'], descendants=False)
            loader.assert_called_once()
        self.assertEqual(opened['counts']['session_links'], 0)
        self.assertEqual(self.call('trace_query', sql='SELECT * FROM session_links')['row_count'], 0)
        # Resuming an open workspace does not reload any sources.
        with patch('azo_session_postmortem.source.load_sources', side_effect=AssertionError('loaded')):
            self.call('trace_open')


if __name__ == '__main__':
    unittest.main()
