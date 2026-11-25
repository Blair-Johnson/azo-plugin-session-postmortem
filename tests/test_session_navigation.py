"""Database-only contracts for observed RLM receipt navigation."""
import json
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

from azo_session_postmortem import engine, source
from azo_session_postmortem.navigation import ensure_session_links, navigation_rows


CHILD = 'ef6adac4-4da5-4517-a171-9403cec73d5e'
OTHER = 'b0652e03-3730-4913-9b44-cd57e4a286c7'
JOB = 'rlm_job_1792099753654470000'
RECEIPT = (
    f'Submitted RLM task {JOB} with profile simple (Simple).\n'
    f'Child live session: {CHILD}\n'
    'Task name (/attach label): resolve_joblib_integration\n'
    'Working directory: /work\n'
    'Input buffers:\n'
    '- buf_2727cb [ready]\n'
    'Output buffers reserved:\n'
    '- joblib_merge_resolution -> buf_f58d78 [reserved]\n'
    f"Use rlm_status(job_id='{JOB}') to inspect it or cancel_rlm(job_id='{JOB}') to kill it."
)
OUTPUT = 'S/o_buf_f58d78'


class SessionNavigationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(engine.SCHEMA)
        self.session('S')

    def session(self, sid, parent=None, title=''):
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)',
                        (sid, parent, title, '/never/read/session.json', '[]'))

    def event(self, text=RECEIPT, *, sid='S', seq=1, kind='result', role='tool',
              tool='submit_rlm', arguments=None):
        ref = f'{sid}/e{seq}'
        self.db.execute('INSERT INTO events '
                        '(ref,session,seq,kind,role,tool,arguments,preview) VALUES(?,?,?,?,?,?,?,?)',
                        (ref, sid, seq, kind, role, tool, arguments, text[:240]))
        self.db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', text))
        return ref

    def object(self, ref=OUTPUT, text=None):
        self.db.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?,?,?)',
                        (ref, 'buffer', 0 if text is None else len(text.splitlines()),
                         0 if text is None else len(text), int(text is not None),
                         ref.rsplit('/', 1)[0], 'Not saved'))
        if text is not None:
            self.db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'text', text))
        return ref

    def link(self, src, dst, kind='references'):
        self.db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?)', (src, dst, kind))

    def launch(self, text=RECEIPT, **kwargs):
        ref = self.event(text, **kwargs)
        sid = kwargs.get('sid', 'S')
        for handle in set(re.findall(r'\bbuf_[A-Za-z0-9_]+\b', text)):
            self.link(ref, self.object(f'{sid}/o_{handle}'))
        return ref

    def stored(self):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM session_links ORDER BY src,dst,job_id,anchor')]

    def snapshot(self):
        tables = ('sessions', 'events', 'objects', 'links', '_content', 'map_entries',
                  'map_members', 'map_links', 'result_items', '_results', '_cursors',
                  '_sources', 'saved_queries')
        return {table: [tuple(r) for r in self.db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in tables}

    def test_exact_real_receipt_and_evidence_anchors(self):
        ref = self.launch()
        self.session(CHILD, parent='S', title='Joblib integration')
        ensure_session_links(self.db)
        rows = self.stored()
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {'src', 'dst', 'kind', 'job_id', 'output_name', 'anchor'})
        child, output = sorted(rows, key=lambda r: r['kind'])
        self.assertEqual((child['src'], child['dst'], child['kind'], child['job_id']),
                         (ref, CHILD, 'rlm_child', JOB))
        self.assertIsNone(child['output_name'])
        self.assertEqual(json.loads(child['anchor']), {'event': ref, 'field': 'text', 'lines': [1, 2]})
        self.assertEqual((output['src'], output['output_name']), (OUTPUT, 'joblib_merge_resolution'))
        self.assertEqual(json.loads(output['anchor']), {'event': ref, 'field': 'text', 'lines': [1, 8]})
        direct = navigation_rows(self.db, [OUTPUT])[0]
        self.assertEqual(direct['association'], 'declared_output')
        self.assertTrue(direct['indexed'])
        self.assertEqual(direct['title'], 'Joblib integration')
        self.assertEqual(direct['observed_session'], 'S')
        self.assertEqual(direct['recorded_parent_id'], 'S')
        self.assertEqual(direct['parent_status'], 'match')
        self.assertFalse(direct['ambiguous'])
        self.assertNotIn('via', direct)
        self.assertNotIn('status', direct)
        launched = navigation_rows(self.db, [ref])
        self.assertEqual(len(launched), 2)
        self.assertEqual(launched[0]['association'], 'observed_submission')
        self.assertEqual(launched[1]['via'], OUTPUT)
        self.assertEqual(launched[1]['src'], ref)

    def test_non_tool_narrative_spoofs_and_other_fields_are_rejected(self):
        cases = (
            dict(kind='message', role='assistant'), dict(kind='message', role='user'),
            dict(kind='call', role='assistant'), dict(kind='result', role='assistant'),
            dict(tool='rlm_status'), dict(tool=None), dict(tool='fake.submit_rlm'),
        )
        for seq, kwargs in enumerate(cases, 1):
            self.event(seq=seq, **kwargs)
        ref = self.event('not a receipt', seq=20, arguments=json.dumps({'prompt': RECEIPT}))
        self.db.execute('UPDATE events SET preview=? WHERE ref=?', (RECEIPT, ref))
        self.db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, 'arguments', RECEIPT))
        self.object(text=RECEIPT)
        ensure_session_links(self.db)
        self.assertEqual(self.stored(), [])

    def test_malformed_embedded_and_status_receipts_are_rejected(self):
        invalid = [
            '', 'Here is an example:\n' + RECEIPT, '```\n' + RECEIPT + '\n```',
            RECEIPT + '\nTask completed successfully.', RECEIPT + '\n' + RECEIPT,
            RECEIPT.replace(f'Child live session: {CHILD}\n', ''),
            RECEIPT.replace(CHILD, 'not-a-session'), RECEIPT.replace(CHILD, '../session.json'),
            RECEIPT.replace(CHILD, ''), RECEIPT.replace('[reserved]', '[ready]'),
            RECEIPT.replace('- joblib_merge_resolution -> buf_f58d78 [reserved]\n', ''),
            RECEIPT.replace('- buf_2727cb [ready]\n', ''),
            RECEIPT.rsplit('\n', 1)[0], RECEIPT.replace("cancel_rlm(job_id='" + JOB,
                                                                "cancel_rlm(job_id='rlm_job_1"),
            RECEIPT.replace('Output buffers reserved:', 'Output buffers:'),
            RECEIPT.replace('Working directory: /work', 'Unrecognized receipt line'),
            json.dumps({'status': 'completed', 'result': RECEIPT, 'child_session_id': CHILD}),
            RECEIPT.replace('Submitted RLM task', 'Resumed RLM task'),
        ]
        for seq, text in enumerate(invalid, 1):
            self.event(text, seq=seq)
        ensure_session_links(self.db)
        self.assertEqual(self.stored(), [])

    def test_multiple_outputs_and_input_exclusion(self):
        text = RECEIPT.replace('- buf_2727cb [ready]',
                               '- buf_2727cb (from prepared_input) [waiting]\n- transcript [ready]')
        text = text.replace('Output buffers reserved:',
                            'Output buffers reserved:\n- another result α -> buf_bbbbbb [reserved]')
        ref = self.launch(text)
        ensure_session_links(self.db)
        outputs = [r for r in self.stored() if r['kind'] == 'rlm_output']
        self.assertEqual({r['output_name'] for r in outputs}, {'joblib_merge_resolution', 'another result α'})
        self.assertEqual(navigation_rows(self.db, ['S/o_buf_2727cb']), [])
        self.assertEqual({r['via'] for r in navigation_rows(self.db, [ref]) if 'via' in r},
                         {OUTPUT, 'S/o_buf_bbbbbb'})
        self.assertTrue(all(not r['ambiguous'] for r in navigation_rows(self.db, [ref])))

    def test_confirmed_resume_format_and_child_id_fallback(self):
        text = RECEIPT.replace(f'Child live session: {CHILD}',
                               f'Child live session: {CHILD}\nResuming completed task: rlm_job_123')
        self.launch(text)
        self.launch(RECEIPT.replace(CHILD, 'rlm-1792099753654470000'), seq=2)
        ensure_session_links(self.db)
        children = [r for r in self.stored() if r['kind'] == 'rlm_child']
        self.assertEqual(len(children), 2)
        self.assertEqual(json.loads(children[0]['anchor'])['lines'], [1, 3])
        self.assertEqual(children[0]['job_id'], JOB)
        self.assertEqual(children[1]['dst'], 'rlm-1792099753654470000')
        self.assertTrue(all('resume_job_id' not in r for r in navigation_rows(self.db, ['S/e1'])))

    def test_minimal_receipt_without_inputs_or_outputs(self):
        lines = RECEIPT.splitlines()
        ref = self.event('\n'.join([lines[0], lines[1], lines[-1]]))
        ensure_session_links(self.db)
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(navigation_rows(self.db, [ref])[0]['kind'], 'rlm_child')

    def test_missing_output_bytes_or_object_does_not_erase_declaration(self):
        self.event()
        ensure_session_links(self.db)
        self.assertEqual(navigation_rows(self.db, [OUTPUT])[0]['association'], 'declared_output')
        self.object()
        self.link('S/e2', OUTPUT)
        event = self.event('inspect buf_f58d78', seq=2, kind='message', role='assistant', tool=None)
        indirect = navigation_rows(self.db, [event])[0]
        self.assertEqual(indirect['via'], OUTPUT)
        self.assertEqual(indirect['anchor']['event'], 'S/e1')
        self.assertFalse(indirect['indexed'])
        self.assertIsNone(indirect['title'])
        self.assertEqual(indirect['parent_status'], 'unknown')
        self.assertEqual(self.db.execute('SELECT available FROM objects WHERE ref=?', (OUTPUT,)).fetchone()[0], 0)
        self.assertIsNone(self.db.execute('SELECT * FROM _content WHERE ref=?', (OUTPUT,)).fetchone())

    def test_existing_output_content_still_only_means_declared_output(self):
        self.event()
        self.object(text='A stored result, not validated by navigation.')
        ensure_session_links(self.db)
        row = navigation_rows(self.db, [OUTPUT])[0]
        self.assertEqual(row['association'], 'declared_output')
        self.assertNotIn('complete', row)
        self.assertNotIn('correct', row)

    def test_copied_duplicate_receipts_preserve_observations_and_scopes(self):
        self.session('T')
        self.session(CHILD, parent='S', title='Original child')
        self.launch()
        self.launch(seq=2)
        self.launch(sid='T')
        ensure_session_links(self.db)
        self.assertEqual(len(self.stored()), 6)
        ensure_session_links(self.db)
        self.assertEqual(len(self.stored()), 6)
        row = self.stored()[0]
        self.db.execute('INSERT OR IGNORE INTO session_links VALUES(?,?,?,?,?,?)', tuple(row.values()))
        self.assertEqual(len(self.stored()), 6)  # NULL output_name is unique too.
        original = navigation_rows(self.db, [OUTPUT])
        copied = navigation_rows(self.db, ['T/o_buf_f58d78'])
        self.assertEqual(len(original), 2)
        self.assertFalse(any(r['ambiguous'] for r in original + copied))
        self.assertEqual({r['anchor']['event'] for r in original}, {'S/e1', 'S/e2'})
        self.assertEqual(copied[0]['observed_session'], 'T')
        self.assertEqual(copied[0]['parent_status'], 'mismatch')
        self.assertEqual(copied[0]['recorded_parent_id'], 'S')

    def test_conflicts_preserved_and_flagged_not_repaired(self):
        self.launch()
        self.launch(RECEIPT.replace(CHILD, OTHER), seq=2)
        self.launch(RECEIPT.replace(JOB, 'rlm_job_456'), seq=3)
        ensure_session_links(self.db)
        rows = navigation_rows(self.db, [OUTPUT])
        self.assertEqual(len(rows), 3)
        self.assertEqual({(r['dst'], r['job_id']) for r in rows},
                         {(CHILD, JOB), (OTHER, JOB), (CHILD, 'rlm_job_456')})
        self.assertTrue(all(r['ambiguous'] for r in rows))
        self.assertTrue(all(r['ambiguous'] for r in navigation_rows(self.db, ['S/e1'])))
        ensure_session_links(self.db, rebuild=True)
        self.assertEqual(rows, navigation_rows(self.db, [OUTPUT]))

    def test_one_hop_only_no_reverse_or_object_chaining(self):
        self.event()
        self.object()
        reader = self.event('read output', seq=2, kind='call', role='assistant', tool='view')
        self.link(reader, OUTPUT)
        other_object = self.object('S/o_buf_cccccc')
        self.link(other_object, OUTPUT)
        reader2 = self.event('read intermediate', seq=3, kind='call', role='assistant', tool='view')
        self.link(reader2, other_object)
        reader3 = self.event('not a reference', seq=4)
        self.link(reader3, OUTPUT, 'call_result')
        self.session(CHILD)
        child_event = self.event('child message', sid=CHILD, kind='message', role='assistant', tool=None)
        ensure_session_links(self.db)
        self.assertEqual(navigation_rows(self.db, [reader])[0]['via'], OUTPUT)
        self.assertEqual(navigation_rows(self.db, [reader2, other_object, child_event, CHILD]), [])
        self.assertTrue(all('via' not in r for r in navigation_rows(self.db, [reader3])))

    def test_dynamic_title_indexed_and_parent_metadata(self):
        self.launch()
        ensure_session_links(self.db)
        self.assertFalse(navigation_rows(self.db, [OUTPUT])[0]['indexed'])
        self.session(CHILD, title='Now indexed')
        row = navigation_rows(self.db, [OUTPUT])[0]
        self.assertTrue(row['indexed'])
        self.assertEqual(row['title'], 'Now indexed')
        self.assertEqual(row['parent_status'], 'unknown')
        self.session(CHILD, parent='S', title='Renamed')
        self.assertEqual(navigation_rows(self.db, [OUTPUT])[0]['parent_status'], 'match')
        self.session(CHILD, parent='different', title='Renamed again')
        row = navigation_rows(self.db, [OUTPUT])[0]
        self.assertEqual(row['title'], 'Renamed again')
        self.assertEqual(row['parent_status'], 'mismatch')

    def test_backfill_once_and_rebuild_preserve_original_and_authored_state(self):
        self.launch()
        self.db.execute('INSERT INTO map_entries VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('note', 'note', 'Keep', 'Authored', '[]', '[]', '{}', 1, None, 7))
        self.db.execute('INSERT INTO map_members VALUES(?,?)', ('note', 'S/e1'))
        self.db.execute('INSERT INTO map_links VALUES(?,?,?,?,?)', ('edge', 'note', 'note', 'keep', ''))
        self.db.execute('INSERT INTO _results VALUES(?,?,?,?)', (1, 'query', '[{"sentinel":true}]', '{}'))
        self.db.execute('INSERT INTO result_items VALUES(?,?,?,?,?)', ('r1', 0, 'S/e1', '"S/e1"', None))
        self.db.execute('INSERT INTO _cursors VALUES(?,?,?)', (1, 'read', '{"snapshot":"keep"}'))
        self.db.execute('INSERT INTO _sources VALUES(?,?)', ('/never/read/session.json', 0))
        self.db.execute('INSERT INTO saved_queries VALUES(?,?,?,?)', ('keep', 'trace_query', '{}', 'r1'))
        before = self.snapshot()
        ensure_session_links(self.db)
        self.assertEqual(before, self.snapshot())
        self.db.execute('UPDATE _content SET text=? WHERE ref=? AND field=?',
                        (RECEIPT.replace(CHILD, OTHER), 'S/e1', 'text'))
        statements = []
        self.db.set_trace_callback(statements.append)
        ensure_session_links(self.db)
        self.db.set_trace_callback(None)
        self.assertFalse(any('FROM events' in s for s in statements))
        self.assertEqual({r['dst'] for r in self.stored()}, {CHILD})
        before_rebuild = self.snapshot()
        ensure_session_links(self.db, rebuild=True)
        self.assertEqual(before_rebuild, self.snapshot())
        self.assertEqual({r['dst'] for r in self.stored()}, {OTHER})
        self.db.execute('DELETE FROM events WHERE ref=?', ('S/e1',))
        before_delete_rebuild = self.snapshot()
        ensure_session_links(self.db, rebuild=True)
        self.assertEqual(self.stored(), [])
        self.assertEqual(before_delete_rebuild, self.snapshot())

    def test_rebuild_retains_other_link_kinds_and_respects_transaction(self):
        self.launch()
        ensure_session_links(self.db)
        self.db.execute('INSERT INTO session_links VALUES(?,?,?,?,?,?)',
                        ('other', 'destination', 'future_kind', 'job', None, '{}'))
        self.db.commit()
        before = self.stored()
        self.db.execute('BEGIN')
        self.db.execute('UPDATE _content SET text=? WHERE ref=? AND field=?',
                        (RECEIPT.replace(CHILD, OTHER), 'S/e1', 'text'))
        ensure_session_links(self.db, rebuild=True)
        self.assertEqual(len(self.stored()), 3)
        self.assertEqual(self.stored()[-1]['kind'], 'future_kind')
        self.assertEqual(navigation_rows(self.db, ['other']), [])
        self.db.rollback()
        self.assertEqual(before, self.stored())

    def test_version_change_backfills_without_source_reload(self):
        self.launch()
        ensure_session_links(self.db)
        self.db.execute('DELETE FROM session_links')
        self.db.execute('UPDATE _session_links_meta SET version=0')
        ensure_session_links(self.db)
        self.assertEqual(len(self.stored()), 2)

    def test_no_automatic_child_loading_and_navigation_is_read_only(self):
        self.launch()
        before = self.snapshot()
        with patch.object(source, 'load_sources', side_effect=AssertionError('autoload')), \
                patch('builtins.open', side_effect=AssertionError('filesystem access')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('filesystem access')):
            ensure_session_links(self.db)
            statements = []
            self.db.set_trace_callback(statements.append)
            rows = navigation_rows(self.db, ['S/e1', OUTPUT])
            self.db.set_trace_callback(None)
        self.assertTrue(rows)
        self.assertTrue(all(not r['indexed'] for r in rows))
        self.assertTrue(all(s.lstrip().upper().startswith('SELECT') for s in statements))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)

    def test_deterministic_rows_and_duplicate_ref_inputs(self):
        self.launch()
        self.launch(RECEIPT.replace(CHILD, OTHER), seq=2)
        ensure_session_links(self.db)
        expected = navigation_rows(self.db, ['S/e1', OUTPUT, 'S/e2'])
        self.assertEqual(expected, navigation_rows(self.db, ['S/e2', OUTPUT, 'S/e1', OUTPUT, 'S/e2']))
        self.assertEqual(navigation_rows(self.db, []), [])
        self.assertEqual(navigation_rows(self.db, ['unknown']), [])

    def test_normalized_saved_tool_event_shape_is_supported(self):
        doc = {'id': 'N', 'entries': [{'tool_calls': [{
            'id': 'call1', 'name': 'submit_rlm', 'parsed_args': {'prompt': 'Work'}, 'result': RECEIPT,
        }]}]}
        normalized = source._normalize(doc, Path('/not/read.json'), [], {})
        for event in normalized['events']:
            if event['kind'] == 'result':
                self.event(event['text'], sid='N', kind=event['kind'], role=event['role'], tool=event['tool'])
        ensure_session_links(self.db)
        self.assertEqual(len(self.stored()), 2)
        self.assertEqual(navigation_rows(self.db, ['N/o_buf_f58d78'])[0]['observed_session'], 'N')

    def test_v04_submit_rlm_receipt_and_attrs_rlm_jobs_link_extraction(self):
        v04_job = 'rlm_1a2b3c4d'
        v04_receipt = (
            f'Submitted RLM {v04_job} (simple (Simple)) | name=analyze_logs\n'
            'resuming completed: rlm_00001111\n'
            'cwd: /work\n'
            'inputs: buf_2727cb [ready], buf_222222 (from prev) [waiting]\n'
            'outputs: summary -> buf_f58d78, notes -> buf_bbbbbb\n'
            'status: waiting for depth-1 concurrency capacity'
        )
        doc = {
            'format_version': 2,
            'meta': {'session_id': 'P04'},
            'features': {
                'session': {
                    '__durable_v__': 1,
                    '__data__': {'title': 'Parent 0.4', 'parent_session_id': ''},
                }
            },
            'attrs': {
                'rlm_jobs': {
                    v04_job: {
                        '__dataclass__': 'agent_zoo.features.rlm.models.RLMJobRecord',
                        '__fields__': {
                            'job_id': v04_job,
                            'child_session_id': CHILD,
                            'output_map': {'summary': 'buf_f58d78', 'notes': 'buf_bbbbbb'},
                        },
                    }
                }
            },
            'entries': [
                {
                    'index': 0,
                    'step': 1,
                    'tool_calls': [
                        {
                            'id': 'call_v04',
                            'name': 'submit_rlm',
                            'parsed_args': {'prompt': 'Analyze'},
                            'result': v04_receipt,
                        }
                    ],
                }
            ],
        }
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_path = root / 'parent_v04.json'
            src_path.write_text(json.dumps(doc), encoding='utf-8')
            ws = root / 'ws'
            engine.dispatch('trace_open', {'workspace': str(ws), 'sessions': [str(src_path)], 'descendants': False})
            with sqlite3.connect(str(ws / 'trace.sqlite3')) as ws_db:
                ws_db.row_factory = sqlite3.Row
                rows = [dict(r) for r in ws_db.execute('SELECT * FROM session_links ORDER BY kind, src, output_name')]
                self.assertEqual(len(rows), 3)
                child_rows = [r for r in rows if r['kind'] == 'rlm_child']
                output_rows = [r for r in rows if r['kind'] == 'rlm_output']
                self.assertEqual(len(child_rows), 1)
                self.assertEqual(child_rows[0]['dst'], CHILD)
                self.assertEqual(child_rows[0]['job_id'], v04_job)
                self.assertEqual(json.loads(child_rows[0]['anchor'])['lines'], [1, 2])
                self.assertEqual(
                    {(r['src'], r['dst'], r['output_name']) for r in output_rows},
                    {
                        ('P04/o_buf_f58d78', CHILD, 'summary'),
                        ('P04/o_buf_bbbbbb', CHILD, 'notes'),
                    },
                )
                for r in output_rows:
                    self.assertEqual(json.loads(r['anchor'])['lines'], [1, 5])
                nav = navigation_rows(ws_db, ['P04/o_buf_f58d78'])
                self.assertEqual(len(nav), 1)
                self.assertEqual(nav[0]['dst'], CHILD)
                self.assertEqual(nav[0]['job_id'], v04_job)
                self.assertEqual(nav[0]['association'], 'declared_output')

        # Embedded/spoofed 0.4.0 receipts are still rejected.
        self.db.execute('INSERT OR REPLACE INTO _rlm_jobs VALUES(?,?,?,?)', ('S', v04_job, CHILD, '{}'))
        self.event('Prose prefix\n' + v04_receipt, seq=90)
        self.event(v04_receipt + '\nTrailing spoof', seq=91)
        ensure_session_links(self.db, rebuild=True)
        self.assertEqual(self.stored(), [])


if __name__ == '__main__':
    unittest.main()
