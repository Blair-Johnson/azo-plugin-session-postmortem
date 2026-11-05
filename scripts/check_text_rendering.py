"""Render frozen CTF response dictionaries without touching their workspaces."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from azo_session_postmortem.render import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('index', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    path = (args.index / 'trace.sqlite3').resolve()
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT e.ref,e.tool,c.text,caller.arguments FROM events e
        JOIN _content c ON c.ref=e.ref AND c.field='text'
        JOIN links l ON l.dst=e.ref AND l.kind='call_result'
        JOIN events caller ON caller.ref=l.src
        WHERE e.tool LIKE 'trace_%' AND e.kind='result'
        ORDER BY e.session,e.seq""").fetchall()
    db.close()
    samples, totals, seen = [], {}, set()
    args.output.mkdir(parents=True, exist_ok=False)
    for row in rows:
        if row['ref'] in seen:
            continue
        seen.add(row['ref'])
        try:
            result = json.loads(row['text'])
            request = json.loads(row['arguments'])
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict) or not isinstance(request, dict):
            continue
        if row['tool'] == 'trace_query' and request.get('cursor'):
            continue  # Historical pages lack positional offsets; don't invent handles.
        if row['tool'] == 'trace_query':
            result.setdefault('page_offset', 0)
        before = copy.deepcopy(result)
        text = render(row['tool'], result)
        assert result == before, 'Renderer mutated frozen response'
        assert isinstance(text, str) and text.strip()
        for block in result.get('blocks', []):
            if 'text' in block:
                assert block['text'] in text, (row['ref'], 'source text changed')
        for group in result.get('hits', {}).values():
            for hit in group:
                assert hit.get('excerpt', '') in text, (row['ref'], 'excerpt changed')
        for key in ('next_cursor', 'plan_cursor', 'unmatched_cursor',
                    'session_navigation_cursor', 'references_cursor'):
            if result.get(key):
                assert result[key] in text, (row['ref'], 'lost continuation', key)
        counts = totals.setdefault(row['tool'], dict(responses=0, json_chars=0, text_chars=0))
        counts['responses'] += 1
        counts['json_chars'] += len(row['text'])
        counts['text_chars'] += len(text)
        if not any(s['tool'] == row['tool'] for s in samples):
            filename = row['tool'] + '.txt'
            (args.output / filename).write_text(text, encoding='utf-8', newline='')
            samples.append(dict(tool=row['tool'], ref=row['ref'], example=filename,
                                json_chars=len(row['text']), text_chars=len(text)))
    assert totals, 'No recorded tool results found'
    report = dict(source_index_read_only=True, source_passages_preserved=True,
                  frozen_initial_query_pages_only=True, totals=totals, samples=samples,
                  interpretation='Rendering-size comparison, not investigator efficacy or token counts.')
    (args.output / 'comparison.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
