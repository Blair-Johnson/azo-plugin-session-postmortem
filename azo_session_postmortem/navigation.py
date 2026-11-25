"""Evidence-backed navigation for recorded public ``submit_rlm`` receipts.

These are observed associations in the indexed source, including copied ancestry;
not canonical ownership, independent launches, completion, or output validation.
Only SQLite is consulted. Destination sessions are never discovered or loaded.
"""
import json
import re


_VERSION = 2
_CHILD_ID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|rlm-[0-9A-Za-z_-]+"
_HEADER = re.compile(r"Submitted RLM task (rlm_job_[0-9]+) with profile [^\r\n]+ \([^\r\n]+\)\.")
_CHILD = re.compile(
    r"Child live session: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|rlm-[0-9]+)"
)
_RESUME = re.compile(r"Resuming completed task: rlm_job_[0-9]+")
_INPUT = re.compile(r"- \S+(?: \(from [^\r\n]+\))? \[(?:ready|waiting)\]")
_OUTPUT = re.compile(r"- ([^\r\n]+) -> (buf_[A-Za-z0-9_]+) \[reserved\]")

# 0.4.0 format: agent_zoo.features.rlm.tools.SubmitRLM.call
_HEADER_V04 = re.compile(
    r"Submitted RLM (rlm_[A-Za-z0-9_-]+) \([^\r\n]+\)(?: \| name=[^\r\n]+)?"
)
_CHILD_V04 = re.compile(rf"(?:Child live session|child): ({_CHILD_ID_PATTERN})")
_RESUME_V04 = re.compile(r"resuming [A-Za-z0-9_-]+: rlm_[A-Za-z0-9_-]+")
_INPUT_V04_ITEM = re.compile(r"\S+(?: \(from [^\r\n,]+\))? \[(?:ready|waiting)\]")
_OUTPUT_V04_ITEM = re.compile(r"([^\r\n,]+?) -> (buf_[A-Za-z0-9_]+)")
_STATUS_V04 = re.compile(r"status: waiting for depth-[0-9]+ concurrency capacity")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _receipt_legacy(lines):
    """Parse legacy ``agent_zoo.tools.rlm.RLMToolHandler._submit_public_tool`` receipts."""
    if len(lines) < 3:
        return None
    header, child = _HEADER.fullmatch(lines[0]), _CHILD.fullmatch(lines[1])
    if not header or not child:
        return None
    job_id = header[1]
    footer = (f"Use rlm_status(job_id='{job_id}') to inspect it or "
              f"cancel_rlm(job_id='{job_id}') to kill it.")
    if lines[-1] != footer:
        return None
    pos = 2
    if _RESUME.fullmatch(lines[pos]):
        pos += 1
    child_end = pos
    for prefix in ('Task name (/attach label): ', 'Working directory: '):
        if lines[pos].startswith(prefix) and len(lines[pos]) > len(prefix):
            pos += 1
    if lines[pos] == 'Input buffers:':
        pos += 1
        start = pos
        while pos < len(lines) - 1 and _INPUT.fullmatch(lines[pos]):
            pos += 1
        if pos == start:
            return None
    outputs = []
    if lines[pos] == 'Output buffers reserved:':
        pos += 1
        names, handles = set(), set()
        while pos < len(lines) - 1:
            output = _OUTPUT.fullmatch(lines[pos])
            if not output or output[1] in names or output[2] in handles:
                return None
            names.add(output[1])
            handles.add(output[2])
            outputs.append((output[1], output[2], pos + 1))
            pos += 1
        if not outputs:
            return None
    if pos != len(lines) - 1:
        return None
    return job_id, child[1], child_end, outputs


def _receipt_v04(lines):
    """Parse 0.4.0 ``agent_zoo.features.rlm.tools.SubmitRLM.call`` receipts."""
    if not lines:
        return None
    header = _HEADER_V04.fullmatch(lines[0])
    if not header:
        return None
    job_id = header[1]
    pos = 1
    child = None
    if pos < len(lines):
        child_match = _CHILD_V04.fullmatch(lines[pos])
        if child_match:
            child = child_match[1]
            pos += 1
    if pos < len(lines) and _RESUME_V04.fullmatch(lines[pos]):
        pos += 1
    child_end = pos
    if pos < len(lines) and lines[pos].startswith('cwd: ') and len(lines[pos]) > 5:
        pos += 1
    if pos < len(lines) and lines[pos].startswith('inputs: '):
        raw_inputs = lines[pos][len('inputs: '):]
        parts = raw_inputs.split(', ') if raw_inputs else []
        if not parts or not all(_INPUT_V04_ITEM.fullmatch(part) for part in parts):
            return None
        pos += 1
    outputs = []
    if pos < len(lines) and lines[pos].startswith('outputs: '):
        raw_outputs = lines[pos][len('outputs: '):]
        parts = raw_outputs.split(', ') if raw_outputs else []
        if not parts:
            return None
        names, handles = set(), set()
        for part in parts:
            output = _OUTPUT_V04_ITEM.fullmatch(part)
            if not output or output[1] in names or output[2] in handles:
                return None
            names.add(output[1])
            handles.add(output[2])
            outputs.append((output[1], output[2], pos + 1))
        pos += 1
    if pos < len(lines) and _STATUS_V04.fullmatch(lines[pos]):
        pos += 1
    if pos != len(lines):
        return None
    return job_id, child, child_end, outputs


def _receipt(text):
    """Accept a complete legacy or 0.4.0 harness receipt, never a substring or status record."""
    lines = text.splitlines()
    if not lines:
        return None
    parsed = _receipt_legacy(lines)
    if parsed is not None:
        return parsed
    return _receipt_v04(lines)


def ensure_session_links(db, *, rebuild=False) -> None:
    """Create/backfill derived links once; rebuild after indexed sources change.

    Uses the caller's transaction, with no commit or source/map/result edits.
    ``anchor`` is canonical JSON {event, field: 'text', lines: [lo, hi]}.
    NULL output_name denotes a child link. Uniqueness includes the receipt
    anchor, so repeated observations and conflicting associations survive.
    ``job_id`` is local to the observed session (the anchor event's session),
    not a globally unique job identity.
    """
    db.execute('CREATE TABLE IF NOT EXISTS session_links ('
               'src TEXT NOT NULL,dst TEXT NOT NULL,kind TEXT NOT NULL,'
               'job_id TEXT NOT NULL,output_name TEXT,anchor TEXT NOT NULL)')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS _session_links_receipt ON "
               "session_links(src,dst,kind,job_id,coalesce(output_name,''),anchor)")
    db.execute('CREATE TABLE IF NOT EXISTS _session_links_meta ('
               'id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS _rlm_jobs ('
               'session TEXT NOT NULL,job_id TEXT NOT NULL,'
               'child_session_id TEXT NOT NULL,output_map TEXT,'
               'PRIMARY KEY(session,job_id))')
    version = db.execute('SELECT version FROM _session_links_meta WHERE id=1').fetchone()
    if not rebuild and version is not None and version['version'] == _VERSION:
        return
    db.execute("DELETE FROM session_links WHERE kind IN ('rlm_child','rlm_output')")
    rlm_jobs_by_key = {}
    for row in db.execute('SELECT session,job_id,child_session_id,output_map FROM _rlm_jobs'):
        try:
            output_map = json.loads(row['output_map']) if row['output_map'] else {}
        except (TypeError, ValueError):
            output_map = {}
        rlm_jobs_by_key[(row['session'], row['job_id'])] = (
            row['child_session_id'],
            output_map if isinstance(output_map, dict) else {},
        )
    # source.py emits these exact kind/role/tool values for normalized results.
    # Do not parse call arguments, previews, other tools, or arbitrary prose.
    receipts = db.execute(
        "SELECT e.ref,e.session,c.text FROM events e JOIN _content c ON c.ref=e.ref "
        "AND c.field='text' WHERE e.kind='result' AND e.role='tool' "
        "AND e.tool='submit_rlm' ORDER BY e.session,e.seq,e.ref"
    )
    for event in receipts:
        parsed = _receipt(event['text'])
        if parsed is None:
            continue
        job_id, child, child_end, outputs = parsed
        job_record = rlm_jobs_by_key.get((event['session'], job_id))
        if not child and job_record is not None:
            child = job_record[0]
        if not child:
            continue
        if not outputs and job_record is not None and job_record[1]:
            outputs = [(name, handle, child_end) for name, handle in sorted(job_record[1].items())]
        anchor = {'event': event['ref'], 'field': 'text', 'lines': [1, child_end]}
        rows = [(event['ref'], child, 'rlm_child', job_id, None, _json(anchor))]
        for name, handle, line in outputs:
            anchor = {'event': event['ref'], 'field': 'text', 'lines': [1, line]}
            rows.append((f"{event['session']}/o_{handle}", child, 'rlm_output',
                         job_id, name, _json(anchor)))
        db.executemany('INSERT OR IGNORE INTO session_links VALUES(?,?,?,?,?,?)', rows)
    db.execute('INSERT OR REPLACE INTO _session_links_meta VALUES(1,?)', (_VERSION,))


def navigation_rows(db, refs) -> list[dict]:
    """Return navigation for delivered event/object refs, after ensure_session_links.

    Direct rows retain session_links.src; an indirect row's src is the delivered
    event and via is its referenced output object. Only one existing 'references'
    hop from an indexed event to an indexed object is followed. Each receipt is
    preserved. ``anchor`` alone is the read target; ``dst`` is a session locator.

    title/indexed/recorded_parent_id are looked up from current indexed sessions.
    parent_status compares that metadata with observed_session, not ownership.
    ambiguous flags multiple distinct (observed_session, child, job) associations
    for the returned src, including associations through different output objects.
    rlm_output means declared output reservation even if object bytes exist.
    """
    rows = []
    for ref in sorted(set(refs)):
        rows.extend(dict(row) for row in db.execute(
            "SELECT * FROM session_links WHERE src=? AND kind IN ('rlm_child','rlm_output') "
            'ORDER BY kind,dst,job_id,output_name,anchor',
            (ref,)))
        for edge in db.execute(
                "SELECT s.* FROM events e JOIN links l ON l.src=e.ref "
                "JOIN objects o ON o.ref=l.dst JOIN session_links s ON s.src=o.ref "
                "WHERE e.ref=? AND l.kind='references' AND s.kind='rlm_output' "
                "ORDER BY s.src,s.dst,s.job_id,s.output_name,s.anchor", (ref,)):
            row = dict(edge)
            row['src'], row['via'] = ref, row['src']
            rows.append(row)
    children, observations, associations = {}, {}, {}
    for row in rows:
        row['anchor'] = json.loads(row['anchor'])
        witness = row['anchor']['event']
        if witness not in observations:
            event = db.execute('SELECT session FROM events WHERE ref=?', (witness,)).fetchone()
            observations[witness] = event['session'] if event else None
        row['observed_session'] = observations[witness]
        if row['dst'] not in children:
            children[row['dst']] = db.execute(
                'SELECT title,parent_id FROM sessions WHERE id=?', (row['dst'],)).fetchone()
        child = children[row['dst']]
        row['indexed'] = child is not None
        row['title'] = child['title'] if child else None
        parent = child['parent_id'] if child else None
        row['recorded_parent_id'] = parent
        row['parent_status'] = ('unknown' if not parent or row['observed_session'] is None
                                else 'match' if parent == row['observed_session'] else 'mismatch')
        row['association'] = ('declared_output' if row['kind'] == 'rlm_output'
                              else 'observed_submission')
        associations.setdefault(row['src'], set()).add(
            (row['observed_session'], row['dst'], row['job_id']))
    for row in rows:
        row['ambiguous'] = len(associations[row['src']]) > 1
    # Stable ordering and exact deduplication without collapsing distinct receipts.
    return sorted({_json(row): row for row in rows}.values(), key=lambda row: (
        row['src'], row.get('via', ''), row['kind'], row['dst'], row['job_id'],
        row['output_name'] or '', _json(row['anchor'])))
