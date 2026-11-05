"""Agent-facing regressions: useful first response, not a metadata envelope."""
import unittest

from azo_session_postmortem.cache_render import render_full, render_preview


def document(operation='trace_search', blocks=None):
    return {'id': 'S17', 'operation': operation, 'workspace': '/private/study',
            'title': '9 matches', 'blocks': blocks or [],
            'counts': {'uninteresting_internal_count': 123}}


def block(index, text, group='hit'):
    return {'id': f'S17-H{index:03d}', 'title': f'S/e{index}:text | tool result',
            'text': text, 'group': group, 'requested': True}


class CachePresentationContractTests(unittest.TestCase):
    def test_zero_preview_retains_result_and_buffer_pointer(self):
        response = render_preview(document(blocks=[block(1, 'BODY')]), 'buf_a1', 0)
        self.assertIn('S17', response)
        self.assertIn('buf_a1', response)
        self.assertNotIn('BODY', response)

    def test_edit_ack_is_short_text_not_json(self):
        response = render_preview({'operation': 'trace_edit', 'ack': {
            'created': [], 'updated': ['finding'], 'deleted': [],
            'linked': [], 'unlinked': []}})
        self.assertIn('Updated: finding', response)
        self.assertNotIn('{', response)
        self.assertNotIn('created', response)

    def test_small_result_answers_without_rendering_workspace_catalog(self):
        response = render_preview(document(blocks=[block(1, 'The conversion failed.')]), 'buf_a1')
        self.assertIn('The conversion failed.', response)
        self.assertIn('S17-H001', response)
        self.assertNotIn('/private/study', response)
        self.assertNotIn('## INDEX', response)
        self.assertNotIn('uninteresting_internal_count', response)

    def test_complete_index_has_discriminating_source_excerpts(self):
        result = document(blocks=[block(1, 'MATCH_CONTEXT_SENTINEL\nrest of source')])
        complete = render_full(result)
        index = complete.split('## INDEX', 1)[1].split('## S17-H001', 1)[0]
        self.assertIn('MATCH_CONTEXT_SENTINEL', index)

    def test_additional_inline_index_contains_excerpt_not_only_address(self):
        blocks = [block(i, 'earlier context ' * 300) for i in range(1, 7)]
        blocks += [block(7, 'LATER_DECISIVE_SUCCESS')]
        response = render_preview(document(blocks=blocks), 'buf_a1', 2600)
        self.assertIn('S17-H007', response)
        self.assertIn('LATER_DECISIVE_SUCCESS', response)
        self.assertLessEqual(len(response), 2600)


    def test_search_preview_and_index_use_match_not_large_leading_context(self):
        hit = block(1, 'unrelated ' * 4000 + '\nDECISIVE_MATCH: repaired\n')
        hit['excerpt'] = 'DECISIVE_MATCH: repaired'
        result = document(blocks=[hit])
        response = render_preview(result, 'buf_a1', 900)
        complete = render_full(result)
        index = complete.split('## INDEX', 1)[1].split('## S17-H001', 1)[0]
        self.assertIn('DECISIVE_MATCH', response)
        self.assertIn('DECISIVE_MATCH', index)
        self.assertIn(hit['text'], complete)
        self.assertLessEqual(len(response), 900)

    def test_read_preview_reserves_space_across_original_target_groups(self):
        blocks = [dict(block(i, f'Range event {i}', 'evidence'), request_group=0)
                  for i in range(1, 101)]
        blocks.append(dict(block(140, 'LATER_SEPARATE_REQUEST', 'evidence'), request_group=1))
        response = render_preview(document('trace_read', blocks), 'buf_a1', 1200)
        self.assertIn('Range event 1', response)
        self.assertIn('LATER_SEPARATE_REQUEST', response)
        self.assertLessEqual(len(response), 1200)

    def test_sql_membership_pointer_is_available_without_inline_clutter(self):
        result = document(blocks=[block(1, 'useful evidence')])
        result['sql_result'] = 'r81'
        self.assertIn('SQL membership: r81', render_full(result))
        self.assertNotIn('SQL membership', render_preview(result, 'buf_a1'))

if __name__ == '__main__':
    unittest.main()
