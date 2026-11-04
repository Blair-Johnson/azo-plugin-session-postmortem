"""Prepare clean, identical trial indexes; never modify original investigations."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

SOURCE_TABLES = {
    'sessions': 'id', 'events': 'ref', 'objects': 'ref',
    'links': 'src,dst,kind', '_content': 'ref,field', '_sources': 'source',
}
ANALYSIS_TABLES = ('map_links', 'map_members', 'map_entries', 'result_items',
                   'saved_queries', '_results', '_cursors')
CASES = {'G': ('a', 58, 15688), 'H': ('discovery', 35, 12102)}


def fingerprint(db):
    digest, counts = hashlib.sha256(), {}
    for table, order in SOURCE_TABLES.items():
        digest.update(table.encode())
        counts[table] = 0
        for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            data = json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode()
            digest.update(len(data).to_bytes(8, 'big'))
            digest.update(data)
            counts[table] += 1
    return {'sha256': digest.hexdigest(), 'counts': counts}


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('previous', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output).free < 4 * 1024**3:
        raise RuntimeError('At least 4 GiB free required before making trial copies')
    targets = [args.output / f'{task}-{name}' for task in CASES
               for name in ('source', 'amber', 'indigo')]
    if any(target.exists() for target in targets):
        raise RuntimeError('Refusing to overwrite any existing trial workspace')
    report = {'originals_read_only': True, 'cases': {}}
    for task, (previous, sessions, events) in CASES.items():
        original = args.previous / previous / 'trace.sqlite3'
        baseline = args.output / f'{task}-source'
        baseline.mkdir()
        path = baseline / 'trace.sqlite3'
        with readonly(original) as source:
            expected = fingerprint(source)
            assert expected['counts']['sessions'] == sessions
            assert expected['counts']['events'] == events
            with sqlite3.connect(path) as target:
                source.backup(target)
                target.execute('PRAGMA secure_delete=ON')
                for table in ANALYSIS_TABLES:
                    target.execute('DELETE FROM ' + table)
                target.commit()
                target.execute('VACUUM')
                assert fingerprint(target) == expected
                assert all(target.execute('SELECT count(*) FROM ' + t).fetchone()[0] == 0
                           for t in ANALYSIS_TABLES)
        copies = []
        for arm in ('amber', 'indigo'):
            destination = args.output / f'{task}-{arm}'
            destination.mkdir()
            copied = destination / 'trace.sqlite3'
            shutil.copyfile(path, copied)
            with readonly(copied) as db:
                assert fingerprint(db) == expected
                assert all(db.execute('SELECT count(*) FROM ' + t).fetchone()[0] == 0
                           for t in ANALYSIS_TABLES)
            copies.append(str(destination))
        report['cases'][task] = dict(expected, source_index=str(original),
                                     frozen_index=str(baseline), copies=copies,
                                     empty_analysis_tables=list(ANALYSIS_TABLES))
    (args.output / 'prepared_sources.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
