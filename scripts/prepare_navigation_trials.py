"""Prepare clean matched repeats of round4 without modifying original investigations."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from prepare_cooccur_trials import ANALYSIS_TABLES, fingerprint, readonly


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('previous', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output).free < 4 * 1024**3:
        raise RuntimeError('At least 4 GiB free required before trial copies')
    manifest = json.loads((args.previous / 'prepared_sources.json').read_text())
    destinations = [args.output / name for name in ('G-new', 'H-new', 'live-smoke')]
    if any(path.exists() for path in destinations):
        raise RuntimeError('Refusing to overwrite existing trial workspaces')
    report = {'originals_read_only': True, 'cases': {}, 'python': sys.executable}
    for case in ('G', 'H'):
        original = args.previous / f'{case}-source' / 'trace.sqlite3'
        destination = args.output / f'{case}-new'
        destination.mkdir()
        path = destination / 'trace.sqlite3'
        with readonly(original) as source:
            before = fingerprint(source)
            expected = manifest['cases'][case]
            assert before['sha256'] == expected['sha256']
            assert before['counts'] == expected['counts']
            with sqlite3.connect(path) as target:
                source.backup(target)
                target.execute('PRAGMA secure_delete=ON')
                for table in ANALYSIS_TABLES:
                    target.execute('DELETE FROM ' + table)
                target.execute('DROP TABLE IF EXISTS session_links')
                target.execute('DROP TABLE IF EXISTS _session_links_meta')
                target.commit()
                target.execute('VACUUM')
                assert fingerprint(target) == before
                assert all(target.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
                           for table in ANALYSIS_TABLES)
            assert fingerprint(source) == before
        report['cases'][case] = dict(before, original=str(original), workspace=str(destination),
                                     empty_analysis_tables=list(ANALYSIS_TABLES))
    smoke = args.output / 'live-smoke'
    smoke.mkdir()
    shutil.copyfile(args.output / 'G-new/trace.sqlite3', smoke / 'trace.sqlite3')
    report['smoke_workspace'] = str(smoke)
    (args.output / 'prepared_sources.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
