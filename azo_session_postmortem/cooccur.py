"""Bounded, same-session joins over immutable retained selections.

No source bodies are copied into joined results. Input JSON is loaded by the
engine's existing result loader (not a streaming or bounded-memory parser).
"""
from bisect import bisect_left, bisect_right

if __package__:
    from . import engine as e
else:  # engine.py's standalone dispatch path
    import engine as e

MAX_INPUT_ANCHORS = 50000
MAX_SEED_EVENTS = 5000
MAX_WITNESS_ROWS = 100000
MAX_RESULT_BYTES = 64 * 1024 * 1024

SEED_COLUMNS = ['row', 'ref', 'session', 'seq', 'anchor', 'seed_anchor_count',
                'conditions', 'failed_conditions']
WITNESS_COLUMNS = ['row', 'ref', 'anchor', 'delta_seq', 'kind', 'role', 'tool',
                   'system_generated']


def _limit(value, ceiling, name):
    if value > ceiling:
        raise ValueError(f'{name} exceeded ({ceiling}); narrow selectors')


def _name(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(label + ' must be a nonempty string')
    return value


def _selector(value):
    if not isinstance(value, dict) or 'result' not in value or set(value) - {'result', 'rows'}:
        raise ValueError('selection requires result and optional rows only')
    if 'rows' in value:
        for index in e._list(value['rows'], 'rows'):
            e._positive(index, 'row', zero=True)
    return value


def _conditions(value):
    e._list(value, 'against')
    if not 1 <= len(value) <= 3:
        raise ValueError('against must contain 1 to 3 conditions')
    names = {'seed'}
    for condition in value:
        if not isinstance(condition, dict) or set(condition) != {'name', 'selection', 'before', 'after'}:
            raise ValueError('each condition requires name, selection, before, after only')
        name = _name(condition['name'], 'condition name')
        if name in names:
            raise ValueError('condition names must be unique and cannot be seed')
        names.add(name)
        _selector(condition['selection'])
        e._positive(condition['before'], 'before', zero=True)
        e._positive(condition['after'], 'after', zero=True)
    return value


class _Membership:
    """Validate once per source field and materialize once per input selection."""

    def __init__(self, db):
        self.db = db
        self.results = {}
        self.events = {}
        self.objects = {}
        self.fields = {}
        self.expanded_count = 0

    def result(self, result):
        # Validate before dictionary lookup: malformed JSON containers must not
        # leak unhashable-type errors from direct Python calls.
        if not isinstance(result, str):
            raise ValueError('invalid result ID')
        if result not in self.results:
            value = e._get_result(self.db, result)
            if value[0] not in ('search', 'occurrences', 'query'):
                raise ValueError('cooccur requires search, occurrences, or query results')
            self.results[result] = value
        return self.results[result]

    def event(self, ref):
        _name(ref, 'source reference')
        if ref not in self.events:
            self.events[ref] = e._row(self.db,
                'SELECT ref,session,seq,kind,role,tool,system_generated FROM events WHERE ref=?', (ref,))
        return self.events[ref]

    def object(self, ref):
        _name(ref, 'source reference')
        if ref not in self.objects:
            self.objects[ref] = e._row(self.db, 'SELECT ref FROM objects WHERE ref=?', (ref,))
        return self.objects[ref]

    def field_length(self, ref, field):
        key = ref, field
        if key not in self.fields:
            text = e._field_text(self.db, ref, field)
            self.fields[key] = None if text is None else max(1, len(text.splitlines()))
        return self.fields[key]

    def consume(self, count=1):
        self.expanded_count += count
        _limit(self.expanded_count, MAX_INPUT_ANCHORS, 'MAX_INPUT_ANCHORS')

    def anchors(self, anchor):
        """Yield exact source anchors; event ranges expand to whole-event refs."""
        if isinstance(anchor, dict) and 'session' in anchor:
            if set(anchor) != {'session', 'events'}:
                raise ValueError('event range requires session and events only')
            _name(anchor['session'], 'session')
            lo, hi = e._range(anchor['events'])
            self.consume(hi - lo + 1)
            rows = e._rows(self.db, 'SELECT ref FROM events WHERE session=? '
                           'AND seq BETWEEN ? AND ? ORDER BY seq,ref', (anchor['session'], lo, hi))
            if len(rows) != hi - lo + 1:
                raise ValueError('event range contains unknown events')
            for row in rows:
                yield row['ref'], self.event(row['ref'])
            return
        if isinstance(anchor, str):
            event = self.event(anchor)
            if event is None and self.object(anchor) is None:
                raise ValueError('unknown source reference: ' + anchor)
        elif isinstance(anchor, dict):
            key = 'object' if 'object' in anchor else 'event' if 'event' in anchor else None
            if key is None or set(anchor) - ({key, 'lines'} | ({'field'} if key == 'event' else set())):
                raise ValueError('invalid source address')
            ref = _name(anchor[key], 'source reference')
            event = self.event(ref) if key == 'event' else None
            if (event if key == 'event' else self.object(ref)) is None:
                raise ValueError('unknown ' + key + ': ' + ref)
            field = _name(anchor.get('field', 'text'), 'field')
            count = self.field_length(ref, field)
            if key == 'event' and count is None:
                raise ValueError('unknown event field: ' + field)
            if 'lines' in anchor:
                _, hi = e._range(anchor['lines'])
                if count is not None and hi > count:
                    raise ValueError('line range exceeds source field: ' + ref + ', field=' + field)
        else:
            raise ValueError('source anchor must be a reference or address object')
        self.consume()
        yield anchor, event

    def row_anchors(self, row):
        if not isinstance(row, dict):
            raise ValueError('malformed retained result row')
        # Map cards are deliberately excluded, not expanded to current anchors.
        if row.get('origin') == 'map' or row.get('entry_id') is not None or row.get('_entry_id') is not None:
            return []
        if '_anchors' in row:
            anchors = e._list(row['_anchors'], 'retained _anchors')
            if row.get('origin') == 'source' and not anchors:
                raise ValueError('retained source row has no source anchor')
            return anchors
        if 'occurrences_result' in row:
            kind, occurrences, _ = self.result(row['occurrences_result'])
            if kind != 'occurrences':
                raise ValueError('occurrences_result must identify an occurrences result')
            anchors = []
            for occurrence in occurrences:
                if not isinstance(occurrence, dict) or occurrence.get('origin') != 'source':
                    raise ValueError('malformed retained occurrence')
                targets = e._row_targets(occurrence)
                if not targets:
                    raise ValueError('retained occurrence has no source anchor')
                anchors.extend(targets)
                # Bound fallback expansion before constructing a huge list.
                _limit(self.expanded_count + len(anchors), MAX_INPUT_ANCHORS, 'MAX_INPUT_ANCHORS')
            return anchors
        targets = e._row_targets(row)
        if row.get('origin') == 'source' and not targets:
            raise ValueError('retained source row has no source anchor')
        return targets

    def select(self, name, selector):
        selector = _selector(selector)
        kind, rows, meta = self.result(selector['result'])
        indexes = selector.get('rows', range(len(rows)))
        seen, events = set(), {}
        duplicate = excluded = anchor_count = 0
        for index in indexes:
            if index >= len(rows):
                raise ValueError('result row out of range')
            targets = self.row_anchors(rows[index])
            if not targets:
                excluded += 1
            for target in targets:
                if isinstance(target, dict) and 'entry' in target:
                    if set(target) - {'entry', 'part'} or target.get('part', 'card') not in ('card', 'anchors'):
                        raise ValueError('invalid entry target')
                    _name(target['entry'], 'entry')
                    excluded += 1
                    continue
                for anchor, event in self.anchors(target):
                    canonical = e._json(anchor)
                    if canonical in seen:
                        duplicate += 1
                        continue
                    seen.add(canonical)
                    if event is None:
                        excluded += 1
                        continue
                    anchor_count += 1
                    group = events.setdefault(event['ref'], {'event': event, 'anchors': []})
                    group['anchors'].append(anchor)
                    if name == 'seed':
                        _limit(len(events), MAX_SEED_EVENTS, 'MAX_SEED_EVENTS')
        coverage = {'unavailable_count': meta.get('unavailable_count') if kind != 'query' else None,
                    'unavailable': meta.get('unavailable', [])[:8] if kind != 'query' else [],
                    'scope': 'input_result'}
        info = {'name': name, 'result': selector['result'], 'selected_rows': len(indexes),
                'event_count': len(events), 'anchor_count': anchor_count,
                'excluded_non_event': excluded, 'duplicate_anchors': duplicate, 'coverage': coverage}
        return events, info


def _anchor_order(anchor):
    if isinstance(anchor, str):
        return '', (), e._json(anchor)
    return anchor.get('field', 'text'), tuple(anchor.get('lines', [])), e._json(anchor)


def _index(events):
    sessions = {}
    for group in events.values():
        event = group['event']
        for anchor in group['anchors']:
            sessions.setdefault(event['session'], []).append((event, anchor))
    indexed = {}
    for session, witnesses in sessions.items():
        witnesses.sort(key=lambda item: (item[0]['seq'], item[0]['ref'], *_anchor_order(item[1])))
        indexed[session] = ([item[0]['seq'] for item in witnesses], witnesses)
    return indexed


def _witness_key(item, seq):
    event, anchor = item
    return abs(event['seq'] - seq), event['seq'], event['ref'], *_anchor_order(anchor)


def _window(index, event, before, after):
    seqs, items = index.get(event['session'], ([], []))
    seq = event['seq']
    lo, hi = bisect_left(seqs, seq - before), bisect_right(seqs, seq + after)
    if lo < hi:
        return 'within_window', items, lo, hi
    if not items:
        return 'no_same_session_match', items, 0, 0
    at = bisect_left(seqs, seq)
    candidates = [at] if at < len(items) else []
    if at:
        # Pick the first anchor of the previous sequence, not its last anchor.
        candidates.append(bisect_left(seqs, seqs[at - 1]))
    nearest = min(candidates, key=lambda i: _witness_key(items[i], seq))
    return 'outside_window', items, nearest, nearest + 1


class _Results:
    def __init__(self, db):
        self.db = db
        self.bytes = 0
        self.witnesses = 0

    def query(self, rows, columns, extra=None):
        meta = {'columns': columns, 'sampling': None, **(extra or {}),
                '_search_no_implicit_objects': True}
        self.bytes += len(e._json(rows).encode('utf-8')) + len(e._json(meta).encode('utf-8'))
        _limit(self.bytes, MAX_RESULT_BYTES, 'MAX_RESULT_BYTES')
        return e._store_result(self.db, 'query', rows, meta)

    def condition(self, condition, index, event):
        status, items, lo, hi = _window(index, event, condition['before'], condition['after'])
        count = hi - lo
        self.witnesses += count
        _limit(self.witnesses, MAX_WITNESS_ROWS, 'MAX_WITNESS_ROWS')
        ordered = sorted(items[lo:hi], key=lambda item: _witness_key(item, event['seq']))
        rows = []
        for n, (witness, anchor) in enumerate(ordered):
            flag = witness['system_generated']
            rows.append({'row': n, 'ref': witness['ref'], 'anchor': anchor,
                         'delta_seq': witness['seq'] - event['seq'],
                         'kind': witness['kind'], 'role': witness['role'], 'tool': witness['tool'],
                         'system_generated': None if flag is None else bool(flag)})
        result = self.query(rows, WITNESS_COLUMNS) if rows else None
        return {'name': condition['name'], 'status': status,
                'before': condition['before'], 'after': condition['after'],
                'witness_count': count, 'witnesses_result': result, 'witnesses': rows[:2]}


def _page(db, result, offset, page_size, max_chars):
    kind, rows, meta = e._get_result(db, result)
    if kind != 'query' or 'cooccur' not in meta:
        raise ValueError('not a cooccur result')
    page, end = e._display_page(rows, offset, page_size, max_chars)
    return {'operation': 'cooccur', 'status': 'complete', 'result': result,
            **meta['cooccur'], 'rows': page, 'row_count': len(rows),
            'next_cursor': e._cursor(db, 'cooccur', {'result': result, 'offset': end}) if end < len(rows) else None}


def trace_cooccur(db, workspace, seed=None, against=None, page_size=10,
                  max_chars=6000, cursor=None, saved=None, save_as=None):
    """Join exact retained anchors by inclusive seed-relative event windows.

    Dispatch owns the transaction: any validation or safety-limit error rolls
    back every result, cursor, and recipe created by this operation.
    """
    e._positive(page_size, 'page_size')
    e._positive(max_chars, 'max_chars')
    if cursor is not None:
        if any(value is not None for value in (seed, against, saved, save_as)):
            raise ValueError('cursor cannot accompany seed, against, saved, or save_as')
        state = e._uncursor(db, cursor, 'cooccur')
        return _page(db, state['result'], state['offset'], page_size, max_chars)
    if save_as is not None:
        _name(save_as, 'save_as')
    if saved is not None:
        _name(saved, 'saved')
        if seed is not None or against is not None:
            raise ValueError('saved cannot accompany seed or against')
        recipe = e._saved(db, saved, 'cooccur')
        seed, against = recipe['seed'], recipe['against']
    _selector(seed)
    _conditions(against)
    membership = _Membership(db)
    seeds, info = membership.select('seed', seed)
    inputs, indexes = [info], []
    for condition in against:
        events, info = membership.select(condition['name'], condition['selection'])
        inputs.append(info)
        indexes.append(_index(events))
    results = _Results(db)
    matched, unmatched = [], []
    ordered = sorted(seeds.values(), key=lambda g: (g['event']['session'], g['event']['seq'], g['event']['ref']))
    for group in ordered:
        event, anchors = group['event'], group['anchors']
        conditions = [results.condition(condition, index, event) for condition, index in zip(against, indexes)]
        failed = [c['name'] for c in conditions if c['status'] != 'within_window']
        destination = unmatched if failed else matched
        destination.append({'row': len(destination), 'ref': event['ref'], 'session': event['session'],
                            'seq': event['seq'], 'anchor': anchors[0], 'seed_anchor_count': len(anchors),
                            'conditions': conditions, 'failed_conditions': failed, '_anchors': anchors})
    unmatched_result = results.query(unmatched, SEED_COLUMNS)
    state = {'unmatched_result': unmatched_result,
             'unmatched_cursor': e._cursor(db, 'query', {'result': unmatched_result, 'offset': 0}) if unmatched else None,
             'counts': {'seed_events': len(seeds), 'matched_seed_events': len(matched),
                        'unmatched_seed_events': len(unmatched)}, 'inputs': inputs}
    result = results.query(matched, SEED_COLUMNS, {'cooccur': state})
    e._save_query(db, save_as, 'cooccur', {'seed': seed, 'against': against}, result)
    if saved is not None:
        db.execute('UPDATE saved_queries SET last_result=? WHERE name=?', (result, saved))
    return _page(db, result, 0, page_size, max_chars)
