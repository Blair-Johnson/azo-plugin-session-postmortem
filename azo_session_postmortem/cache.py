"""Complete native delivery with persistent, workspace-scoped evidence aliases.

The legacy engine owns selection semantics. This adapter never follows display
cursors: private materialization hooks return retained selections or a read plan.
Only presentation is delegated to cache_render; neither module needs the host.
"""
import json
from pathlib import Path
import re
import sqlite3

from . import engine as e
from . import render as r


SCHEMA = '''
CREATE TABLE IF NOT EXISTS _cache_state(generation INTEGER NOT NULL);
INSERT INTO _cache_state SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM _cache_state);
CREATE TABLE IF NOT EXISTS _cache_documents(
    number INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, operation TEXT,
    generation INTEGER, targets TEXT, groups_json TEXT, document TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS _cache_items(
    id TEXT PRIMARY KEY, document_id TEXT, generation INTEGER, targets TEXT);
CREATE TABLE IF NOT EXISTS _cache_results(result TEXT PRIMARY KEY,generation INTEGER);
CREATE TABLE IF NOT EXISTS _cache_recipes(name TEXT PRIMARY KEY,context_lines INTEGER);
'''
PREFIX = {'trace_search': 'S', 'trace_query': 'Q', 'trace_read': 'R',
          'trace_cooccur': 'C', 'trace_open': 'O'}
ALIAS = re.compile(r'[SQRCO]\d+(?:-[A-Z]\d+)?\Z')
ALLOWED = {
    'trace_open': {'sessions', 'descendants', 'refresh'},
    'trace_query': {'sql', 'params', 'sample', 'saved', 'save_as'},
    'trace_search': {'query', 'scope', 'mode', 'context_lines', 'on', 'collapse',
                     'projection', 'follow_object_links', 'saved', 'save_as'},
    'trace_read': {'targets', 'before', 'after', 'pair', 'line_context', 'projection'},
    'trace_cooccur': {'seed', 'against', 'saved', 'save_as'},
    'trace_edit': {'put', 'delete', 'link', 'unlink'},
}


def _generation(db):
    return db.execute('SELECT generation FROM _cache_state').fetchone()[0]


def _fresh(db, generation):
    if generation != _generation(db):
        raise ValueError('stale cache selection: sources were reloaded; create a new selection')


def _result(db, result):
    tracked = e._row(db, 'SELECT generation FROM _cache_results WHERE result=?', (result,))
    if tracked:
        _fresh(db, tracked['generation'])
    return e._get_result(db, result)


def _alias(db, name, group=None, indexes=None):
    if not isinstance(name, str):
        raise ValueError('invalid cache alias')
    item = e._row(db, 'SELECT * FROM _cache_items WHERE id=?', (name,))
    if item:
        if group is not None or indexes is not None:
            raise ValueError('item aliases do not accept group or rows')
        _fresh(db, item['generation'])
        targets = json.loads(item['targets'])
        if not targets:
            raise ValueError('item contains non-navigable rows, not source evidence: ' + name)
        return targets
    document = e._row(db, 'SELECT * FROM _cache_documents WHERE id=?', (name,))
    if not document:
        raise ValueError('unknown cache alias in this workspace: ' + name)
    _fresh(db, document['generation'])
    if group is None:
        targets = json.loads(document['targets'])
    else:
        groups = json.loads(document['groups_json'])
        if group not in groups:
            raise ValueError('unknown result group: ' + str(group))
        targets = groups[group]
    if indexes is not None:
        # Rows address the chosen group's original retained rows, not buffer lines.
        rows = _membership_targets(db, targets)
        picked = []
        for index in e._list(indexes, 'rows'):
            e._positive(index, 'row', zero=True)
            if index >= len(rows):
                raise ValueError('result row out of range')
            picked.append(rows[index])
        return picked
    return targets


def _targets(db, values):
    """Resolve aliases recursively without widening their retained membership."""
    def expand(values, trail=()):
        out = []
        def alias(name, group=None, indexes=None):
            if name in trail:
                raise ValueError('cyclic cache alias: ' + ' -> '.join((*trail, name)))
            if len(trail) >= 64:
                raise ValueError('cache alias nesting exceeds 64 levels')
            return expand(_alias(db, name, group, indexes), (*trail, name))

        for value in e._list(values, 'targets'):
            if isinstance(value, str) and ALIAS.fullmatch(value):
                out.extend(alias(value))
            elif isinstance(value, dict) and 'items' in value:
                if set(value) != {'items'}:
                    raise ValueError('items selector accepts items only')
                items = e._list(value['items'], 'items')
                if any(not isinstance(item, str) for item in items):
                    raise ValueError('items must contain alias strings')
                for item in items:
                    out.extend(alias(item))
            elif isinstance(value, dict) and 'result' in value:
                if set(value) - {'result', 'rows', 'group'}:
                    raise ValueError('invalid result target')
                if 'rows' in value:
                    e._list(value['rows'], 'rows')
                if 'group' in value and not isinstance(value['group'], str):
                    raise ValueError('group must be a string')
                result = value['result']
                if isinstance(result, str) and ALIAS.fullmatch(result):
                    out.extend(alias(result, value.get('group'), value.get('rows')))
                else:
                    if 'group' in value:
                        raise ValueError('group requires a public cache result ID')
                    _result(db, result)
                    out.append(value)
            else:
                out.append(value)
        return out
    return expand(values)


def _membership_targets(db, targets):
    """Flatten row identities, not anchors: each result keeps its own metadata."""
    identities = []
    for target in targets:
        if isinstance(target, dict) and 'result' in target:
            _, rows, _ = _result(db, target['result'])
            indexes = target.get('rows', range(len(rows)))
            if 'rows' in target:
                e._list(indexes, 'rows')
            for index in indexes:
                e._positive(index, 'row', zero=True)
                if index >= len(rows):
                    raise ValueError('result row out of range')
                identities.append({'result': target['result'], 'rows': [index]})
        else:
            identities.append(target)
    return identities


def _membership_rows(db, targets):
    rows = []
    for target in targets:
        if isinstance(target, dict) and 'result' in target:
            _, selected, _ = _result(db, target['result'])
            indexes = target.get('rows', range(len(selected)))
            if 'rows' in target:
                e._list(indexes, 'rows')
            for index in indexes:
                e._positive(index, 'row', zero=True)
                if index >= len(selected):
                    raise ValueError('result row out of range')
                row = selected[index]
                if not e._row_targets(row):
                    raise ValueError('result contains non-navigable rows, not source evidence')
                rows.append(row)
        else:
            rows.append({'_anchors': [target]})
    return rows


def _selection(db, selector):
    if not isinstance(selector, dict) or not ({'items', 'result'} & set(selector)):
        raise ValueError('selection requires result or items')
    targets = _targets(db, [selector])
    rows = _membership_rows(db, targets)  # Aggregate-only rows fail, never disappear.
    if len(targets) == 1 and isinstance(targets[0], dict) and 'result' in targets[0]:
        return targets[0]
    result = e._store_result(db, 'query', rows, {'columns': [], 'sampling': None})
    db.execute('INSERT INTO _cache_results VALUES(?,?)', (result, _generation(db)))
    return {'result': result}


def _scope(db, scope, trusted=False):
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ValueError('scope must be an object')
    if 'items' in scope:
        if set(scope) != {'items'}:
            raise ValueError('scope must specify exactly one selector')
        return {'items': _targets(db, scope['items'] if trusted else [scope])}
    if 'refs' in scope:
        if set(scope) != {'refs'}:
            raise ValueError('scope must specify exactly one selector')
        return {'refs': _targets(db, scope['refs'])}
    if 'result' in scope:
        targets = _targets(db, [scope])
        if len(targets) == 1 and set(targets[0]) == {'result'}:
            return targets[0]
        return {'items': targets}
    return scope


def _retained(db, rows, noimplicit=False):
    result = e._store_result(db, 'query', rows, {
        'columns': [], 'sampling': None, '_search_no_implicit_objects': noimplicit})
    db.execute('INSERT INTO _cache_results VALUES(?,?)', (result, _generation(db)))
    return {'result': result}


def _freeze_targets(db, targets):
    """Freeze source membership, retaining each nested result's search policy."""
    frozen = []
    for target in targets:
        if isinstance(target, dict) and 'result' in target:
            _, rows, meta = _result(db, target['result'])
            indexes = target.get('rows', range(len(rows)))
            selected = []
            for index in indexes:
                e._positive(index, 'row', zero=True)
                if index >= len(rows):
                    raise ValueError('result row out of range')
                anchors = e._row_targets(rows[index])
                if not anchors:
                    raise ValueError('result contains non-navigable rows')
                selected.append({'_anchors': _freeze_targets(db, anchors)})
            frozen.append(_retained(db, selected, meta.get('_search_no_implicit_objects', False)))
        else:
            frozen.extend(e._expand(db, e._resolve(db, [target])))
    return e._unique(frozen)


def _request_groups(db, groups, projection):
    """Index original request ownership without reordering the canonical read plan."""
    ownership = []
    for group, targets in enumerate(groups):
        anchors = e._expand(db, e._resolve(db, targets))
        cards = {a['entry'] for a in anchors if isinstance(a, dict) and 'entry' in a}
        sources = [a for a in anchors if not (isinstance(a, dict) and 'entry' in a)]
        slices = [a for a in sources if isinstance(a, dict) and 'lines' in a]
        whole = [a for a in sources if a not in slices]
        for priority, selected in enumerate((slices, whole)):
            ownership.append((group, cards, {e._ref(a) for a in selected},
                              e._source_windows(db, selected, False, projection), priority))
    # Match the canonical planner's explicit-slice-before-whole-target priority.
    return sorted(ownership, key=lambda item: (
        item[-1], sum(hi - lo + 1 for ranges in item[3].values() for lo, hi in ranges), item[0]))


def _request_group(block, ownership):
    for group, cards, refs, windows, _ in ownership:
        if block.get('entry_id') in cards:
            return group
        if block.get('available') is False and block.get('ref') in refs:
            return group
        anchor = block.get('anchor', {})
        if isinstance(anchor, dict) and 'lines' in anchor:
            lo, hi = anchor['lines']
            if any(a <= hi and lo <= b for a, b in windows.get((block['ref'], block['field']), [])):
                return group
    raise ValueError('requested passage has no original target')


def _group_read_plan(plan, ownership):
    """Keep canonical source order; split merged windows only at request boundaries."""
    for block in plan:
        if not block['requested']:
            yield block
            continue
        anchor = block.get('anchor', {})
        if not isinstance(anchor, dict) or 'lines' not in anchor or 'text' not in block:
            yield dict(block, request_group=_request_group(block, ownership))
            continue
        lo, hi = anchor['lines']
        cuts = {lo, hi + 1}
        for _, _, _, windows, _ in ownership:
            for start, end in windows.get((block['ref'], block['field']), []):
                cuts.update(point for point in (start, end + 1) if lo < point <= hi)
        spans = []
        boundaries = sorted(cuts)
        for start, end in zip(boundaries, boundaries[1:]):
            piece = dict(block, anchor=dict(anchor, lines=[start, end - 1]))
            group = _request_group(piece, ownership)
            if spans and spans[-1][2] == group:
                spans[-1][1] = end
            else:
                spans.append([start, end, group])
        lines = block['text'].splitlines(keepends=True) or ['']
        for start, end, group in spans:
            yield dict(block, anchor=dict(anchor, lines=[start, end - 1]),
                       text=''.join(lines[start - lo:end - lo]), line_start=start,
                       request_group=group)

def _source_title(db, ref):
    source = e._row(db, 'SELECT role,tool,kind FROM events WHERE ref=?', (ref,))
    if source is None:
        source = e._row(db, 'SELECT kind FROM objects WHERE ref=?', (ref,)) or {}
    labels = list(dict.fromkeys(source.get(key) for key in ('role', 'tool', 'kind') if source.get(key)))
    return ref + (' · ' + ' / '.join(labels) if labels else '')


def _labeled(values):
    """Generated presentation, not an opaque serialized metadata wrapper."""
    return '\n'.join(f'{key}: {r._value(value)}' for key, value in values.items())


def _card_text(card):
    # Put the investigator's own continuation text ahead of generated metadata.
    note = card.get('note')
    return r._join([note or '', r._card({**card, 'note': None})])


def _window_label(condition):
    return f"[-{condition['before']}, +{condition['after']}]"


def _slice(db, anchor, context=0):
    ref = e._ref(anchor)
    field = anchor.get('field', 'text') if isinstance(anchor, dict) else 'text'
    text = e._field_text(db, ref, field)
    if text is None:
        return ''
    if not isinstance(anchor, dict) or 'lines' not in anchor:
        return text
    lines = text.splitlines(keepends=True) or ['']
    lo, hi = anchor['lines']
    return ''.join(lines[max(0, lo - 1 - context):hi + context])


def _excerpt(db, anchor):
    text = _slice(db, anchor)
    return text[:240] + ('…' if len(text) > 240 else '')


class _Document:
    def __init__(self, db, workspace, operation):
        self.db = db
        self.generation = _generation(db)
        self.number = db.execute('INSERT INTO _cache_documents(operation,generation) VALUES(?,?)',
                                 (operation, self.generation)).lastrowid
        self.id = PREFIX[operation] + str(self.number)
        self.document = {'id': self.id, 'operation': operation, 'workspace': str(workspace),
                         'title': operation, 'blocks': []}
        self.counters = {}
        self.targets = []
        self.groups = {}

    def block(self, code, title, text, group, targets=(), requested=True, **metadata):
        self.counters[code] = self.counters.get(code, 0) + 1
        item = self.id + '-' + code + f'{self.counters[code]:03d}'
        block = {'id': item, 'title': title, 'text': text, 'group': group,
                 'requested': requested, **metadata}
        self.document['blocks'].append(block)
        self.db.execute('INSERT INTO _cache_items VALUES(?,?,?,?)',
                        (item, self.id, self.generation, e._json(list(targets))))
        return block

    def finish(self):
        self.db.execute('UPDATE _cache_documents SET id=?,targets=?,groups_json=?,document=?,path=? '
                        'WHERE number=?', (self.id, e._json(self.targets), e._json(self.groups),
                         e._json(self.document), 'results/' + self.id + '.txt', self.number))
        return self.document


def _query(db, doc, args):
    result = e.trace_query(db, doc.document['workspace'], _materialize=True, **args)
    _, rows, meta = _result(db, result)
    doc.targets = [{'result': result}]
    description = 'Columns: ' + ', '.join(meta['columns'])
    if meta.get('sampling'):
        description += '\nSampling: ' + r._value(meta['sampling'])
    doc.document.update(title=f'Query: {len(rows)} rows', counts={'rows': len(rows)},
                        description=description)
    for index, row in enumerate(rows):
        doc.block('R', f'Row {index}', _labeled({key: row[key] for key in meta['columns']}),
                  'row', [{'result': result, 'rows': [index]}])
    return result


def _search(db, doc, args, context):
    result = e.trace_search(db, doc.document['workspace'], _materialize=True, **args)
    _, rows, meta = _result(db, result)
    doc.targets = [{'result': result}]
    doc.document.update(title=f'Search: {len(rows)} hits', counts=meta['matched_total'],
                        description=f"Mode: {args.get('mode', 'terms')} · context: {context} lines")
    for index, row in enumerate(rows):
        selection = [{'result': result, 'rows': [index]}]
        if row['origin'] == 'map':
            card = e._card(db, row['entry_id'])
            doc.block('M', f"Map {card['id']} · {row['matched_field']}", _card_text(card), 'map', selection)
            continue
        anchor = row['anchor']
        block = doc.block('H', f"{_source_title(db, row['ref'])} · {row['matched_field']} · lines {anchor['lines']}",
                          _slice(db, anchor, context), 'hit', selection, anchor=anchor,
                          match_lines=anchor['lines'], context_lines=context,
                          excerpt=row['excerpt'])
        if 'occurrences_result' in row:
            occurrence_result = row['occurrences_result']
            _, occurrences, _ = _result(db, occurrence_result)
            links = []
            for number, occurrence in enumerate(occurrences):
                child = doc.block('O', f"Occurrence {_source_title(db, occurrence['ref'])} · {occurrence['anchor']['lines']}",
                                  _slice(db, occurrence['anchor'], context), 'occurrence',
                                  [{'result': occurrence_result, 'rows': [number]}],
                                  requested=False, anchor=occurrence['anchor'])
                links.append({'id': child['id'], 'label': 'occurrence',
                              'text': _excerpt(db, occurrence['anchor'])})
            block['links'] = links
    for notice in meta.get('unavailable', []):
        anchor = notice['anchor']
        ref = e._ref(anchor)
        doc.block('U', 'Unavailable source ' + ref,
                  f"Not searched: {notice['reason']}",
                  'coverage', [anchor], requested=False)
    doc.document['blocks'].sort(key=lambda block: block['group'] == 'occurrence')
    return result


def _read(db, doc, args):
    targets = args.pop('targets', None)
    if targets is None:
        raise ValueError('targets are required')
    groups = [_freeze_targets(db, _targets(db, [target]))
              for target in e._list(targets, 'targets')]
    targets = [target for group in groups for target in group]
    before, after, context = (args.get('before', 0), args.get('after', 0), args.get('line_context', 10))
    for name, value in (('before', before), ('after', after), ('line_context', context)):
        e._positive(value, name, zero=True)
    pair = args.get('pair', True)
    if not isinstance(pair, bool):
        raise ValueError('pair must be a boolean')
    projection = e._projection(args.get('projection', 'content'))
    plan = e._read_plan(db, targets, before, after, pair, context, projection)
    doc.targets = targets
    ownership = _request_groups(db, groups, projection)
    plan = list(_group_read_plan(plan, ownership))
    refs, requested = [], {}
    for block in plan:
        anchor = block.get('anchor', {'entry': block.get('entry_id'), 'part': 'card'})
        body = block.get('text', _card_text(block['card']) if 'card' in block else block.get('reason', 'Unavailable'))
        metadata = {key: value for key, value in block.items() if key not in ('text', 'card', 'requested', 'anchor')}
        delivered = doc.block('B', (_source_title(db, block['ref']) if block.get('ref') else 'Map ' + block['entry_id']) +
                  (' · ' + block['field'] if 'field' in block else ''), body,
                  'evidence' if block['requested'] else 'context', [anchor],
                  requested=block['requested'], anchor=anchor, **metadata)
        if block.get('ref') is not None:
            refs.append(block['ref'])
            if block['requested']:
                requested.setdefault(block['ref'], []).append(delivered)
    relations = e._unique([row for ref in set(refs) for row in e._rows(db,
        'SELECT * FROM links WHERE src=? OR dst=? ORDER BY kind,src,dst', (ref, ref))])
    for row in relations:
        doc.block('N', 'Relation ' + row['kind'], r._link(row), 'navigation',
                  [row['src'], row['dst']], requested=False)
    navigation = e.navigation_rows(db, list(dict.fromkeys(refs)))
    for row in navigation:
        child = doc.block('N', 'Session navigation', r._navigation(row), 'navigation',
                          [row['anchor']], requested=False)
        for block in requested.get(row['src'], []):
            block.setdefault('links', []).append({
                'id': child['id'], 'label': 'Child launch', 'text': r._navigation(row)})
    doc.document.update(title=f'Read: {len(plan)} passages',
                        counts={'passages': len(plan), 'relations': len(relations), 'navigation': len(navigation)})


def _cooccur(db, doc, args):
    from .cooccur import trace_cooccur
    args['seed'] = _selection(db, args.get('seed'))
    against = e._list(args.get('against'), 'against')
    args['against'] = [dict(condition, selection=_selection(db, condition.get('selection')))
                       if isinstance(condition, dict) else condition for condition in against]
    result = trace_cooccur(db, doc.document['workspace'], _materialize=True, **args)
    _, matched, meta = _result(db, result)
    state = meta['cooccur']
    unmatched_result = state['unmatched_result']
    _, unmatched, _ = _result(db, unmatched_result)
    doc.targets = [{'result': result}]
    doc.groups = {'matched': doc.targets, 'unmatched': [{'result': unmatched_result}], 'witnesses': []}
    doc.document.update(title=f"Cooccurrence: {len(matched)} matched, {len(unmatched)} unmatched",
                        counts=state['counts'], description='Conditions: ' + '; '.join(
                            condition['name'] + ' ' + _window_label(condition) for condition in args['against']))
    witness_blocks = []
    for code, group, rows, retained in (('M', 'matched', matched, result),
                                        ('U', 'unmatched', unmatched, unmatched_result)):
        for index, row in enumerate(rows):
            links = []
            title = f"{row['ref']} · seq {row['seq']} · {group}"
            missing = [f"{condition['name']}: no witness in session · {_window_label(condition)}"
                       for condition in row['conditions'] if condition['witnesses_result'] is None]
            if missing:
                title += ' · ' + '; '.join(missing)
            block = doc.block(code, title, _excerpt(db, row['anchor']), group,
                              [{'result': retained, 'rows': [index]}], anchor=row['anchor'])
            for condition in row['conditions']:
                witness_result = condition['witnesses_result']
                if witness_result is None:
                    continue
                doc.groups['witnesses'].append({'result': witness_result})
                _, witnesses, _ = _result(db, witness_result)
                for number, witness in enumerate(witnesses):
                    excerpt = _excerpt(db, witness['anchor'])
                    status = 'nearest outside window' if condition['status'] == 'outside_window' else 'within window'
                    label = (f"{condition['name']} · {witness['delta_seq']:+d} · "
                             f"{status} {_window_label(condition)}")
                    child = doc.block('W', f"{witness['ref']} · {label}",
                                      excerpt, 'witness',
                                      [{'result': witness_result, 'rows': [number]}],
                                      requested=False, anchor=witness['anchor'])
                    witness_blocks.append(child)
                    links.append({'id': child['id'], 'label': label, 'text': excerpt})
            block['links'] = links
    # Requested seeds precede their witness expansion, without losing any relation.
    doc.document['blocks'] = [b for b in doc.document['blocks'] if b['group'] != 'witness'] + witness_blocks
    return result


def _open(db, doc, args):
    state = e.trace_open(db, doc.document['workspace'], _materialize=True, **args)
    # trace_open can reload sources; allocate aliases in the resulting generation.
    doc.generation = _generation(db)
    db.execute('UPDATE _cache_documents SET generation=? WHERE number=?', (doc.generation, doc.number))
    doc.document.update(title=f"Workspace: {len(state['sources'])} sessions", counts=state['counts'])
    cards = [e._card(db, row['id']) for row in e._rows(db,
             'SELECT id FROM map_entries ORDER BY pinned DESC,revision DESC,id')]
    for card in cards:
        targets = [{'entry': card['id'], 'part': 'card'}]
        doc.targets.extend(targets)
        doc.block('P' if card['pinned'] else 'M', 'Map ' + card['id'] + ' · ' + card['title'],
                  _card_text(card), 'pinned' if card['pinned'] else 'map', targets)
    for source in state['sources']:
        events = [row['ref'] for row in e._rows(db,
                  'SELECT ref FROM events WHERE session=? ORDER BY seq', (source['id'],))]
        objects = [row['ref'] for row in e._rows(db,
                   'SELECT ref FROM objects WHERE session=? ORDER BY ref', (source['id'],))]
        targets = [_retained(db, [{'_anchors': [ref]} for ref in events + objects], True)]
        doc.targets.extend(targets)
        details = {'Session': source['id'], 'Title': source['title'] or '',
                   'Source': source['source'], 'Events': len(events), 'Objects': len(objects)}
        if source['parent_id']:
            details['Parent'] = source['parent_id']
        if source['warnings']:
            details['Source notes'] = source['warnings']
        doc.block('S', 'Session ' + source['id'] + ' · ' + (source['title'] or ''),
                  _labeled(details), 'session', targets)
    for relation in e._rows(db, 'SELECT * FROM map_links ORDER BY id'):
        doc.block('M', 'Map relation ' + relation['id'], r._link(relation), 'map',
                  [{'entry': relation[key], 'part': 'card'} for key in ('src', 'dst')])
    for recipe in state['saved_queries']:
        details = {'Operation': recipe['tool'], **recipe['arguments']}
        if recipe['last_result']:
            details['Last selection'] = recipe['last_result']
        doc.block('Q', 'Recipe ' + recipe['name'], _labeled(details), 'recipe',
                  [{'result': recipe['last_result']}] if recipe['last_result'] else [])
    for prior in e._rows(db, 'SELECT id,operation,path,generation FROM _cache_documents '
                            'WHERE id IS NOT NULL ORDER BY number DESC'):
        details = {'Operation': prior['operation'],
                   'Full text': str(Path(doc.document['workspace']) / prior['path']),
                   'Selection': prior['id']}
        if prior['generation'] != doc.generation:
            details['Evidence'] = 'stale selection; full text remains available'
        doc.block('R', 'Prior result ' + prior['id'], _labeled(details), 'result',
                  [prior['id']])


def _saved_arguments(db, tool, args):
    saved = args.pop('saved', None)
    if saved is None:
        return args, None, None
    allowed = {'save_as', 'context_lines'} if tool == 'trace_search' else {'save_as'}
    if set(args) - allowed:
        raise ValueError('saved cannot accompany a new selection')
    recipe = e._saved(db, saved, tool.removeprefix('trace_'))
    context = None
    if tool == 'trace_search':
        row = e._row(db, 'SELECT context_lines FROM _cache_recipes WHERE name=?', (saved,))
        context = row['context_lines'] if row else 2
        recipe.pop('top_k', None)  # Legacy display control never truncates selection.
        recipe.setdefault('projection', 'all')
    return {**recipe, **args}, saved, context


def dispatch(tool, arguments):
    """Return the indexed-delivery envelope; preview budget never limits evidence."""
    if tool not in ALLOWED:
        raise ValueError('unknown trace tool: ' + str(tool))
    if not isinstance(arguments, dict):
        raise ValueError('arguments must be an object')
    args = dict(arguments)
    preview = args.pop('preview_chars', None)
    preview = (12000 if tool == 'trace_read' else 6000) if preview is None else preview
    e._positive(preview, 'preview_chars', zero=True)  # Before mkdir, DB, or source I/O.
    unknown = set(args) - ALLOWED[tool] - {'workspace'}
    if unknown:
        raise ValueError('unknown arguments: ' + ', '.join(sorted(unknown)))
    workspace = args.pop('workspace', None)
    if not isinstance(workspace, (str, Path)) or not str(workspace):
        raise ValueError('workspace is required and must be a directory path')
    workspace = Path(workspace).expanduser().resolve()
    args = {key: value for key, value in args.items() if value is not None}
    if 'context_lines' in args:
        e._positive(args['context_lines'], 'context_lines', zero=True)
    path = workspace / 'trace.sqlite3'
    if tool == 'trace_open':
        workspace.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ValueError('workspace is not initialized; call trace_open first')
    db = None
    output_path = temporary = None
    try:
        db = sqlite3.connect(str(path))
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        if tool == 'trace_open':
            db.executescript(e.SCHEMA)
        # CREATEs are transactional; no executescript implicit commit in operations.
        with db:
            db.execute('BEGIN')
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    db.execute(statement)
            e._ensure_system_generated(db)
            e.ensure_session_links(db)
            first_result = db.execute('SELECT coalesce(max(id),0) FROM _results').fetchone()[0]
            if tool == 'trace_edit':
                patches = []
                for patch in e._list(args.get('put', []), 'put'):
                    if not isinstance(patch, dict):
                        raise ValueError('invalid map entry fields')
                    patch = dict(patch)
                    for key in ('anchors', 'add_anchors', 'remove_anchors'):
                        if key in patch:
                            patch[key] = _targets(db, patch[key])
                    patches.append(patch)
                args['put'] = patches
                ack = e.trace_edit(db, workspace, **args)
                return {'document': {'operation': tool, 'ack': ack}, 'preview_chars': preview}
            saved = context = None
            if tool in ('trace_query', 'trace_search', 'trace_cooccur'):
                args, saved, context = _saved_arguments(db, tool, args)
            doc = _Document(db, workspace, tool)
            result = None
            if tool == 'trace_open':
                _open(db, doc, args)
            elif tool == 'trace_query':
                result = _query(db, doc, args)
            elif tool == 'trace_search':
                context = args.pop('context_lines', 2 if context is None else context)
                args['scope'] = _scope(db, args.get('scope'), trusted=saved is not None)
                if 'follow_object_links' in args:
                    args['include_references'] = args.pop('follow_object_links')
                result = _search(db, doc, args, context)
                for name in (saved, args.get('save_as')):
                    if name:
                        db.execute('INSERT OR REPLACE INTO _cache_recipes VALUES(?,?)', (name, context))
            elif tool == 'trace_read':
                _read(db, doc, args)
            elif tool == 'trace_cooccur':
                result = _cooccur(db, doc, args)
            if saved:
                db.execute('UPDATE saved_queries SET last_result=? WHERE name=?', (result, saved))
            db.execute("INSERT OR IGNORE INTO _cache_results SELECT 'r' || id,? FROM _results WHERE id>?",
                       (_generation(db), first_result))
            if result is not None:
                doc.document['sql_result'] = result
            document = doc.finish()
            from .cache_render import render_full
            text = render_full(document)
            directory = workspace / 'results'
            directory.mkdir(exist_ok=True)
            output_path = directory / (doc.id + '.txt')
            temporary = directory / (doc.id + '.tmp')
            with temporary.open('w', encoding='utf-8', newline='') as stream:
                stream.write(text)
            temporary.replace(output_path)
        return {'document': document, 'text': text, 'path': str(output_path), 'preview_chars': preview}
    except (sqlite3.Error, OSError, TypeError, KeyError) as exc:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        raise ValueError(str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if db is not None:
            db.close()
