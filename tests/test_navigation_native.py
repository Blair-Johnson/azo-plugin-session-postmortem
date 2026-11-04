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
        self.source = self.root / 'source.json'
        self.source.write_text(json.dumps({'meta': {'session_id': 'P'}, 'events': [
            {'kind': 'result', 'role': 'tool', 'tool': 'submit_rlm', 'text': LAUNCH},
            {'kind': 'message', 'role': 'assistant', 'text': 'Read handoff buf_aaaaaa.'}],
            'components': {'buffer_manager': {'buffers': {
                'buf_aaaaaa': {'text': 'object_only_marker'}}}}}))
        self.invoke('trace_open', sessions=[str(self.source)], descendants=False)

    def invoke(self, name, error=False, **args):
        args = {'workspace': 'study', **args}
        call = ToolCall(id='navigation', name=name, raw_args=json.dumps(args), parsed_args=args)
        self.state.pending_tool_calls = [call]
        getattr(native, name)(self.state)
        self.assertEqual(call.error, error, call.result)
        return call.result if error else json.loads(call.result)

    def test_search_false_default_and_explicit_object(self):
        args = dict(scope={'refs': ['P/e2']}, query='object_only_marker', mode='literal')
        self.assertEqual(self.invoke('trace_search', **args)['matched_total']['source'], 1)
        narrow = self.invoke('trace_search', **args, include_references=False, save_as='narrow')
        self.assertEqual(narrow['matched_total']['source'], 0)
        self.assertFalse(narrow['scope_summary']['include_references'])
        self.assertEqual(self.invoke('trace_search', saved='narrow')['matched_total']['source'], 0)
        direct = self.invoke('trace_search', scope={'refs': [{'object': 'P/o_buf_aaaaaa'}]},
                             query='object_only_marker', mode='literal', include_references=False)
        self.assertEqual(direct['matched_total']['source'], 1)

    def test_native_null_omitted_false_preserved(self):
        with patch.object(native.engine, 'dispatch', return_value={}) as dispatch:
            self.invoke('trace_search', query='x', include_references=None)
        self.assertNotIn('include_references', dispatch.call_args.args[1])
        with patch.object(native.engine, 'dispatch', return_value={}) as dispatch:
            self.invoke('trace_search', query='x', include_references=False)
        self.assertIs(dispatch.call_args.args[1]['include_references'], False)

    def test_handoff_navigation_and_queryable_evidence(self):
        read = self.invoke('trace_read', targets=[{'event': 'P/e2', 'field': 'text'}],
                           pair=False, line_context=0, output='compact')
        links = [r for r in read['session_navigation'] if r['dst'] == CHILD]
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]['indexed'])
        self.assertEqual(links[0]['anchor']['event'], 'P/e1')
        evidence = self.invoke('trace_read', targets=[links[0]['anchor']], pair=False, line_context=0)
        self.assertIn(CHILD, ''.join(b.get('text', '') for b in evidence['blocks']))
        rows = self.invoke('trace_query', sql="SELECT src,dst,kind,anchor FROM session_links WHERE kind='rlm_output'")
        self.assertEqual(rows['rows'][0]['dst'], CHILD)
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
