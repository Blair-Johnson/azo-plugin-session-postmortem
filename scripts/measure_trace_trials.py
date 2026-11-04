#!/usr/bin/env python3
"""Offline saved-native accounting; copies the index before public query/read calls.
Characters are Unicode len(), not tokens/bytes; inline delivery is not proof of reading.
Read text_chars includes map cards; inline_source_chars excludes them. Buffer exports
are not observed inline. JSON output contains refs/metrics only, never source bodies.

Optional metadata metrics (successful, recognized responses only):
* search_scope_summary_{known,unknown}_pages: scope_summary is/is not a dict.
* search_scope_include_references_{true,false,unknown}_pages: effective response
  policy, never inferred from arguments, defaults, saved names, or cursor calls.
* search_scope_{event,object,field}_count_page_total: sum of nonnegative integer
  scope_summary counts across response pages, NOT hits or unique source counts.
  Each has matching *_known_pages / *_unknown_pages coverage counters. Map-only
  null counts, absent legacy summaries, and invalid values are unknown, not zero.
* read_navigation_{known,unknown}_pages: session_navigation is/is not a list.
  read_navigation_pages_with_rows counts nonempty lists; *_visible_rows counts
  list entries. *_reported_rows_page_total sums valid session_navigation_count
  values independently, with *_reported_rows_{known,unknown}_pages coverage.
* read_navigation_{indexed,missing,ambiguous}_rows: visible entries with indexed
  true, indexed false, and ambiguous true respectively (ambiguity overlaps the
  other categories). Missing means destination not indexed, not missing evidence.
  *_unknown_{indexed,ambiguous}_rows and *_malformed_rows retain invalid coverage.
Navigation counts are exposure, not unique associations or evidence of use.
Repeated response pages contribute again; duplicate links to the same result ref
are still counted once. Unknown metadata has no numeric total contribution.
Per-call include_references/output are sanitized explicit arguments (null means
absent/invalid), distinct from effective response policy. include_references_{true,
false} counts explicit trace_search requests; output_compact counts explicit
trace_read requests, even on errors or missing results.
No metric implies benefit, graph-following, or reading a delivered body.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import shutil
import sys

TOOLS = ('trace_open', 'trace_query', 'trace_search', 'trace_read', 'trace_edit', 'trace_cooccur')


def search_scope_metrics(body):
    m = Counter()
    summary = body.get('scope_summary')
    known = isinstance(summary, dict)
    m['search_scope_summary_' + ('known' if known else 'unknown') + '_pages'] = 1
    summary = summary if known else {}
    policy = summary.get('include_references')
    label = str(policy).lower() if type(policy) is bool else 'unknown'
    m['search_scope_include_references_' + label + '_pages'] = 1
    for field in ('event_count', 'object_count', 'field_count'):
        value = summary.get(field)
        prefix = 'search_scope_' + field
        if type(value) is int and value >= 0:
            m[prefix + '_page_total'] = value
            m[prefix + '_known_pages'] = 1
        else:
            m[prefix + '_unknown_pages'] = 1
    return m


def navigation_metrics(body):
    m = Counter()
    count = body.get('session_navigation_count')
    if type(count) is int and count >= 0:
        m['read_navigation_reported_rows_page_total'] = count
        m['read_navigation_reported_rows_known_pages'] = 1
    else:
        m['read_navigation_reported_rows_unknown_pages'] = 1
    rows = body.get('session_navigation')
    if not isinstance(rows, list):
        m['read_navigation_unknown_pages'] = 1
        return m
    m.update(read_navigation_known_pages=1,
             read_navigation_pages_with_rows=int(bool(rows)),
             read_navigation_visible_rows=len(rows),
             read_navigation_indexed_rows=0, read_navigation_missing_rows=0,
             read_navigation_ambiguous_rows=0)
    for row in rows:
        if not isinstance(row, dict):
            m['read_navigation_malformed_rows'] += 1
            row = {}
        indexed = row.get('indexed')
        if type(indexed) is bool:
            m['read_navigation_' + ('indexed' if indexed else 'missing') + '_rows'] += 1
        else:
            m['read_navigation_unknown_indexed_rows'] += 1
        ambiguous = row.get('ambiguous')
        if type(ambiguous) is bool:
            m['read_navigation_ambiguous_rows'] += int(ambiguous)
        else:
            m['read_navigation_unknown_ambiguous_rows'] += 1
    return m


def metrics(tool, text):
    m = Counter(results=1, response_chars=len(text))
    if text.lstrip().startswith('ERROR:'):
        return m + Counter(errors=1)
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return m + Counter(malformed=1)
    if not isinstance(body, dict):
        return m + Counter(unknown=1)
    if body.get('error') or body.get('isError') is True:
        return m + Counter(errors=1)
    if tool != 'trace_read':
        keys = {'trace_open': 'sources', 'trace_query': 'rows', 'trace_search': 'hits',
                'trace_edit': 'created', 'trace_cooccur': 'rows'}
        m += Counter(unknown=int(keys[tool] not in body))
        if tool == 'trace_search' and 'hits' in body:
            m.update(search_scope_metrics(body))
        return m
    if 'plan_result' in body:  # Costs are metadata, not delivered source.
        return m + Counter(inspection_results=1)
    buffered = 'buffer' in body or 'buffer_path' in body
    if not buffered and not isinstance(body.get('blocks'), list):
        return m + Counter(unknown=1)
    m.update(navigation_metrics(body))
    for field in ('text_chars', 'packet_chars'):
        value = body.get(field)
        if type(value) is int and value >= 0:
            m['read_' + field] = value
        else:
            m['unknown_' + field] += 1
    if buffered:
        m['buffer_exports'] = 1
        m['buffer_export_text_chars'] = m['read_text_chars']
    else:
        for block in body['blocks']:
            if not isinstance(block, dict):
                m['malformed_blocks'] += 1
            elif isinstance(block.get('anchor'), dict) and ('event' in block['anchor'] or 'object' in block['anchor']):
                if isinstance(block.get('text'), str):
                    m['inline_source_chars'] += len(block['text'])
                elif block.get('available') is not False:
                    m['unknown_source_blocks'] += 1
    return m


def account(rows, fetch):
    calls, seen, sessions = {}, set(), defaultdict(lambda: defaultdict(Counter))
    for row in rows:
        ref, sid, tool, result = (row[k] for k in ('ref', 'session', 'tool', 'result_ref'))
        total = sessions[sid][tool]
        if ref not in calls:
            try:
                args = json.loads(row['arguments'])
                if not isinstance(args, dict):
                    raise ValueError('arguments must be an object')
            except (ValueError, TypeError):
                args = {}
                total['malformed_arguments'] += 1
            inspect = args.get('inspect') is True
            include = args.get('include_references')
            include = include if type(include) is bool else None
            output = args.get('output')
            output = output if output in ('inline', 'compact', 'buffer') else None
            calls[ref] = dict(ref=ref, session=sid, tool=tool, inspect=inspect,
                              include_references=include, output=output, results=[])
            total.update(calls=1, inspect_true=int(inspect))
            if tool == 'trace_search' and include is not None:
                total['include_references_' + str(include).lower()] += 1
            if tool == 'trace_read' and output == 'compact':
                total['output_compact'] += 1
        if result is None:
            total['missing_results'] += 1
        elif result not in seen:
            seen.add(result)
            measured = metrics(tool, fetch(result))
            total.update(measured)
            calls[ref]['results'].append(dict(ref=result, **measured))
        else:
            total['duplicate_result_links'] += 1
            calls[ref]['results'].append(dict(ref=result, counted=False))
    return {'sessions': {s: {'totals': dict(sum(tools.values(), Counter())), 'by_tool': dict(tools)} for s, tools in sessions.items()}, 'calls': list(calls.values())}


def collect(workspace):
    from azo_session_postmortem.engine import dispatch
    def pages(tool, **args):
        while True:
            page = dispatch(tool, dict(workspace=str(workspace), **args))
            yield page
            if not page.get('next_cursor'):
                break
            args = dict(cursor=page['next_cursor'], max_chars=1000000)
    def fetch(ref):
        chunks = []
        for page in pages('trace_read', targets=[{'event': ref, 'field': 'text'}], pair=False, line_context=0, max_chars=1000000):
            if page.get('unavailable_count'):
                raise ValueError('Unavailable saved result: ' + ref)
            chunks.extend(b['text'] for b in page['blocks'])
        return ''.join(chunks)
    names = ','.join(repr(t) for t in TOOLS)
    sql = f"SELECT c.ref,c.session,c.tool,c.arguments,l.dst AS result_ref FROM events c LEFT JOIN links l ON l.src=c.ref AND l.kind='call_result' WHERE c.kind='call' AND c.tool IN ({names}) ORDER BY c.session,c.seq,l.dst"
    rows = (r for p in pages('trace_query', sql=sql, page_size=100, max_chars=1000000) for r in p['rows'])
    return account(rows, fetch)


def self_test():
    read = lambda b: metrics('trace_read', json.dumps(b))
    assert metrics('trace_query', '{')['malformed'] == 1
    assert metrics('trace_query', '[]')['unknown'] == 1
    assert metrics('trace_query', 'ERROR: failure')['errors'] == 1
    assert read({'plan_result': 'r1', 'known_text_chars': 900, 'rows': [{'text_chars': 900}]})['read_text_chars'] == 0
    assert read({'buffer': 'buf_x', 'text_chars': 20, 'packet_chars': 200})['inline_source_chars'] == 0
    assert read({'buffer': 'buf_x', 'text_chars': 20, 'packet_chars': 200})['buffer_export_text_chars'] == 20
    m = read({'text_chars': 20, 'packet_chars': 200, 'blocks': [{'anchor': {'event': 's/e1'}, 'text': 'ERROR:'}, {'entry_id': 'map', 'text': 'card'}]})
    assert (m['inline_source_chars'], m['read_text_chars'], m['read_packet_chars'], m['errors']) == (6, 20, 200, 0)
    assert read({'blocks': [None], 'text_chars': 'bad'})['malformed_blocks'] == 1
    row = dict(ref='s/e1', session='s', tool='trace_read', arguments='{"inspect":true}', result_ref='s/e2')
    result = account([row, row], lambda _: '{')['sessions']['s']['totals']
    assert (result['calls'], result['inspect_true'], result['results'], result['malformed'], result['duplicate_result_links']) == (1, 1, 1, 1, 1)
    print(json.dumps({'self_test': 'passed'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', nargs='?', type=Path)
    parser.add_argument('--work-dir', type=Path, help='New directory for a disposable index copy (required for measurement)')
    parser.add_argument('--output', type=Path, help='Optional per-call JSON (refs and metrics, no bodies)')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        if not args.workspace or not args.work_dir:
            parser.error('workspace and --work-dir are required')
        args.work_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(args.workspace / 'trace.sqlite3', args.work_dir / 'trace.sqlite3')
        if (args.workspace / 'trace.sqlite3-wal').exists():
            parser.error('WAL present; supply a quiescent, checkpointed index copy')
        result = collect(args.work_dir)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'sessions': result['sessions'], 'python': sys.executable}))
