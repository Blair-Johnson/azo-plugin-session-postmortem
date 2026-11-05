"""Native text is measurable transport, not a structured metadata source."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.measure_trace_trials import TOOLS, account, metrics


class TextAccountingTests(unittest.TestCase):
    def test_text_responses_count_unicode_without_parsing_prose(self):
        text = 'Trace response: café 🧭\ntext_chars=123; indexed=true; buffer=buf_fake'
        for tool in TOOLS:
            with self.subTest(tool=tool):
                m = metrics(tool, text)
                self.assertEqual(m['results'], 1)
                self.assertEqual(m['response_chars'], len(text))
                self.assertEqual(m['text_responses'], 1)
                self.assertEqual(m['text_response_chars'], len(text))
                self.assertEqual(m['structured_metadata_unavailable'], 1)
                for key in ('malformed', 'unknown', 'read_text_chars',
                            'read_packet_chars', 'inline_source_chars',
                            'buffer_exports', 'buffer_export_text_chars',
                            'inspection_results', 'read_navigation_visible_rows'):
                    self.assertNotIn(key, m)

    def test_search_and_read_metadata_are_unknown_not_zero_exposure(self):
        m = metrics('trace_search', 'Search: 2 hits; follow_object_links=false')
        self.assertEqual(m['search_scope_summary_unknown_pages'], 1)
        self.assertEqual(m['search_scope_include_references_unknown_pages'], 1)
        for field in ('event', 'object', 'field'):
            self.assertEqual(m[f'search_scope_{field}_count_unknown_pages'], 1)
            self.assertNotIn(f'search_scope_{field}_count_page_total', m)
        m = metrics('trace_read', 'Read: navigation=5; text_chars=100')
        for key in ('read_navigation_unknown_pages',
                    'read_navigation_reported_rows_unknown_pages',
                    'unknown_text_chars', 'unknown_packet_chars',
                    'unknown_inline_source_chars', 'unknown_read_delivery_pages'):
            self.assertEqual(m[key], 1)
        self.assertNotIn('read_navigation_reported_rows_page_total', m)
        self.assertNotIn('read_navigation_indexed_rows', m)

    def test_legacy_json_and_error_classification_are_unchanged(self):
        for text, key in (('{', 'malformed'), ('[bad', 'malformed'),
                          ('"unfinished', 'malformed'), ('', 'malformed'),
                          ('[]', 'unknown'), ('{}', 'unknown'),
                          ('ERROR: failed', 'errors'),
                          ('{"error":"failed"}', 'errors')):
            m = metrics('trace_read', text)
            expected = dict(results=1, **{key: 1})
            if text:  # Legacy Counter addition discards zero-valued keys.
                expected['response_chars'] = len(text)
            self.assertEqual(dict(m), expected)
        text = json.dumps(dict(rows=[], counts={'matched_seed_events': 0}))
        self.assertEqual(dict(metrics('trace_cooccur', text)),
                         dict(results=1, response_chars=len(text)))
        text = json.dumps(dict(blocks=[dict(anchor={'event': 'S/e1'}, text='body')],
                               text_chars=4, packet_chars=80,
                               session_navigation=[], session_navigation_count=0))
        m = metrics('trace_read', text)
        self.assertEqual((m['read_text_chars'], m['read_packet_chars'],
                          m['inline_source_chars']), (4, 80, 4))
        self.assertNotIn('text_responses', m)
        self.assertNotIn('structured_metadata_unavailable', m)
        self.assertIn('read_navigation_visible_rows', m)
        self.assertEqual(m['read_navigation_visible_rows'], 0)

    def test_explicit_new_flags_are_sanitized_without_effective_policy_inference(self):
        arguments = [dict(follow_object_links=False, query='PRIVATE_QUERY'),
                     dict(follow_object_links=True),
                     dict(follow_object_links='false'),
                     dict(cursor='PRIVATE_CURSOR'),
                     dict(include_references=True, follow_object_links=False),
                     dict(output='text'), dict(output={'bad': True})]
        rows = [dict(ref=f'S/c{i}', session='S', tool='trace_search' if i < 5 else 'trace_read',
                     arguments=json.dumps(args), result_ref=f'S/r{i}')
                for i, args in enumerate(arguments)]
        report = account(rows, lambda _: 'Native response: PRIVATE_BODY')
        calls = report['calls']
        self.assertEqual([c['follow_object_links'] for c in calls[:5]],
                         [False, True, None, None, False])
        self.assertIsNone(calls[0]['include_references'])
        self.assertIs(calls[4]['include_references'], True)
        self.assertEqual(calls[5]['output'], 'text')
        self.assertIsNone(calls[6]['output'])
        total = report['sessions']['S']['totals']
        self.assertEqual(total['follow_object_links_false'], 2)
        self.assertEqual(total['follow_object_links_true'], 1)
        self.assertEqual(total['include_references_true'], 1)
        self.assertEqual(total['output_text'], 1)
        self.assertEqual(total['search_scope_include_references_unknown_pages'], 5)
        self.assertNotIn('search_scope_include_references_false_pages', total)
        self.assertNotIn('PRIVATE_', json.dumps(report))

    def test_mixed_results_and_duplicates_keep_known_metadata_separate(self):
        rows = [dict(ref=f'S/c{i}', session='S', tool='trace_read', arguments='{}',
                     result_ref=f'S/r{i}') for i in range(2)]
        bodies = {'S/r0': json.dumps(dict(blocks=[dict(anchor={'event': 'S/e1'}, text='body')],
                    text_chars=4, packet_chars=80)), 'S/r1': 'Native read: text_chars=999'}
        report = account(rows + [rows[1]], bodies.__getitem__)
        total = report['sessions']['S']['totals']
        self.assertEqual((total['results'], total['text_responses'],
                          total['duplicate_result_links']), (2, 1, 1))
        self.assertEqual(total['response_chars'], sum(map(len, bodies.values())))
        self.assertEqual(total['read_text_chars'], 4)
        self.assertEqual(total['inline_source_chars'], 4)
        self.assertEqual(total['unknown_text_chars'], 1)
        self.assertEqual(total['structured_metadata_unavailable'], 1)
        self.assertIs(report['calls'][1]['results'][1]['counted'], False)

    def test_new_explicit_flags_are_counted_on_errors_and_missing_results(self):
        rows = [dict(ref='S/c0', session='S', tool='trace_search',
                     arguments='{"follow_object_links":false}', result_ref='S/r0'),
                dict(ref='S/c1', session='S', tool='trace_read',
                     arguments='{"output":"text"}', result_ref=None)]
        total = account(rows, lambda _: 'ERROR: failure')['sessions']['S']['totals']
        self.assertEqual(total['follow_object_links_false'], 1)
        self.assertEqual(total['output_text'], 1)
        self.assertEqual(total['errors'], 1)
        self.assertEqual(total['missing_results'], 1)
        self.assertNotIn('text_responses', total)


class EngineCompactReplayTests(unittest.TestCase):
    def test_historical_comparison_reports_engine_not_native(self):
        from azo_session_postmortem import engine
        from scripts.check_compact import main
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.json'
            source.write_text(json.dumps({'format_version': 2, 'meta': {'session_id': 'S'},
                'entries': [{'index': 0, 'step': 0, 'messages': [
                    {'role': 'user', 'content': 'body café'}]}]}))
            workspace = root / 'study'
            engine.dispatch('trace_open', dict(workspace=str(workspace), sessions=[str(source)]))
            output = io.StringIO()
            with patch('sys.argv', ['check_compact', str(workspace), '--label', 'synthetic',
                                   '--read-json', '{"targets":["S/e1"],"pair":false}']), contextlib.redirect_stdout(output):
                main()
            report = json.loads(output.getvalue())
            self.assertEqual(report['comparison'], 'engine_inline_vs_compact_json')
            self.assertNotIn('native_module', report)
            self.assertTrue(report['same_evidence_and_navigation'])
            self.assertTrue(report['maps_unchanged'])


class EngineCompatibilityTests(unittest.TestCase):
    def test_historical_inspection_and_shared_replay_client(self):
        from azo_session_postmortem import engine
        from scripts.check_inspection import main
        from scripts.replay_cooccur import EngineClient
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.json'
            source.write_text(json.dumps({'format_version': 2, 'meta': {'session_id': 'S'},
                'entries': [{'index': 0, 'step': 0, 'messages': [
                    {'role': 'user', 'content': 'Synthetic checker evidence'}]}]}))
            workspace = root / 'study'
            engine.dispatch('trace_open', dict(workspace=str(workspace), sessions=[str(source)]))
            output = io.StringIO()
            with patch('sys.argv', ['checker', str(workspace), '--read-json',
                                   '{"targets":["S/e1"],"pair":false}']), contextlib.redirect_stdout(output):
                main()
            report = json.loads(output.getvalue())
            self.assertEqual(report['comparison'], 'engine_inspection_compatibility')
            self.assertTrue(report['engine_packet_file'])
            self.assertTrue(report['map_unchanged'])
            self.assertNotIn('native_response_chars', report)
            client = EngineClient(workspace)
            opened = client.call('trace_open')
            self.assertEqual(opened['counts']['sessions'], 1)
            self.assertEqual(client.response_chars, len(json.dumps(opened, ensure_ascii=False)))
            self.assertEqual(client.calls, 1)


@unittest.skipUnless(importlib.util.find_spec('agent_utils'), 'requires Agent Zoo host')
class NativeProbeTests(unittest.TestCase):
    def setUp(self):
        from agent_utils import State
        from agent_utils.files.buffer_manager import BufferManager
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.json'
        self.source_text = 'body café 🧭\r\n' + ('complete evidence ' * 1000) + '\r\nlate target'
        self.source.write_text(json.dumps({'format_version': 2, 'meta': {'session_id': 'S'},
            'entries': [{'index': 0, 'step': 0, 'messages': [
                {'role': 'user', 'content': self.source_text}]}]}))
        self.state = State()
        self.state.session_cwd, self.state.tool_schemas = temporary.name, {}
        self.state.buffer_manager = BufferManager()

    def invoke(self, name, **arguments):
        from scripts.native_probe import invoke
        return invoke(self.state, name, dict(workspace='study', **arguments))

    def test_probe_observes_real_cache_envelopes_and_accounts_inline_only(self):
        from azo_session_postmortem import native
        original_dispatch = native.cache.dispatch
        original_preview = native.cache_render.render_preview
        responses = []
        opened = self.invoke('trace_open', sessions=[str(self.source)])
        responses.append(opened)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(opened.text)
        responses.append(self.invoke('trace_read', targets=['S/e1'], pair=False,
                                     preview_chars=400))
        responses.append(self.invoke('trace_query', sql='SELECT ref FROM events'))
        responses.append(self.invoke('trace_search', query='late target', mode='literal'))
        query_id = responses[2].require_body()['document']['id']
        responses.append(self.invoke('trace_cooccur', seed={'result': query_id}, against=[
            dict(name='same', selection={'result': query_id}, before=0, after=0)]))
        buffer_chars = 0
        for response in responses:
            envelope = response.require_body()
            document = envelope['document']
            self.assertEqual(document, response.render_document)
            self.assertEqual(response.text, original_preview(document, response.buffer_id,
                                                             envelope['preview_chars']))
            self.assertEqual(envelope['text'], native.cache_render.render_full(document))
            self.assertTrue(response.buffer_id.startswith('buf_'))
            content = self.state.buffer_manager.resolve_for_read(self.state, response.buffer_id).text
            self.assertEqual(content, envelope['text'])
            with open(envelope['path'], encoding='utf-8', newline='') as packet:
                self.assertEqual(packet.read(), content)
            buffer_chars += len(content)
            measured = metrics(document['operation'], response.text)
            self.assertEqual(measured['response_chars'], len(response.text))
            self.assertEqual(measured['text_response_chars'], len(response.text))
            self.assertEqual(measured['structured_metadata_unavailable'], 1)
            self.assertNotIn('buffer_export_text_chars', measured)
        read = responses[1]
        self.assertIn(self.source_text, read.require_body()['text'])
        self.assertNotIn(self.source_text, read.text)
        self.assertGreater(len(read.require_body()['text']), len(read.text))
        rows = [dict(ref=f'S/c{i}', session='S', tool=r.require_body()['document']['operation'],
                     arguments='{}', result_ref=f'S/r{i}') for i, r in enumerate(responses)]
        texts = {f'S/r{i}': r.text for i, r in enumerate(responses)}
        totals = account(rows, texts.__getitem__)['sessions']['S']['totals']
        self.assertEqual(totals['response_chars'], sum(len(r.text) for r in responses))
        self.assertNotEqual(totals['response_chars'], buffer_chars)
        self.assertIs(native.cache.dispatch, original_dispatch)
        self.assertIs(native.cache_render.render_preview, original_preview)
        failed = self.invoke('trace_query', sql='DELETE FROM events')
        self.assertTrue(failed.error)
        self.assertEqual(failed.text, self.state.pending_tool_calls[0].result)
        self.assertEqual(metrics('trace_query', failed.text)['response_chars'], len(failed.text))
        with self.assertRaises(RuntimeError):
            failed.require_body()
        self.assertIs(native.cache.dispatch, original_dispatch)
        self.assertIs(native.cache_render.render_preview, original_preview)

    def test_zero_preview_still_delivers_full_managed_buffer(self):
        self.invoke('trace_open', sessions=[str(self.source)]).require_body()
        response = self.invoke('trace_read', targets=['S/e1'], pair=False, preview_chars=0)
        self.assertRegex(response.text, r'^Result R\d+ · buffer buf_\w+\n$')
        self.assertNotIn(self.source_text, response.text)
        envelope = response.require_body()
        self.assertEqual(envelope['preview_chars'], 0)
        content = self.state.buffer_manager.resolve_for_read(self.state, response.buffer_id).text
        self.assertEqual(content, envelope['text'])
        self.assertIn(self.source_text, content)
        self.assertEqual(metrics('trace_read', response.text)['response_chars'], len(response.text))

    def test_probe_restores_patches_on_unexpected_exception(self):
        from azo_session_postmortem import native
        original_dispatch = native.cache.dispatch
        original_preview = native.cache_render.render_preview
        with self.assertRaises(AttributeError):
            self.invoke('not_a_native_tool')
        self.assertIs(native.cache.dispatch, original_dispatch)
        self.assertIs(native.cache_render.render_preview, original_preview)

    def test_retrieval_requires_manager_but_edit_does_not(self):
        from agent_utils import State
        from scripts.native_probe import invoke
        self.invoke('trace_open', sessions=[str(self.source)]).require_body()
        state = State()
        state.session_cwd, state.tool_schemas = str(self.root), {}
        failure = invoke(state, 'trace_open', dict(workspace='absent'))
        self.assertTrue(failure.error)
        self.assertFalse((self.root / 'absent').exists())
        edited = invoke(state, 'trace_edit', dict(workspace='study', put=[
            dict(id='note', kind='note', title='Cache edit')]))
        envelope = edited.require_body()
        self.assertEqual(envelope['document']['operation'], 'trace_edit')
        self.assertIn('ack', envelope['document'])
        self.assertNotIn('text', envelope)
        self.assertEqual(edited.buffer_id, '')

    def test_cache_workspace_checker_reports_real_native_delivery(self):
        from scripts.check_workspace import main
        self.invoke('trace_open', sessions=[str(self.source)]).require_body()
        output = io.StringIO()
        with patch('sys.argv', ['checker', str(self.root / 'study')]), contextlib.redirect_stdout(output):
            main()
        report = json.loads(output.getvalue())
        self.assertEqual(report['comparison'], 'cache_native_workspace')
        self.assertTrue(report['native_managed_buffer'])
        self.assertTrue(report['map_unchanged'])
        self.assertGreater(report['native_response_chars'], 0)
        self.assertGreater(report['buffer_chars'], 0)
        self.assertEqual(report['calls'], 3)


if __name__ == '__main__':
    unittest.main()
