"""Host surface for scope control, field diagnostics and recorded child navigation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HOST = importlib.util.find_spec('agent_utils') is not None
if HOST:
    from agent_utils import State, ToolCall
    from agent_utils.files.buffer_manager import BufferManager
    from azo_session_postmortem import engine
    import test_native as native_fixtures
    from azo_session_postmortem import native

CHILD = '11111111-2222-4333-8444-555555555555'
LAUNCH = (f'Submitted RLM task rlm_job_123 with profile simple (Simple).\n'
          f'Child live session: {CHILD}\nTask name (/attach label): worker\n'
          'Working directory: /not-read\nInput buffers:\n- buf_bbbbbb [ready]\n'
          'Output buffers reserved:\n- answer -> buf_aaaaaa [reserved]\n'
          "Use rlm_status(job_id='rlm_job_123') to inspect it or "
          "cancel_rlm(job_id='rlm_job_123') to kill it.")

@unittest.skipUnless(HOST, 'run in the Agent Zoo host environment')
class NavigationNativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = State()
        self.state.session_cwd = str(self.root)
        self.state.tool_schemas = {}
        self.state.buffer_manager = BufferManager()
        self.source = self.root / 'source.json'
        self.source.write_text(json.dumps({'meta': {'session_id': 'P'}, 'events': [
            {'kind': 'result', 'role': 'tool', 'tool': 'submit_rlm', 'text': LAUNCH},
            {'kind': 'message', 'role': 'assistant', 'text': 'Read handoff buf_aaaaaa.'}],
            'components': {'buffer_manager': {'buffers': {
                'buf_aaaaaa': {'text': 'object_only_marker'}}}}}))
        self.invoke('trace_open', sessions=[str(self.source)], descendants=False)

    def invoke(self, name, error=False, **args):
        return native_fixtures.NativeTests.invoke(self, name, expect_error=error, **args)

    def test_search_false_default_and_explicit_object(self):
        args = dict(scope={'refs': ['P/e2']}, query='object_only_marker', mode='literal')
        self.assertEqual(len(self.invoke('trace_search', **args)['blocks']), 1)
        narrow = self.invoke('trace_search', **args, follow_object_links=False, save_as='narrow')
        self.assertEqual(narrow['blocks'], [])
        self.assertEqual(self.invoke('trace_search', saved='narrow')['blocks'], [])
        direct = self.invoke('trace_search', scope={'refs': [{'object': 'P/o_buf_aaaaaa'}]},
                             query='object_only_marker', mode='literal', follow_object_links=False)
        self.assertEqual(len(direct['blocks']), 1)

    def test_native_null_omitted_false_preserved(self):
        with patch.object(native.cache, 'dispatch', wraps=native.cache.dispatch) as dispatch:
            self.invoke('trace_search', query='x', follow_object_links=None)
        self.assertNotIn('follow_object_links', dispatch.call_args.args[1])
        with patch.object(native.cache, 'dispatch', wraps=native.cache.dispatch) as dispatch:
            self.invoke('trace_search', query='x', follow_object_links=False)
        self.assertIs(dispatch.call_args.args[1]['follow_object_links'], False)

    def test_native_schema_renames_flag_and_keeps_saved_engine_recipes(self):
        props = native.trace_search.schema['function']['parameters']['properties']
        self.assertIn('follow_object_links', props)
        self.assertNotIn('include_references', props)
        self.assertEqual(props['follow_object_links']['type'], ['boolean', 'null'])
        legacy = engine.dispatch('trace_search', {
            'workspace': str(self.root / 'study'), 'scope': {'refs': ['P/e2']},
            'query': 'object_only_marker', 'mode': 'literal',
            'include_references': False, 'save_as': 'legacy-engine-recipe',
        })
        saved = self.invoke('trace_search', saved='legacy-engine-recipe', follow_object_links=None)
        self.assertEqual(legacy['matched_total']['source'], 0)
        self.assertEqual(saved['blocks'], [])
        self.invoke('trace_search', saved='legacy-engine-recipe', follow_object_links=True, error=True)
        found = self.invoke('trace_search', query='.', mode='regex')
        self.assertGreater(len(found['blocks']), 1)
        self.invoke('trace_search', cursor='c1', error=True)

    def test_handoff_navigation_and_queryable_evidence(self):
        read = self.invoke('trace_read', targets=[{'event': 'P/e2', 'field': 'text'}],
                           pair=False, line_context=0)
        self.assertIn(CHILD, self.last_text)
        self.assertTrue(any(b['group'] == 'navigation' for b in read['blocks']))
        evidence = self.invoke('trace_read', targets=[{'event': 'P/e1', 'field': 'text'}],
                               pair=False, line_context=0)
        self.assertIn(CHILD, ''.join(b.get('text', '') for b in evidence['blocks']))
        rows = self.invoke('trace_query', sql="SELECT src,dst,kind,anchor FROM session_links WHERE kind='rlm_output'")
        self.assertIn(CHILD, rows['blocks'][0]['text'])
        self.assertEqual(self.invoke('trace_open')['counts']['sessions'], 1)

    def test_bad_field_identifies_ref_and_available_field(self):
        error = self.invoke('trace_read', error=True,
                            targets=[{'event': 'P/e1', 'field': 'text'},
                                     {'event': 'P/e2', 'field': 'arguments'}], pair=False)
        for text in ('P/e2', 'arguments', 'text'):
            self.assertIn(text, error)
        self.assertFalse((self.root / 'study' / 'packets').exists())

if __name__ == '__main__':
    unittest.main()
