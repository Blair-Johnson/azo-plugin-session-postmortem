"""Small persistent trace workspace. Source sessions are never modified.

SQL exposes the documented tables only; text lives in a private content table.
Cursors and selections are immutable snapshots persisted in trace.sqlite3.
"""
from bisect import bisect_right
import json
from pathlib import Path
import random
import re
import sqlite3

if __package__:
    from .navigation import ensure_session_links, navigation_rows
else:
    from navigation import ensure_session_links, navigation_rows


PUBLIC = {'sessions', 'events', 'objects', 'links', 'session_links', 'map_entries',
          'map_members', 'map_links', 'result_items', 'saved_queries'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,parent_id TEXT,title TEXT,source TEXT,warnings TEXT);
CREATE TABLE IF NOT EXISTS events(ref TEXT PRIMARY KEY,session TEXT,seq INTEGER,elapsed_s REAL,kind TEXT,role TEXT,tool TEXT,arguments TEXT,preview TEXT,system_generated INTEGER);
CREATE TABLE IF NOT EXISTS objects(ref TEXT PRIMARY KEY,kind TEXT,line_count INTEGER,char_count INTEGER,available INTEGER,session TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS links(src TEXT,dst TEXT,kind TEXT,UNIQUE(src,dst,kind));
CREATE TABLE IF NOT EXISTS _content(ref TEXT,field TEXT,text TEXT,PRIMARY KEY(ref,field));
CREATE TABLE IF NOT EXISTS map_entries(id TEXT PRIMARY KEY,kind TEXT,title TEXT,note TEXT,anchors TEXT,tags TEXT,fields TEXT,pinned INTEGER,details_path TEXT,revision INTEGER);
CREATE TABLE IF NOT EXISTS map_members(entry_id TEXT,ref TEXT,UNIQUE(entry_id,ref));
CREATE TABLE IF NOT EXISTS map_links(id TEXT PRIMARY KEY,src TEXT,dst TEXT,label TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS result_items(result_id TEXT,row INTEGER,ref TEXT,anchor TEXT,entry_id TEXT);
CREATE TABLE IF NOT EXISTS saved_queries(name TEXT PRIMARY KEY,tool TEXT,arguments TEXT,last_result TEXT);
CREATE TABLE IF NOT EXISTS _results(id INTEGER PRIMARY KEY,kind TEXT,rows TEXT,meta TEXT);
CREATE TABLE IF NOT EXISTS _cursors(id INTEGER PRIMARY KEY,kind TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS _sources(source TEXT PRIMARY KEY,descendants INTEGER);
'''


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _positive(value, name, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(name + ' must be ' + ('nonnegative' if zero else 'positive') + ' integer')
    return value


def _list(value, name):
    if not isinstance(value, list):
        raise ValueError(name + ' must be a list')
    return value


def _unique(values):
    return list({_json(v): v for v in values}.values())


def _row(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


# Whole-event content projection is deliberately an allowlist: replay, usage,
# pointers and flags remain addressable through explicit field anchors or "all".
CONTENT_FIELDS = frozenset({'text', 'arguments', 'raw_args', 'blocks',
                            'reasoning', 'reasoning_content', 'reasoning_text',
                            'reasoning_summary', 'thinking', 'thinking_content'})
READ_BLOCK_LIMIT = 64
READ_PREVIEW_LIMIT = 8


def _projection(value):
    if value not in ('content', 'all'):
        raise ValueError('projection must be content or all')
    return value


def _system_generated(fields):
    """Only explicit booleans count; absent/invalid metadata stays unknown."""
    value = fields.get('system_generated')
    metadata = fields.get('metadata')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if isinstance(value, bool):
        return int(value)
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = None
    if isinstance(metadata, dict) and isinstance(metadata.get('system_generated'), bool):
        return int(metadata['system_generated'])
    return None


def _ensure_system_generated(db):
    """One-column compatibility guard; never reload sources or reset workspace state."""
    if 'system_generated' in {r['name'] for r in db.execute('PRAGMA table_info(events)')}:
        return
    db.execute('ALTER TABLE events ADD COLUMN system_generated INTEGER')
    rows = _rows(db, 'SELECT e.ref, f.text AS system_generated, m.text AS metadata '
                    'FROM events e LEFT JOIN _content f '
                    'ON f.ref=e.ref AND f.field=? '
                    'LEFT JOIN _content m ON m.ref=e.ref AND m.field=?',
                    ('system_generated', 'metadata'))
    db.executemany('UPDATE events SET system_generated=? WHERE ref=?',
                   ((_system_generated(row), row['ref']) for row in rows))


def _card(db, entry):
    row = _row(db, 'SELECT * FROM map_entries WHERE id=?', (entry,))
    if row is None:
        raise ValueError('unknown map entry: ' + str(entry))
    for key in ('anchors', 'tags', 'fields'):
        row[key] = json.loads(row[key])
    row['pinned'] = bool(row['pinned'])
    row.pop('revision', None)
    return row


def _preview(card):
    return {k: card[k] for k in ('id', 'kind', 'title', 'pinned')} | {
        'note': card['note'][:240], 'anchor_count': len(card['anchors'])}


def _cursor(db, kind, payload):
    n = db.execute('INSERT INTO _cursors(kind,payload) VALUES(?,?)', (kind, _json(payload))).lastrowid
    return 'c' + str(n)


def _uncursor(db, token, kind):
    if not isinstance(token, str) or not re.fullmatch(r'c\d+', token):
        raise ValueError('invalid cursor')
    row = _row(db, 'SELECT * FROM _cursors WHERE id=?', (int(token[1:]),))
    if not row or row['kind'] != kind:
        raise ValueError('cursor does not belong to ' + kind)
    return json.loads(row['payload'])


def _get_result(db, result):
    if not isinstance(result, str) or not re.fullmatch(r'r\d+', result):
        raise ValueError('invalid result ID')
    row = _row(db, 'SELECT * FROM _results WHERE id=?', (int(result[1:]),))
    if not row:
        raise ValueError('unknown result: ' + result)
    return row['kind'], json.loads(row['rows']), json.loads(row['meta'])


def _row_targets(row):
    if row.get('entry_id') is not None:
        return [{'entry': row['entry_id'], 'part': 'card'}]
    anchors = row.get('_anchors')
    if anchors is not None:
        return anchors
    anchor = row.get('anchor')
    if anchor is not None:
        if isinstance(anchor, str):
            try:
                anchor = json.loads(anchor)
            except (ValueError, TypeError):
                pass
        return [anchor]
    if row.get('ref') is not None:
        return [row['ref']]
    if row.get('_entry_id') is not None:
        return [{'entry': row['_entry_id'], 'part': 'card'}]
    return []


def _store_result(db, kind, rows, meta):
    n = db.execute('INSERT INTO _results(kind,rows,meta) VALUES(?,?,?)',
                   (kind, _json(rows), _json(meta))).lastrowid
    result = 'r' + str(n)
    for i, row in enumerate(rows):
        for anchor in _row_targets(row):
            if isinstance(anchor, dict) and 'entry' in anchor:
                db.execute('INSERT INTO result_items VALUES(?,?,?,?,?)',
                           (result, i, None, None, anchor['entry']))
            else:
                ref = anchor if isinstance(anchor, str) else anchor.get('event', anchor.get('object'))
                db.execute('INSERT INTO result_items VALUES(?,?,?,?,?)',
                           (result, i, ref, _json(anchor), None))
    return result


def _save_query(db, name, tool, arguments, result):
    if name is not None:
        if not isinstance(name, str) or not name:
            raise ValueError('save_as must be a nonempty name')
        db.execute('INSERT OR REPLACE INTO saved_queries VALUES(?,?,?,?)',
                   (name, tool, _json(arguments), result))


def _saved(db, name, tool):
    row = _row(db, 'SELECT * FROM saved_queries WHERE name=?', (name,))
    if not row or row['tool'] != tool:
        raise ValueError('unknown saved ' + tool + ' query: ' + str(name))
    return json.loads(row['arguments'])


def _known(db, ref):
    return (db.execute('SELECT 1 FROM events WHERE ref=? UNION ALL SELECT 1 FROM objects WHERE ref=?',
                       (ref, ref)).fetchone() is not None or
            db.execute('SELECT 1 FROM links WHERE src=? OR dst=?', (ref, ref)).fetchone() is not None)


def _field_text(db, ref, field):
    row = _row(db, 'SELECT text FROM _content WHERE ref=? AND field=?', (ref, field))
    return None if row is None else row['text']


def _anchor(db, value):
    if isinstance(value, str):
        if not _known(db, value):
            raise ValueError('unknown source reference: ' + value)
        return value
    if not isinstance(value, dict):
        raise ValueError('source anchor must be a reference or address object')
    keys = set(value)
    if 'session' in value:
        if keys != {'session', 'events'}:
            raise ValueError('event range requires session and events only')
        lo, hi = _range(value['events'])
        count = db.execute('SELECT count(*) FROM events WHERE session=? AND seq BETWEEN ? AND ?',
                           (value['session'], lo, hi)).fetchone()[0]
        if count != hi - lo + 1:
            raise ValueError('event range contains unknown events')
        return value
    key = 'object' if 'object' in value else 'event' if 'event' in value else None
    if key is None or keys - ({key, 'lines'} | ({'field'} if key == 'event' else set())):
        raise ValueError('invalid source address')
    ref = value[key]
    table = 'objects' if key == 'object' else 'events'
    if not db.execute('SELECT 1 FROM ' + table + ' WHERE ref=?', (ref,)).fetchone():
        raise ValueError('unknown ' + key + ': ' + str(ref))
    field = value.get('field', 'text')
    text = _field_text(db, ref, field)
    if key == 'event' and text is None:
        fields = [r['field'] for r in _rows(db,
            'SELECT field FROM _content WHERE ref=? ORDER BY field', (ref,))]
        example = {'event': ref, 'field': fields[0]} if fields else ref
        raise ValueError(
            f'unknown event field: ref={ref!r}, requested field={field!r}; '
            f'available fields={_json(fields)}; valid target example: {_json(example)}')
    if 'lines' in value:
        lo, hi = _range(value['lines'])
        line_count = max(1, len(text.splitlines())) if text is not None else None
        if line_count is not None and hi > line_count:
            whole = {key: ref, **({'field': field} if key == 'event' else {})}
            raise ValueError(
                f'line range exceeds source field: ref={ref!r}, field={field!r}, '
                f'requested=[{lo}, {hi}], available line count={line_count}; '
                f'omit lines to read the whole field: {_json(whole)}')
    return value


def _range(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('range must be [first,last]')
    lo, hi = value
    _positive(lo, 'range start')
    _positive(hi, 'range end')
    if hi < lo:
        raise ValueError('range end precedes start')
    return lo, hi


def _resolve(db, targets, source_only=False, *, search_projection=None):
    out = []
    for target in _list(targets, 'targets'):
        if isinstance(target, dict) and 'result' in target:
            if set(target) - {'result', 'rows'}:
                raise ValueError('invalid result target')
            _, rows, meta = _get_result(db, target['result'])
            indexes = target.get('rows', list(range(len(rows))))
            _list(indexes, 'rows')
            found = []
            for i in indexes:
                _positive(i, 'row', zero=True)
                if i >= len(rows):
                    raise ValueError('result row out of range')
                selected = _row_targets(rows[i])
                if not selected:
                    raise ValueError('result contains non-navigable rows')
                found.extend(selected)
            resolved = _resolve(db, found, source_only, search_projection=search_projection)
            if search_projection is not None and meta.get('_search_no_implicit_objects'):
                # Cooccur stores original anchors for read/map/requery consumers.
                # Only search resolves whole events to existing explicit fields,
                # preventing _source_windows from following object references.
                for anchor in resolved:
                    if isinstance(anchor, str) and db.execute(
                            'SELECT 1 FROM events WHERE ref=?', (anchor,)).fetchone():
                        out.extend({'event': anchor, 'field': row['field']} for row in _rows(db,
                            'SELECT field FROM _content WHERE ref=? ORDER BY field', (anchor,))
                            if search_projection == 'all' or row['field'] in CONTENT_FIELDS)
                    else:
                        out.append(anchor)
            else:
                out.extend(resolved)
        elif isinstance(target, dict) and 'entry' in target:
            if set(target) - {'entry', 'part'}:
                raise ValueError('invalid entry target')
            card = _card(db, target['entry'])
            part = target.get('part', 'card')
            if part == 'anchors':
                out.extend(card['anchors'])
            elif part == 'card' and not source_only:
                out.append({'entry': card['id'], 'part': 'card'})
            else:
                raise ValueError('map cards are not source anchors; select part="anchors"')
        else:
            out.append(_anchor(db, target))
    return _unique(out)


def _expand(db, anchors):
    """Expand event ranges, retaining all exact field/line slices."""
    result = []
    for anchor in anchors:
        # Persisted map anchors can become stale after an explicit source refresh.
        # Validate them too: a missing field/range must not look like an empty read.
        if not (isinstance(anchor, dict) and 'entry' in anchor):
            _anchor(db, anchor)
        if isinstance(anchor, dict) and 'session' in anchor:
            lo, hi = anchor['events']
            result.extend(r['ref'] for r in _rows(db,
                'SELECT ref FROM events WHERE session=? AND seq BETWEEN ? AND ? ORDER BY seq',
                (anchor['session'], lo, hi)))
        else:
            result.append(anchor)
    return _unique(result)


def _ref(anchor):
    return anchor if isinstance(anchor, str) else anchor.get('event', anchor.get('object'))


# Catalog and structural queries.
def trace_open(db, workspace, sessions=None, descendants=True, refresh=False,
               page_size=8, cursor=None):
    _positive(page_size, 'page_size')
    if cursor:
        state = _uncursor(db, cursor, 'open')
    else:
        if not isinstance(descendants, bool) or not isinstance(refresh, bool):
            raise ValueError('descendants and refresh must be booleans')
        incoming = [] if sessions is None else _list(sessions, 'sessions')
        if any(not isinstance(s, str) or not s for s in incoming):
            raise ValueError('sessions must contain nonempty strings')
        loaders = {s: descendants for s in incoming}
        if refresh:
            loaders = {r['source']: bool(r['descendants']) for r in _rows(db, 'SELECT * FROM _sources')} | loaders
        if loaders:
            if __package__:
                from .source import load_sources
            else:
                from source import load_sources
            for locator, descend in loaders.items():
                for session in load_sources([locator], descendants=descend):
                    sid = session['id']
                    oldrefs = [r['ref'] for r in _rows(db,
                        'SELECT ref FROM events WHERE session=? UNION SELECT ref FROM objects WHERE session=?', (sid, sid))]
                    for ref in oldrefs:
                        db.execute('DELETE FROM _content WHERE ref=?', (ref,))
                        db.execute('DELETE FROM links WHERE src=?', (ref,))
                    db.execute('DELETE FROM events WHERE session=?', (sid,))
                    db.execute('DELETE FROM objects WHERE session=?', (sid,))
                    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)',
                        (sid, session.get('parent_id'), session.get('title', ''),
                         session.get('source', locator), _json(session.get('warnings', []))))
                    for event in session.get('events', []):
                        ref, text = event['ref'], event.get('text', '') or ''
                        args = event.get('arguments')
                        fields = dict(event.get('fields') or {})
                        if isinstance(event.get('system_generated'), bool):
                            fields['system_generated'] = _json(event['system_generated'])
                        db.execute('INSERT INTO events '
                            '(ref,session,seq,elapsed_s,kind,role,tool,arguments,preview,system_generated) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (ref, sid, event['seq'], event.get('elapsed_s'), event['kind'],
                             event.get('role', ''), event.get('tool'),
                             None if args is None else _json(args), text[:240], _system_generated(fields)))
                        fields['text'] = text
                        if args is not None:
                            fields['arguments'] = json.dumps(args, ensure_ascii=False, indent=2, sort_keys=True)
                        for field, content in fields.items():
                            db.execute('INSERT INTO _content VALUES(?,?,?)', (ref, field, str(content)))
                    for obj in session.get('objects', []):
                        text = obj.get('text')
                        available = bool(obj.get('available')) and text is not None
                        db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                            (obj['ref'], obj['kind'], len(text.splitlines()) if available else 0,
                             len(text) if available else 0, int(available), obj.get('session', sid), obj.get('reason')))
                        if available:
                            db.execute('INSERT INTO _content VALUES(?,?,?)', (obj['ref'], 'text', text))
                    for edge in session.get('links', []):
                        db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?)', (edge['src'], edge['dst'], edge['kind']))
                db.execute('INSERT OR REPLACE INTO _sources VALUES(?,?)', (locator, int(descend)))
            ensure_session_links(db, rebuild=True)
        sources = _rows(db, 'SELECT * FROM sessions ORDER BY id')
        for source in sources:
            source['warnings'] = json.loads(source['warnings'])
        cards = [_card(db, r['id']) for r in _rows(db, 'SELECT id FROM map_entries ORDER BY revision DESC,id')]
        state = {'offset': 0, 'sources': sources,
                 'pinned_entries': [_preview(c) for c in cards if c['pinned']],
                 'recent_entries': [_preview(c) for c in cards],
                 'saved_queries': _rows(db, 'SELECT * FROM saved_queries ORDER BY name'),
                 'counts': {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                            for table in ('sessions', 'events', 'objects', 'links', 'session_links', 'map_entries', 'map_links', 'saved_queries')}}
        for recipe in state['saved_queries']:
            recipe['arguments'] = json.loads(recipe['arguments'])
    offset = state['offset']
    keys = ('sources', 'pinned_entries', 'recent_entries', 'saved_queries')
    answer = {key: state[key][offset:offset + page_size] for key in keys}
    answer.update(workspace=str(workspace), counts=state['counts'], capabilities={
        'search_modes': ['literal', 'regex', 'terms'], 'similar': False,
        'embeddings': 'unsupported; no model calls',
        'content_types': ['events', 'objects', 'map_entries'],
        'projections': ['content', 'all'], 'default_projection': 'content',
        'read_budget_unit': 'text_chars', 'read_inspection': True,
        'session_navigation': True, 'search_include_references': True,
        'map_kinds': {'episode': 'requires anchors', 'note': 'anchors optional; use pinned for continuation'},
        'query_schema': 'docs/api.md',
        'sql_tables': sorted(PUBLIC), 'buffer_output': 'workspace packet file'})
    state['offset'] += page_size
    answer['next_cursor'] = _cursor(db, 'open', state) if any(len(state[k]) > state['offset'] for k in keys) else None
    return answer


def _sql(db, sql, params):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError('sql must be a read-only SELECT')
    cleaned = re.sub(r'/\*.*?\*/|--[^\n]*', '', sql, flags=re.S).lstrip()
    if not re.match(r'(?i)(SELECT|WITH)\b', cleaned):
        raise ValueError('only read-only SELECT queries are allowed')
    if not isinstance(params, dict):
        raise ValueError('params must be a named-parameter object')
    read_tables = set()
    def authorize(action, a, b, database, trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and a in PUBLIC and database == 'main':
            read_tables.add(a)
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (b or '').lower() not in {
                'load_extension', 'readfile', 'writefile', 'edit', 'eval', 'fts3_tokenizer'}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    ticks = [0]
    def progress():
        ticks[0] += 1
        return ticks[0] > 20000
    db.set_authorizer(authorize)
    db.set_progress_handler(progress, 1000)
    try:
        cursor = db.execute(sql, params)
        columns = [c[0] for c in cursor.description]
        if len(set(columns)) != len(columns):
            raise ValueError('query columns must have distinct names; use aliases')
        rows = [dict(r) for r in cursor]
    except sqlite3.Error as exc:
        raise ValueError('read-only SQL failed: ' + str(exc)) from exc
    finally:
        # Python 3.9/3.10 do not support set_authorizer(None).
        db.set_authorizer(lambda *unused: sqlite3.SQLITE_OK)
        db.set_progress_handler(None, 0)
    if 'map_entries' in read_tables and 'id' in columns:
        entry_ids = {r['id'] for r in _rows(db, 'SELECT id FROM map_entries')}
        for row in rows:
            if row['id'] in entry_ids:
                row['_entry_id'] = row['id']
    return columns, rows


def _sample(rows, sample, columns):
    if sample is None:
        return rows, None
    if not isinstance(sample, dict) or set(sample) - {'n', 'by', 'seed'} or 'n' not in sample:
        raise ValueError('sample requires n and optional by, seed')
    n = _positive(sample['n'], 'sample.n', zero=True)
    by = _list(sample.get('by', []), 'sample.by')
    if any(c not in columns for c in by):
        raise ValueError('sample.by contains an unknown column')
    rng = random.Random(sample.get('seed', 0))
    groups = {}
    for row in rows:
        key = _json([row[c] for c in by])
        groups.setdefault(key, []).append(row)
    original = {key: len(group) for key, group in groups.items()}
    for group in groups.values():
        rng.shuffle(group)
    chosen, counts = [], dict.fromkeys(groups, 0)
    while len(chosen) < min(n, len(rows)):
        for key, group in groups.items():
            if group and len(chosen) < n:
                chosen.append(group.pop())
                counts[key] += 1
    return chosen, {'n': n, 'by': by, 'seed': sample.get('seed', 0),
                    'population': len(rows), 'selected': len(chosen),
                    'strata': [{'values': json.loads(k), 'population': original[k], 'selected': counts[k]} for k in groups],
                    'unsampled_strata': [json.loads(k) for k in groups if not counts[k]]}


def _display_page(rows, offset, page_size, max_chars):
    _positive(page_size, 'page_size')
    _positive(max_chars, 'max_chars')
    selected, used = [], 0
    for row in rows[offset:offset + page_size]:
        visible = {k: v for k, v in row.items() if k not in {'_anchors', '_passage', '_entry_id'}}
        size = len(_json(visible))
        if used + size > max_chars:
            if selected:
                break
            # Never lose navigation or stall on a giant SQL cell. The full row persists.
            navigation = {'origin', 'ref', 'entry_id', 'anchor', 'occurrences_result',
                          'retrieval_mode', 'matched_field'}
            strings = {k: v for k, v in visible.items()
                       if k not in navigation and isinstance(v, str)}
            def clipped(limit):
                return {k: (v[:limit] if k in strings else v) for k, v in visible.items()} | {
                    '_display_truncated': True}
            low, high = 0, max((len(v) for v in strings.values()), default=0)
            while low < high:
                middle = (low + high + 1) // 2
                if len(_json(clipped(middle))) <= max_chars:
                    low = middle
                else:
                    high = middle - 1
            # Tiny budgets cannot discard the keys/anchors needed for navigation.
            visible = clipped(low)
            size = len(_json(visible))
        selected.append(visible)
        used += size
    return selected, offset + len(selected)


def _query_page(db, result, offset, page_size, max_chars):
    kind, rows, meta = _get_result(db, result)
    if kind != 'query':
        raise ValueError('not a query result')
    page, end = _display_page(rows, offset, page_size, max_chars)
    return {'result': result, 'columns': meta['columns'], 'rows': page,
            'row_count': len(rows), 'sampling': meta['sampling'],
            'next_cursor': _cursor(db, 'query', {'result': result, 'offset': end}) if end < len(rows) else None}


def trace_query(db, workspace, sql=None, params=None, saved=None, sample=None,
                page_size=20, max_chars=6000, cursor=None, save_as=None):
    if cursor:
        state = _uncursor(db, cursor, 'query')
        return _query_page(db, state['result'], state['offset'], page_size, max_chars)
    if saved:
        recipe = _saved(db, saved, 'query')
        sql, params, sample = recipe['sql'], recipe['params'], recipe.get('sample')
    if sql is None:
        raise ValueError('provide sql or saved')
    params = {} if params is None else params
    columns, rows = _sql(db, sql, params)
    rows, sampling = _sample(rows, sample, columns)
    result = _store_result(db, 'query', rows, {'columns': columns, 'sampling': sampling})
    _save_query(db, save_as, 'query', {'sql': sql, 'params': params, 'sample': sample}, result)
    if saved:
        db.execute('UPDATE saved_queries SET last_result=? WHERE name=?', (result, saved))
    return _query_page(db, result, 0, page_size, max_chars)


# Source scopes and lexical retrieval.
def _scope(db, scope, on, *, search_projection=None):
    scope = {'workspace': True} if scope is None else scope
    if not isinstance(scope, dict) or len(scope) != 1:
        raise ValueError('scope must specify exactly one selector')
    key, value = next(iter(scope.items()))
    if key == 'workspace' and value is True:
        return None, None
    if key == 'sessions':
        _list(value, 'scope.sessions')
        anchors = []
        for sid in value:
            if not db.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
                raise ValueError('unknown session: ' + str(sid))
            anchors.extend(r['ref'] for r in _rows(db,
                'SELECT ref FROM events WHERE session=? UNION SELECT ref FROM objects WHERE session=?', (sid, sid)))
    elif key == 'entries':
        _list(value, 'scope.entries')
        anchors = [a for entry in value for a in _card(db, entry)['anchors']]
        return _expand(db, anchors), set(value)
    elif key == 'refs':
        anchors = _resolve(db, value, True, search_projection=search_projection)
    elif key == 'result':
        anchors = _resolve(db, [{'result': value}], on != 'map',
                           search_projection=search_projection)
        cards = [a['entry'] for a in anchors if isinstance(a, dict) and 'entry' in a]
        anchors = [a for a in anchors if not (isinstance(a, dict) and 'entry' in a)]
        if cards:
            return _expand(db, anchors), set(cards)
    else:
        raise ValueError('invalid scope selector')
    return _expand(db, anchors), None


def _merge_ranges(ranges):
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(hi, merged[-1][1])
        else:
            merged.append([lo, hi])
    return merged


def _source_windows(db, anchors, references=True, projection='all', unavailable=None):
    """Unique (ref,field) windows. Explicit event fields bypass projection."""
    _projection(projection)
    if anchors is None:
        anchors = [r['ref'] for r in _rows(db, 'SELECT ref FROM events UNION SELECT ref FROM objects')]
    anchors = list(anchors)
    if references:
        extra = []
        for anchor in anchors:
            if isinstance(anchor, str) and db.execute('SELECT 1 FROM events WHERE ref=?', (anchor,)).fetchone():
                extra.extend(r['dst'] for r in _rows(db,
                    'SELECT l.dst FROM links l JOIN objects o ON o.ref=l.dst '
                    'WHERE l.src=? AND l.kind="references"', (anchor,)))
        anchors += extra
    windows = {}
    for anchor in anchors:
        ref = _ref(anchor)
        explicit_field = isinstance(anchor, dict) and 'event' in anchor
        rows = _rows(db, 'SELECT field,text FROM _content WHERE ref=? ORDER BY field', (ref,))
        if not rows and unavailable is not None:
            obj = _row(db, 'SELECT reason FROM objects WHERE ref=?', (ref,))
            unavailable.setdefault(ref, {'anchor': anchor,
                'reason': (obj or {}).get('reason') or 'Referenced content is unavailable'})
        for row in rows:
            if explicit_field:
                if row['field'] != anchor.get('field', 'text'):
                    continue
            elif projection == 'content':
                if row['field'] not in CONTENT_FIELDS:
                    continue
            limits = anchor.get('lines') if isinstance(anchor, dict) else None
            count = max(1, len(row['text'].splitlines()))
            lo, hi = limits or [1, count]
            windows.setdefault((ref, row['field']), []).append([lo, hi])
    return {key: _merge_ranges(value) for key, value in windows.items()}


def _intersects(db, left, right):
    # Anchor intersection honors field and line slices, not just container IDs.
    a = _source_windows(db, _expand(db, left), False)
    b = _source_windows(db, _expand(db, right), False)
    for key in a.keys() & b.keys():
        if any(x <= v and u <= y for x, y in a[key] for u, v in b[key]):
            return True
    # Unavailable references still anchor map entries.
    return bool({_ref(x) for x in _expand(db, left)} & {_ref(x) for x in _expand(db, right)} -
                {key[0] for key in a.keys() | b.keys()})


def _matcher(query, mode):
    if not isinstance(query, str) or not query:
        raise ValueError('query must be nonempty text')
    if mode == 'similar':
        raise ValueError('unsupported mode: similar (no embedding index configured)')
    if mode == 'literal':
        return re.compile(re.escape(query)), None
    if mode == 'regex':
        try:
            return re.compile(query), None
        except re.error as exc:
            raise ValueError('invalid regex: ' + str(exc)) from exc
    if mode == 'terms':
        terms = set(re.findall(r'\w+', query.casefold()))
        if not terms:
            raise ValueError('terms query contains no searchable words')
        return None, terms
    raise ValueError('unknown search mode: ' + str(mode))


def _matches(text, mode, pattern, terms, offset=0):
    lines = text.splitlines() or ['']
    if mode == 'terms':
        for i, line in enumerate(lines):
            count = total = 0
            first = None
            folded = line.casefold()
            for word in re.finditer(r'\w+', folded):
                total += 1
                if word.group() in terms:
                    count += 1
                    if first is None:
                        first = word.start()
            if count:
                if len(folded) != len(line):
                    folded_offset = 0
                    for position, char in enumerate(line):
                        folded_offset += len(char.casefold())
                        if folded_offset > first:
                            first = position
                            break
                begin = max(0, first - 160) if len(line) > 600 else 0
                excerpt = (('…' if begin else '') + line[begin:begin + 600])[:600]
                yield i + offset + 1, i + offset + 1, excerpt, count / max(1, total) ** .5
        return
    # Avoid rescanning the full prefix for every literal/regex occurrence.
    starts = [0] + [match.end() for match in re.finditer('\n', text)]
    seen = set()
    for match in pattern.finditer(text):
        lo = bisect_right(starts, match.start())
        hi = bisect_right(starts, max(match.start(), match.end() - 1))
        if (lo, hi) not in seen:
            seen.add((lo, hi))
            excerpt = '\n'.join(lines[lo - 1:hi])
            # A very long line must show the match, not only the line prefix.
            start_in_line = match.start() - starts[lo - 1]
            if len(excerpt) > 600:
                begin = max(0, start_in_line - 160)
                excerpt = (('…' if begin else '') + excerpt[begin:begin + 600])[:600]
            yield lo + offset, hi + offset, excerpt, None


def _scope_summary(db, windows, unavailable, include_references, projection):
    """Bounded source selection accounting, not a hit summary or contiguous range.

    Counts use effective windows plus known unavailable references. Fields count
    distinct searchable (ref, field) pairs; unavailable objects count as objects
    but contribute no searchable fields. Map-only searches have no source counts.
    """
    summary = {'include_references': include_references, 'projection': projection,
               'event_count': None, 'object_count': None, 'field_count': None,
               'unavailable_object_count': None, 'session_count': None,
               'sessions': [], 'sessions_truncated': False,
               'sequence_coverage': 'envelopes_not_contiguous'}
    if windows is None:
        return summary
    field_counts = {}
    for ref, field in windows:
        field_counts[ref] = field_counts.get(ref, 0) + 1
    sessions = {}
    events = objects = missing = 0
    for ref in sorted(set(field_counts) | set(unavailable)):
        event = _row(db, 'SELECT session,seq FROM events WHERE ref=?', (ref,))
        source = event or _row(db, 'SELECT session FROM objects WHERE ref=?', (ref,))
        if source is None:
            continue
        session = sessions.setdefault(source['session'], {
            'session': source['session'], 'event_count': 0, 'object_count': 0,
            'field_count': 0, 'unavailable_object_count': 0,
            'seq_min': None, 'seq_max': None})
        session['field_count'] += field_counts.get(ref, 0)
        if event:
            events += 1
            session['event_count'] += 1
            seq = event['seq']
            session['seq_min'] = seq if session['seq_min'] is None else min(session['seq_min'], seq)
            session['seq_max'] = seq if session['seq_max'] is None else max(session['seq_max'], seq)
        else:
            objects += 1
            session['object_count'] += 1
            if ref in unavailable:
                missing += 1
                session['unavailable_object_count'] += 1
    summary.update(event_count=events, object_count=objects, field_count=len(windows),
                   unavailable_object_count=missing, session_count=len(sessions),
                   sessions=[sessions[sid] for sid in sorted(sessions, key=str)[:READ_PREVIEW_LIMIT]],
                   sessions_truncated=len(sessions) > READ_PREVIEW_LIMIT)
    return summary


def _search_page(db, result, offset, page_size, max_chars):
    kind, rows, meta = _get_result(db, result)
    if kind not in ('search', 'occurrences'):
        raise ValueError('not a search result')
    page, end = _display_page(rows, offset, page_size, max_chars)
    return {'result': result, 'hits': {
        'source': [r for r in page if r['origin'] == 'source'],
        'map': [r for r in page if r['origin'] == 'map']},
        'matched_total': meta['matched_total'], 'selected_count': meta['selected_count'],
        # Old selections did not capture coverage; absence is unknown, not zero.
        'unavailable': meta.get('unavailable', []),
        'unavailable_count': meta.get('unavailable_count'),
        'scope_summary': meta.get('scope_summary'),
        'next_cursor': _cursor(db, 'search', {'result': result, 'offset': end}) if end < len(rows) else None}


def trace_search(db, workspace, scope=None, query=None, mode='terms', on='source',
                 collapse='none', top_k=100, page_size=12, max_chars=6000,
                 saved=None, cursor=None, save_as=None, projection='content',
                 include_references=True):
    _projection(projection)
    if not isinstance(include_references, bool):
        raise ValueError('include_references must be a boolean')
    if cursor:
        state = _uncursor(db, cursor, 'search')
        return _search_page(db, state['result'], state['offset'], page_size, max_chars)
    if saved:
        recipe = _saved(db, saved, 'search')
        scope, query, mode, on, collapse, top_k = (recipe[k] for k in ('scope', 'query', 'mode', 'on', 'collapse', 'top_k'))
        # Pre-v1 recipes searched all fields; do not silently change their meaning.
        projection = _projection(recipe.get('projection', 'all'))
        include_references = recipe.get('include_references', True)
        if not isinstance(include_references, bool):
            raise ValueError('include_references must be a boolean')
    if mode == 'similar':
        raise ValueError('unsupported mode: similar (no embedding index configured)')
    if on not in ('source', 'map', 'both'):
        raise ValueError('on must be source, map, or both')
    if collapse not in ('none', 'exact'):
        raise ValueError('collapse must be none or exact')
    _positive(top_k, 'top_k')
    pattern, terms = _matcher(query, mode)
    anchors, map_ids = _scope(db, scope, on,
                             search_projection=projection if on != 'map' else None)
    hits = {'source': [], 'map': []}
    coverage = {'scope_summary': _scope_summary(db, None, {}, include_references, projection)}
    if on in ('source', 'both'):
        unavailable = {}
        windows = _source_windows(db, anchors, references=include_references,
                                  projection=projection, unavailable=unavailable)
        if isinstance(scope, dict) and 'sessions' in scope:
            allowed_refs = {r['ref'] for sid in scope['sessions'] for r in _rows(db,
                'SELECT ref FROM events WHERE session=? UNION SELECT ref FROM objects WHERE session=?', (sid, sid))}
            windows = {key: value for key, value in windows.items() if key[0] in allowed_refs}
            unavailable = {ref: notice for ref, notice in unavailable.items() if ref in allowed_refs}
        coverage = {'unavailable': list(unavailable.values())[:READ_PREVIEW_LIMIT],
                    'unavailable_count': len(unavailable),
                    'scope_summary': _scope_summary(db, windows, unavailable,
                                                    include_references, projection)}
        for (ref, field), ranges in windows.items():
            text = _field_text(db, ref, field)
            lines = text.splitlines() or ['']
            is_event = db.execute('SELECT 1 FROM events WHERE ref=?', (ref,)).fetchone() is not None
            for start, end in ranges:
                passage = '\n'.join(lines[start - 1:end])
                for lo, hi, excerpt, score in _matches(passage, mode, pattern, terms, start - 1):
                    anchor = {'event': ref, 'field': field, 'lines': [lo, hi]} if is_event else {'object': ref, 'lines': [lo, hi]}
                    hit = {'ref': ref, 'anchor': anchor, 'matched_field': field,
                           'excerpt': excerpt[:600], 'origin': 'source', 'retrieval_mode': mode,
                           '_passage': '\n'.join(lines[lo - 1:hi])}
                    if score is not None:
                        hit['score'] = score
                    hits['source'].append(hit)
    if on in ('map', 'both'):
        if on == 'both':
            # The search-only source restriction must not narrow map intersections.
            anchors, map_ids = _scope(db, scope, on)
        for record in _rows(db, 'SELECT id FROM map_entries ORDER BY id'):
            card = _card(db, record['id'])
            if map_ids is not None:
                if card['id'] not in map_ids:
                    continue
            elif anchors is not None and not _intersects(db, card['anchors'], anchors):
                continue
            fields = {'title': card['title'], 'note': card['note'], 'tags': ' '.join(card['tags'])}
            fields.update({'fields.' + k: str(v) for k, v in card['fields'].items()
                           if isinstance(v, (str, int, float, bool))})
            for field, text in fields.items():
                for lo, hi, excerpt, score in _matches(text, mode, pattern, terms):
                    hit = {'entry_id': card['id'], 'matched_field': field, 'excerpt': excerpt[:600],
                           'origin': 'map', 'retrieval_mode': mode}
                    if score is not None:
                        hit['score'] = score
                    hits['map'].append(hit)
    counts = {key: len(value) for key, value in hits.items()}
    if mode == 'terms':
        for group in hits.values():
            group.sort(key=lambda h: -h['score'])
    if collapse == 'exact':
        groups = {}
        for hit in hits['source']:
            groups.setdefault(hit['_passage'], []).append(hit)
        collapsed = []
        for group in groups.values():
            hit = dict(group[0])
            hit['occurrence_count'] = len(group)
            for i, occurrence in enumerate(group):
                occurrence['row'] = i
            hit['occurrences_result'] = _store_result(db, 'occurrences', group, {
                'matched_total': {'source': len(group), 'map': 0},
                'selected_count': {'source': len(group), 'map': 0}, **coverage})
            hit['_anchors'] = [h['anchor'] for h in group]
            collapsed.append(hit)
        hits['source'] = collapsed
    rows = hits['source'] + hits['map']
    for i, hit in enumerate(rows):
        hit['row'] = i
    result = _store_result(db, 'search', rows, {'matched_total': counts, 'selected_count': dict(counts), **coverage})
    recipe = {'scope': scope, 'query': query, 'mode': mode, 'on': on, 'collapse': collapse,
              'top_k': top_k, 'projection': projection, 'include_references': include_references}
    _save_query(db, save_as, 'search', recipe, result)
    if saved:
        db.execute('UPDATE saved_queries SET last_result=? WHERE name=?', (result, saved))
    return _search_page(db, result, 0, page_size, max_chars)


# Bounded reading: requested slices first, then whole targets, then added context.
def _subtract(ranges, excluded):
    result = []
    for lo, hi in _merge_ranges(ranges):
        pieces = [[lo, hi]]
        for x, y in _merge_ranges(excluded):
            next_pieces = []
            for a, b in pieces:
                if y < a or x > b:
                    next_pieces.append([a, b])
                else:
                    if a < x:
                        next_pieces.append([a, x - 1])
                    if y < b:
                        next_pieces.append([y + 1, b])
            pieces = next_pieces
        result.extend(pieces)
    return result


def _read_plan(db, targets, before, after, pair, line_context, projection='content'):
    targets = _expand(db, _resolve(db, targets))
    cards = [a for a in targets if isinstance(a, dict) and 'entry' in a]
    sources = [a for a in targets if a not in cards]
    slices = [a for a in sources if isinstance(a, dict) and 'lines' in a]
    whole = [a for a in sources if a not in slices]
    contexts = []
    for anchor in sources:
        ref = _ref(anchor)
        event = _row(db, 'SELECT * FROM events WHERE ref=?', (ref,))
        if event:
            if before or after:
                contexts.extend(r['ref'] for r in _rows(db,
                    'SELECT ref FROM events WHERE session=? AND seq BETWEEN ? AND ? ORDER BY seq',
                    (event['session'], max(1, event['seq'] - before), event['seq'] + after)))
            if pair:
                contexts.extend(r['ref'] for r in _rows(db,
                    'SELECT dst AS ref FROM links WHERE src=? AND kind="call_result" '
                    'UNION SELECT src AS ref FROM links WHERE dst=? AND kind="call_result"', (ref, ref)))
        if isinstance(anchor, dict) and 'lines' in anchor and line_context:
            field = anchor.get('field', 'text')
            text = _field_text(db, ref, field)
            if text is not None:
                lo, hi = anchor['lines']
                context = dict(anchor)
                context['lines'] = [max(1, lo - line_context), min(max(1, len(text.splitlines())), hi + line_context)]
                contexts.append(context)
    plan = [{'entry_id': a['entry'], 'card': _card(db, a['entry']), 'requested': True} for a in cards]
    delivered_windows = {}
    for priority, anchors in enumerate((slices, whole, contexts)):
        windows = _source_windows(db, anchors, False, projection)
        for (ref, field), ranges in windows.items():
            previous = delivered_windows.setdefault((ref, field), [])
            needed = _subtract(ranges, previous)
            previous[:] = _merge_ranges(previous + ranges)
            event = _row(db, 'SELECT * FROM events WHERE ref=?', (ref,))
            obj = _row(db, 'SELECT * FROM objects WHERE ref=?', (ref,))
            text = _field_text(db, ref, field)
            lines = text.splitlines(keepends=True) or ['']
            for lo, hi in needed:
                anchor = {'event': ref, 'field': field, 'lines': [lo, hi]} if event else {'object': ref, 'lines': [lo, hi]}
                block = {'ref': ref, 'field': field, 'anchor': anchor,
                         'text': ''.join(lines[lo - 1:hi]), 'kind': event['kind'] if event else obj['kind'],
                         'requested': priority < 2, 'line_start': lo, 'char_offset': 0}
                if event:
                    generated = event.get('system_generated')
                    block.update(role=event['role'], tool=event['tool'],
                                 system_generated=None if generated is None else bool(generated))
                else:
                    block['session'] = obj['session']
                plan.append(block)
    for anchor in _unique(sources + contexts):
        ref = _ref(anchor)
        if not db.execute('SELECT 1 FROM _content WHERE ref=?', (ref,)).fetchone():
            obj = _row(db, 'SELECT * FROM objects WHERE ref=?', (ref,))
            if not any(b.get('ref') == ref and b.get('available') is False for b in plan):
                plan.append({'ref': ref, 'anchor': anchor, 'available': False,
                             'reason': (obj or {}).get('reason') or 'Referenced content is unavailable',
                             'requested': anchor in sources})
    return plan


def _read_size(block):
    """Charge cards as compact JSON and source bodies as Unicode characters."""
    if 'card' in block:
        return len(_json(block['card']))
    return len(block['text']) if 'text' in block else None


def _read_take(size, card, budget, block_count):
    """Return the next block's text allowance, or None when this page must stop."""
    if budget <= 0 or block_count >= READ_BLOCK_LIMIT:
        return None
    # A card only splits when it is the first block, even if some budget remains.
    if card and size > budget and block_count:
        return None
    return min(size, budget) if size is not None else 0


def _inspect_read(db, plan, max_chars):
    """Persist only cost metadata and exact anchors, never read bodies or cursors."""
    _positive(max_chars, 'max_chars')
    rows = []
    budget, block_count, stopped = max_chars, 0, False
    for index, block in enumerate(plan):
        size = _read_size(block)
        take = None if stopped else _read_take(size, 'card' in block, budget, block_count)
        if take is None:
            stopped = True
        else:
            budget -= take
            block_count += 1
        card = block.get('card')
        anchor = {'entry': block['entry_id'], 'part': 'card'} if card is not None else block['anchor']
        metadata = block
        if size is None:
            metadata = (_row(db, 'SELECT kind,role,tool FROM events WHERE ref=?', (block['ref'],))
                        or _row(db, 'SELECT kind FROM objects WHERE ref=?', (block['ref'],)) or {})
        rows.append({'row': index, 'anchor': anchor, 'requested': block['requested'],
                     'kind': 'map_card' if card is not None else metadata.get('kind'),
                     'role': metadata.get('role'), 'tool': metadata.get('tool'),
                     'text_chars': size, 'would_deliver_chars': (take or 0) if size is not None else None,
                     'available': size is not None, '_anchors': [anchor]})
    columns = ['row', 'anchor', 'requested', 'kind', 'role', 'tool',
               'text_chars', 'would_deliver_chars', 'available']
    result = _store_result(db, 'query', rows, {'columns': columns, 'sampling': None})
    page, _ = _display_page(rows, 0, 16, 4000)
    # Unlike ordinary query display, inspection's fixed cap cannot overflow for
    # a giant navigation anchor. Keep such a row available through plan_cursor.
    while len(_json(page)) > 4000:
        page.pop()
    return {'plan_result': result, 'rows': page,
            'plan_cursor': _cursor(db, 'query', {'result': result, 'offset': len(page)}) if len(page) < len(rows) else None,
            'total_rows': len(rows), 'known_text_chars': sum(r['text_chars'] or 0 for r in rows),
            'unavailable_rows': sum(not r['available'] for r in rows),
            'partially_delivered_rows': sum(0 < r['would_deliver_chars'] < r['text_chars']
                                            for r in rows if r['available']),
            'fully_deferred_rows': sum(r['text_chars'] > 0 and r['would_deliver_chars'] == 0
                                      for r in rows if r['available']),
            'max_chars': max_chars, 'budget_unit': 'text_chars',
            'predicted_text_chars': max_chars - budget}


def _compact_read(result):
    """Render a selected page without changing its bodies, coverage or navigation."""
    result = dict(result)
    blocks, delivered = [], []
    for original in result['blocks']:
        block = dict(original)
        if block.get('requested') is True:
            block.pop('requested')
        if 'text' in block:
            if block.get('char_end') == block.get('char_offset', 0) + len(block['text']):
                block.pop('char_end')
            if block.get('char_offset') == 0:
                block.pop('char_offset')
            if block.get('complete') is True:
                block.pop('complete')
        blocks.append(block)
        delivery = ({'anchor': block['anchor']} if 'anchor' in block else
                    {'entry': block.get('entry_id'), 'part': 'card'})
        if 'text' in block:
            offset = block.get('char_offset', 0)
            delivery.update(char_offset=offset, char_end=block.get('char_end', offset + len(block['text'])))
        elif 'card' not in block:
            delivery = {'anchor': block.get('anchor'), 'available': False}
        delivered.append(delivery)
    # Old cursor blocks may lack anchors. Keep any non-reconstructible navigation.
    if delivered == result['delivered']:
        result.pop('delivered')
    result.update(blocks=blocks, packet_format='compact_read_json_v1', packet_chars=0)
    # Include the count's own digits, just as in the legacy inline layout.
    while result['packet_chars'] != len(_json(result)):
        result['packet_chars'] = len(_json(result))
    return result


def _read_page(db, workspace, plan, max_chars, output):
    """max_chars bounds content text, not headers, metadata or JSON escaping.

    packet_chars is Unicode characters in the UTF-8 packet file (buffer), or
    in this response's canonical compact JSON (inline/compact), not CLI pretty JSON.
    """
    _positive(max_chars, 'max_chars')
    if output not in ('inline', 'compact', 'buffer'):
        raise ValueError('output must be inline, compact or buffer')
    blocks, delivered = [], []
    budget = max_chars
    rest = []
    for index, original in enumerate(plan):
        size = _read_size(original)
        take = _read_take(size, 'card' in original, budget, len(blocks))
        if take is None:
            rest.extend(plan[index:])
            break
        block = dict(original)
        if 'card' in block:
            if size > budget:
                # Split oversized cards as compact JSON, preserving character offsets.
                block['text'] = _json(block.pop('card'))
                block['format'] = 'map_card_json'
                block['char_offset'] = 0
            else:
                blocks.append(block)
                delivered.append({'entry': block['entry_id'], 'part': 'card'})
                budget -= size
                continue
        if 'text' not in block:
            blocks.append(block)
            delivered.append({'anchor': block.get('anchor', block.get('ref')), 'available': False})
            continue
        text = block['text']
        shown = text[:take]
        block['text'] = shown
        start = block.get('line_start', 1)
        line_end = start + shown.count('\n') - (1 if shown.endswith('\n') else 0)
        line_end = max(start, line_end)
        if 'anchor' in block:
            block['anchor'] = dict(block['anchor'])
            block['anchor']['lines'] = [start, line_end]
        block['char_end'] = block.get('char_offset', 0) + take
        block['complete'] = take == len(text)
        # Do not duplicate parsed arguments: the exact argument text is already here.
        blocks.append(block)
        delivery = {'anchor': block['anchor']} if 'anchor' in block else {'entry': block.get('entry_id'), 'part': 'card'}
        delivery.update(char_offset=block.get('char_offset', 0), char_end=block['char_end'])
        delivered.append(delivery)
        budget -= take
        if take < len(text):
            tail = dict(original)
            if 'card' in tail:
                tail.pop('card')
                tail.update(format='map_card_json', text=text, char_offset=0)
            tail['text'] = text[take:]
            tail['char_offset'] = block['char_end']
            tail['line_start'] = start + shown.count('\n')
            if 'anchor' in tail:
                tail['anchor'] = dict(tail['anchor'])
                tail['anchor']['lines'] = [tail['line_start'], tail['anchor']['lines'][1]]
            rest.append(tail)
            rest.extend(plan[index + 1:])
            break
    remaining = []
    for block in rest[:READ_PREVIEW_LIMIT]:
        item = {'anchor': block['anchor']} if 'anchor' in block else {'entry': block.get('entry_id'), 'part': 'card'}
        if 'char_offset' in block:
            item['char_offset'] = block['char_offset']
        remaining.append(item)
    references = []
    sources = {}
    for block in blocks:
        ref = block.get('ref')
        if ref is None or ref in sources:
            continue
        sources[ref] = {k: block[k] for k in ('kind', 'role', 'tool', 'system_generated')
                        if k in block and (block[k] is not None or k == 'system_generated')}
        if 'role' not in block and block.get('session') is not None:
            sources[ref]['session'] = block['session']
        references.extend(_rows(db, 'SELECT * FROM links WHERE src=? OR dst=? ORDER BY kind,src,dst', (ref, ref)))
    references = sorted(_unique(references), key=lambda r: (r['kind'], r['src'], r['dst']))
    # Compact old cursor blocks too, without rewriting or re-projecting their snapshot.
    private_headers = {'ref', 'field', 'kind', 'role', 'tool', 'system_generated',
                       'session', 'targets', 'line_start', 'arguments'}
    blocks = [{k: v for k, v in block.items() if k not in private_headers} for block in blocks]
    result = {'blocks': blocks, 'sources': sources, 'delivered': delivered,
              'block_count': len(blocks), 'remaining': remaining, 'remaining_count': len(rest),
              'references': references[:READ_PREVIEW_LIMIT], 'reference_count': len(references),
              'unavailable': [{'anchor': b.get('anchor', b.get('ref')), 'reason': b.get('reason')}
                              for b in plan if b.get('available') is False][:READ_PREVIEW_LIMIT],
              'unavailable_count': sum(b.get('available') is False for b in plan),
              'max_chars': max_chars, 'budget_unit': 'text_chars', 'text_chars': max_chars - budget,
              'next_cursor': _cursor(db, 'read', {'blocks': rest}) if rest else None}
    if len(references) > READ_PREVIEW_LIMIT:
        # Full reference snapshot is query-pageable using the existing query operation.
        rows = [dict(r, _anchors=[r['src'], r['dst']]) for r in references]
        result['references_result'] = _store_result(db, 'query', rows,
            {'columns': ['src', 'dst', 'kind'], 'sampling': None})
        result['references_cursor'] = _cursor(db, 'query',
            {'result': result['references_result'], 'offset': READ_PREVIEW_LIMIT})
    navigation = navigation_rows(db, list(sources))
    result.update(session_navigation=navigation[:READ_PREVIEW_LIMIT],
                  session_navigation_count=len(navigation))
    if len(navigation) > READ_PREVIEW_LIMIT:
        rows = [dict(row, _anchors=[row['anchor']]) for row in navigation]
        result['session_navigation_result'] = _store_result(db, 'query', rows,
            {'columns': sorted({key for row in navigation for key in row}), 'sampling': None})
        result['session_navigation_cursor'] = _cursor(db, 'query',
            {'result': result['session_navigation_result'], 'offset': READ_PREVIEW_LIMIT})
    if output == 'buffer':
        packet = Path(workspace) / 'packets'
        packet.mkdir(exist_ok=True)
        token = _cursor(db, 'packet', {})
        path = packet / (token + '.txt')
        sections = ['# sources ' + _json(sources)] if sources else []
        for block in blocks:
            label = {k: v for k, v in block.items() if k not in ('text', 'card')}
            body = block['text'] if 'text' in block else _json(block['card']) if 'card' in block else ''
            sections.append('# ' + _json(label) + '\n' + body)
        packet_text = '\n\n'.join(sections)
        path.write_text(packet_text, encoding='utf-8')
        result.pop('blocks')
        result.update(buffer=str(path), buffer_path=str(path),
                      packet_format='text', packet_chars=len(packet_text))
    elif output == 'compact':
        return _compact_read(result)
    else:
        result.update(packet_format='compact_json', packet_chars=0)
        # Include the count's own digits in the compact response length.
        while result['packet_chars'] != len(_json(result)):
            result['packet_chars'] = len(_json(result))
    return result


def trace_read(db, workspace, targets=None, before=0, after=0, pair=True,
               line_context=10, max_chars=12000, output='inline', cursor=None,
               projection='content', inspect=False):
    if not isinstance(inspect, bool):
        raise ValueError('inspect must be a boolean')
    if inspect and cursor is not None:
        raise ValueError('inspect=true cannot accompany cursor; inspection is initial-request-only')
    if inspect and output != 'inline':
        raise ValueError('inspect=true requires output="inline"')
    _projection(projection)
    for name, value in (('before', before), ('after', after), ('line_context', line_context)):
        _positive(value, name, zero=True)
    if not isinstance(pair, bool):
        raise ValueError('pair must be a boolean')
    if cursor:
        plan = _uncursor(db, cursor, 'read')['blocks']
    else:
        if targets is None:
            raise ValueError('targets are required')
        plan = _read_plan(db, targets, before, after, pair, line_context, projection)
    if inspect:
        return _inspect_read(db, plan, max_chars)
    return _read_page(db, workspace, plan, max_chars, output)


# Investigator-authored map updates; the enclosing dispatch transaction is atomic.
def trace_edit(db, workspace, put=None, delete=None, link=None, unlink=None):
    put = _list([] if put is None else put, 'put')
    delete = _list([] if delete is None else delete, 'delete')
    link = _list([] if link is None else link, 'link')
    unlink = _list([] if unlink is None else unlink, 'unlink')
    created, updated, linked, unlinked, deleted, previews = [], [], [], [], [], []
    for relation in unlink:
        if not isinstance(relation, str):
            raise ValueError('unlink IDs must be strings')
        if db.execute('DELETE FROM map_links WHERE id=?', (relation,)).rowcount:
            unlinked.append(relation)
    ids = set()
    revision = db.execute('SELECT coalesce(max(revision),0) FROM map_entries').fetchone()[0]
    for patch in put:
        allowed = {'id', 'kind', 'title', 'note', 'anchors', 'tags', 'fields', 'pinned',
                   'details_path', 'add_anchors', 'remove_anchors'}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError('invalid map entry fields')
        entry = patch.get('id')
        if not isinstance(entry, str) or not entry:
            raise ValueError('map entry requires a nonempty string id')
        if entry in ids or entry in delete:
            raise ValueError('duplicate or conflicting map entry operation: ' + entry)
        ids.add(entry)
        exists = db.execute('SELECT 1 FROM map_entries WHERE id=?', (entry,)).fetchone() is not None
        if not exists and not {'id', 'kind', 'title'} <= set(patch):
            raise ValueError('new entry requires id, kind, title')
        if 'anchors' in patch and {'add_anchors', 'remove_anchors'} & set(patch):
            raise ValueError('anchors cannot accompany add_anchors or remove_anchors')
        card = _card(db, entry) if exists else {
            'id': entry, 'kind': patch['kind'], 'title': patch['title'], 'note': '',
            'anchors': [], 'tags': [], 'fields': {}, 'pinned': False, 'details_path': None}
        for key in ('kind', 'title', 'note', 'tags', 'pinned', 'details_path'):
            if key in patch:
                default = {'note': '', 'tags': [], 'pinned': False, 'details_path': None}
                card[key] = default.get(key) if patch[key] is None else patch[key]
        if card['kind'] not in ('episode', 'note'):
            raise ValueError('map kind must be episode or note')
        if not isinstance(card['title'], str) or not isinstance(card['note'], str):
            raise ValueError('title and note must be strings')
        if not isinstance(card['tags'], list) or any(not isinstance(t, str) for t in card['tags']):
            raise ValueError('tags must be a list of strings')
        if not isinstance(card['pinned'], bool):
            raise ValueError('pinned must be a boolean')
        if card['details_path'] is not None and not isinstance(card['details_path'], str):
            raise ValueError('details_path must be a string or null')
        if 'fields' in patch:
            if patch['fields'] is None:
                card['fields'] = {}
            elif not isinstance(patch['fields'], dict):
                raise ValueError('fields must be an object or null')
            else:
                for key, value in patch['fields'].items():
                    if value is None:
                        card['fields'].pop(key, None)
                    else:
                        card['fields'][key] = value
        if 'anchors' in patch:
            card['anchors'] = _resolve(db, patch['anchors'], True)
        else:
            remove = _resolve(db, patch.get('remove_anchors', []), True)
            if any(a not in card['anchors'] for a in remove):
                raise ValueError('remove_anchors must name exact existing anchors')
            card['anchors'] = [a for a in card['anchors'] if a not in remove]
            card['anchors'] = _unique(card['anchors'] + _resolve(db, patch.get('add_anchors', []), True))
        if card['kind'] == 'episode' and not card['anchors']:
            raise ValueError('episode requires at least one anchor')
        revision += 1
        db.execute('INSERT OR REPLACE INTO map_entries VALUES(?,?,?,?,?,?,?,?,?,?)',
            (entry, card['kind'], card['title'], card['note'], _json(card['anchors']),
             _json(card['tags']), _json(card['fields']), int(card['pinned']), card['details_path'], revision))
        db.execute('DELETE FROM map_members WHERE entry_id=?', (entry,))
        for anchor in _expand(db, card['anchors']):
            db.execute('INSERT OR IGNORE INTO map_members VALUES(?,?)', (entry, _ref(anchor)))
        (updated if exists else created).append(entry)
        previews.append(_preview(card))
    for edge in link:
        if not isinstance(edge, dict) or set(edge) - {'id', 'from', 'to', 'label', 'note'}:
            raise ValueError('invalid relation fields')
        if not {'id', 'from', 'to', 'label'} <= set(edge):
            raise ValueError('relation requires id, from, to, label')
        if any(not isinstance(edge[k], str) or not edge[k] for k in ('id', 'from', 'to', 'label')):
            raise ValueError('relation id, from, to, and label must be nonempty strings')
        note = edge.get('note') or ''
        if not isinstance(note, str):
            raise ValueError('relation note must be a string')
        _card(db, edge['from'])
        _card(db, edge['to'])
        db.execute('INSERT OR REPLACE INTO map_links VALUES(?,?,?,?,?)',
                   (edge['id'], edge['from'], edge['to'], edge['label'], note))
        linked.append(edge['id'])
    for entry in delete:
        if not isinstance(entry, str):
            raise ValueError('delete IDs must be strings')
        if db.execute('SELECT 1 FROM map_links WHERE src=? OR dst=?', (entry, entry)).fetchone():
            raise ValueError('deletion leaves dangling relations; unlink them in the same call')
        if db.execute('DELETE FROM map_entries WHERE id=?', (entry,)).rowcount:
            db.execute('DELETE FROM map_members WHERE entry_id=?', (entry,))
            deleted.append(entry)
    return {'created': created, 'updated': updated, 'deleted': deleted, 'linked': linked,
            'unlinked': unlinked, 'entry_previews': previews}


def trace_cooccur(db, workspace, seed=None, against=None, page_size=10,
                  max_chars=6000, cursor=None, saved=None, save_as=None):
    """Join retained selections without loading the backend for other tools."""
    if __package__:
        from .cooccur import trace_cooccur as cooccur
    else:
        from cooccur import trace_cooccur as cooccur
    return cooccur(db, workspace, seed=seed, against=against, page_size=page_size,
                   max_chars=max_chars, cursor=cursor, saved=saved, save_as=save_as)


_OPERATIONS = {
    'trace_open': (trace_open, {'sessions', 'descendants', 'refresh', 'page_size', 'cursor'}),
    'trace_query': (trace_query, {'sql', 'params', 'saved', 'sample', 'page_size', 'max_chars', 'cursor', 'save_as'}),
    'trace_search': (trace_search, {'scope', 'query', 'mode', 'on', 'collapse', 'top_k', 'page_size', 'max_chars', 'saved', 'cursor', 'save_as', 'projection', 'include_references'}),
    'trace_read': (trace_read, {'targets', 'before', 'after', 'pair', 'line_context', 'max_chars', 'output', 'cursor', 'projection', 'inspect'}),
    'trace_edit': (trace_edit, {'put', 'delete', 'link', 'unlink'}),
    'trace_cooccur': (trace_cooccur, {'seed', 'against', 'page_size', 'max_chars', 'cursor', 'saved', 'save_as'}),
}


def dispatch(tool, arguments):
    """Execute one of the six trace contracts in a single SQLite transaction."""
    if tool not in _OPERATIONS:
        raise ValueError('unknown trace tool: ' + str(tool))
    if not isinstance(arguments, dict):
        raise ValueError('arguments must be an object')
    operation, allowed = _OPERATIONS[tool]
    unknown = set(arguments) - allowed - {'workspace'}
    if unknown:
        raise ValueError('unknown arguments: ' + ', '.join(sorted(unknown)))
    args = dict(arguments)
    workspace = args.pop('workspace', None)
    if not isinstance(workspace, (str, Path)) or not str(workspace):
        raise ValueError('workspace is required and must be a directory path')
    workspace = Path(workspace).expanduser().resolve()
    if args.get('cursor') is not None:
        paging = {'cursor', 'page_size', 'max_chars'}
        if tool == 'trace_read':
            paging = {'cursor', 'max_chars', 'output'}
        prohibited = set(args) - paging
        if tool == 'trace_read':
            # False is inert; trace_read validates the type and rejects True.
            # Keep the existing cursor-selection diagnostic unchanged.
            prohibited.discard('inspect')
        if prohibited:
            raise ValueError(
                'cursor cannot accompany supplied selection or expansion arguments: '
                + ', '.join(sorted(prohibited))
                + '; allowed arguments with cursor: '
                + ', '.join(sorted({'workspace'} | (paging & allowed)))
                + '. Omit other arguments (native tools also accept null).')
    if tool == 'trace_query' and args.get('saved') is not None and any(k in args for k in ('sql', 'params', 'sample')):
        raise ValueError('saved cannot accompany a new query')
    if tool == 'trace_search' and args.get('saved') is not None and any(k in args for k in ('scope', 'query', 'mode', 'on', 'collapse', 'top_k', 'projection', 'include_references')):
        raise ValueError('saved cannot accompany a new search')
    if tool == 'trace_cooccur' and args.get('saved') is not None and any(k in args for k in ('seed', 'against')):
        raise ValueError('saved cannot accompany seed or against')
    db = None
    try:
        path = workspace / 'trace.sqlite3'
        if tool == 'trace_open':
            workspace.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise ValueError('workspace is not initialized; call trace_open first')
        db = sqlite3.connect(str(path))
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        if tool == 'trace_open':
            db.executescript(SCHEMA)
        with db:
            # Keep ALTER and backfill atomic even if the requested operation fails.
            db.execute('BEGIN')
            _ensure_system_generated(db)
            ensure_session_links(db)
            return operation(db, workspace, **args)
    except (sqlite3.Error, OSError, TypeError, KeyError) as exc:
        raise ValueError(str(exc)) from exc
    finally:
        if db is not None:
            db.close()
