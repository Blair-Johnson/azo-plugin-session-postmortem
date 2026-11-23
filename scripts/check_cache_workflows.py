"""Synthetic real-native cache workflows; not a fresh-investigator efficacy test."""
import json
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from agent_utils.core.events import RunToolCall
    from agent_utils.core.state import State, ToolCall
    from agent_utils.features.buffers.manager import BufferManager
    from azo_session_postmortem import native

    with tempfile.TemporaryDirectory(prefix='postmortem-cache-workflow-') as temp:
        root = Path(temp)
        source = root / 'source.json'
        bodies = ['Keep retries disabled.', 'Investigating the timeout.',
                  'Enable retries after the timeout fix.']
        bodies += [f'TypeError sample{i:03d}: StringAttr conversion failed' for i in range(80)]
        source.write_text(json.dumps({'format_version': 2, 'meta': {'session_id': 'S'},
            'entries': [{'index': i, 'step': i, 'messages': [
                {'role': 'user' if i in (0, 2) else 'assistant', 'content': body}]}
                for i, body in enumerate(bodies)],
            'trace_objects': [{'ref': 'S/o-log', 'session': 'S', 'kind': 'buffer',
                'available': True, 'text': '\n'.join(f'log line {i}' for i in range(40000))}]}))
        state = State(cwd=str(root))
        state.tool_schemas = {}
        state.buffer_manager = BufferManager()
        calls = chars = 0

        def call(name, **args):
            nonlocal calls, chars
            args = {'workspace': 'study', **args}
            pending = ToolCall(id=f'cache-smoke-{calls}', name=name,
                               raw_args=json.dumps(args), parsed_args=args)
            state.pending_tool_calls = [pending]
            getattr(native, name).handle(RunToolCall(pending), state, lambda _ev: None)
            assert not pending.error, pending.result
            calls += 1
            chars += len(pending.result)
            handles = re.findall(r'\bbuf_[A-Za-z0-9]+\b', pending.result)
            text = state.buffer_manager.resolve_for_read(state, handles[0]).text if handles else ''
            return pending.result, text

        call('trace_open', sessions=[str(source)], descendants=False)
        preview, complete = call('trace_search', query='TypeError', mode='literal', preview_chars=900)
        assert 'TypeError' in preview and 'sample079' in complete
        assert len(re.findall(r'^## S\d+-H\d+', complete, re.M)) >= 80
        hit = re.search(r'^## (S\d+-H\d+)\s*$', complete, re.M).group(1)
        _, selected = call('trace_read', targets=[hit], pair=False, line_context=0)
        assert 'TypeError sample' in selected
        compared, full_read = call('trace_read', targets=['S/e1', 'S/o-log', 'S/e3'],
                                  pair=False, line_context=0, preview_chars=1800)
        assert bodies[0] in compared and bodies[2] in compared
        assert 'log line 39999' in full_read and len(full_read.splitlines()) > 40000
        seed_text, _ = call('trace_query', sql="SELECT ref FROM events WHERE seq IN (1,3) ORDER BY seq")
        witness_text, _ = call('trace_query', sql="SELECT ref FROM events WHERE seq=3")
        seed = re.search(r'\bQ\d+\b', seed_text).group(0)
        witness = re.search(r'\bQ\d+\b', witness_text).group(0)
        joined, full_join = call('trace_cooccur', seed={'result': seed}, against=[
            {'name': 'later', 'selection': {'result': witness}, 'before': 0, 'after': 0}])
        assert re.search(r'\bC\d+-M\d+\b', joined) and re.search(r'\bC\d+-U\d+\b', joined)
        assert re.search(r'^## C\d+-W\d+', full_join, re.M)
        call('trace_edit', put=[{'id': 'continue', 'kind': 'note', 'title': 'Continue',
                                'note': 'Inspect the conversion repair.', 'pinned': True,
                                'anchors': [hit]}])
        state.buffer_manager = BufferManager()
        resumed, full_open = call('trace_open')
        assert 'Inspect the conversion repair.' in resumed
        assert 'results' in full_open
        _, recovered = call('trace_read', targets=[hit], pair=False, line_context=0)
        assert 'TypeError sample' in recovered
        print(json.dumps({'status': 'passed', 'synthetic_only': True, 'native_calls': calls,
            'response_chars': chars, 'all_80_hits_buffered': True,
            'late_target_visible': True, 'full_40000_line_log': True,
            'matched_and_unmatched_visible': True, 'aliases_survive_reopen': True,
            'interpretation': 'Interface regression, not investigator efficacy.'}))


if __name__ == '__main__':
    main()
