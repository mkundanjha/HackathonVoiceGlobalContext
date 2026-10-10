"""Local History API simulation: durable event queue and structured context activity."""

import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

from events import emit

DB_PATH = Path(__file__).resolve().parent.parent / 'memory' / 'demo_history.sqlite3'
_worker_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()
_thread = None


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS activity (
        event_id TEXT PRIMARY KEY, glid TEXT NOT NULL, role TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL, saved_at REAL NOT NULL,
        received_at REAL, finished_at REAL, version INTEGER, save_ms REAL,
        processing_ms REAL, error TEXT)''')
    try:
        with db:
            yield db
    finally:
        db.close()


def submit(payload):
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM activity WHERE event_id=?', (payload['event_id'],)).fetchone()
        if previous:
            if previous['payload'] != encoded:
                raise ValueError('Event ID already exists with different activity details')
            return dict(previous)
        saved = time.time()
        db.execute('INSERT INTO activity(event_id,glid,role,payload,status,saved_at) VALUES(?,?,?,?,?,?)',
                   (payload['event_id'], payload['glid'], payload['role'], encoded, 'queued', saved))
    emit('demo_history', 'saved', payload['glid'], payload['role'], event_id=payload['event_id'])
    emit('demo_event', 'emitted', payload['glid'], payload['role'], event_id=payload['event_id'])
    _wake.set()
    return get(payload['event_id'])


def get(event_id):
    with connect() as db:
        row = db.execute('SELECT * FROM activity WHERE event_id=?', (event_id,)).fetchone()
    return dict(row) if row else None


def retry(event_id):
    with connect() as db:
        row = db.execute('SELECT * FROM activity WHERE event_id=?', (event_id,)).fetchone()
        if not row:
            raise KeyError(event_id)
        if row['status'] == 'failed':
            db.execute("UPDATE activity SET status='queued',error=NULL WHERE event_id=?", (event_id,))
    _wake.set()
    return get(event_id)


def user_status(glid, role):
    if not DB_PATH.exists():
        return {'pending': False, 'latest': None, 'metrics': {}}
    with connect() as db:
        rows = db.execute('SELECT * FROM activity WHERE glid=? AND role=? ORDER BY saved_at DESC', (glid, role)).fetchall()
    successful = sorted(r['save_ms'] for r in rows if r['status'] == 'done' and r['save_ms'] is not None)
    def percentile(q):
        if not successful:
            return None
        position = (len(successful) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(successful) - 1)
        return round(successful[lower] + (successful[upper] - successful[lower]) * (position - lower), 1)
    return {'pending': any(r['status'] in ('queued', 'processing') for r in rows),
            'latest': dict(rows[0]) if rows else None,
            'metrics': {'count': len(successful), 'p50_ms': percentile(.5), 'p95_ms': percentile(.95)}}


def active_records(glid, role):
    if not DB_PATH.exists():
        return []
    with connect() as db:
        rows = db.execute('''SELECT * FROM activity WHERE glid=? AND role=?
            AND status IN ('processing','done') ORDER BY saved_at DESC, rowid DESC''',
            (glid, role)).fetchall()
    result, tickets = [], set()
    recent = 0
    for row in rows:
        data = json.loads(row['payload'])
        if data['activity_type'].startswith('ticket.'):
            # Latest state per ID is durable; closure must survive source refreshes.
            if data['ticket_id'] in tickets:
                continue
            tickets.add(data['ticket_id'])
        elif row['saved_at'] < time.time() - 30 * 86400 or recent >= 3:
            continue
        else:
            recent += 1
        result.append(dict(row))
    return result


def decorate(body, meta):
    """Merge durable demo sources on every current-file write, never history snapshots."""
    from renderer import insert_bullets
    rows = active_records(meta['glid'], meta['role'])
    lines = [line for line in body.splitlines() if '[Demo history:' not in line]
    body = '\n'.join(lines)
    section = 'Requirements and Enquiries' if meta['role'] == 'buyer' else 'Leads and Enquiries'
    bullets = []
    for row in rows:
        data = json.loads(row['payload'])
        date = datetime.fromtimestamp(row['saved_at']).strftime('%d %b %Y')
        if data['activity_type'].startswith('ticket.'):
            ticket_id = data['ticket_id']
            # Override only this ticket's source/learned state and related follow-ups.
            current_section = None
            kept = []
            for line in body.splitlines():
                if line.startswith('## '):
                    current_section = line[3:]
                match = re.search(r'\bticket\s*[:#]?\s*(\d+)\b', line, re.I)
                if current_section in ('Support and Service State', 'Pending Actions and Clarifications') and match and match[1] == ticket_id:
                    continue
                kept.append(line)
            body = '\n'.join(kept)
            if data['activity_type'] == 'ticket.opened':
                detail = f"Open support ticket: {ticket_id}; status WIP; issue: {data['issue']}"
            else:
                detail = f'Support ticket: {ticket_id}; status Closed in local demo'
            body = insert_bullets(body, 'Support and Service State',
                [f'- {detail}; {date}. [Demo history: {row["event_id"]}]'])
            continue
        kind = 'New available lead' if data['activity_type'] == 'lead.available' else 'New enquiry'
        if meta['role'] == 'buyer':
            kind = 'Requirement submitted'
        amount = f"; {data['quantity']} {data['unit']}" if data['activity_type'] == 'enquiry.created' else ''
        bullets.append(f"- {date}: {kind}: {data['product']}{amount}; "
                       f"{data['city']}. [Demo history: {row['event_id']}]")
    if bullets:
        body = insert_bullets(body, section, bullets)
    # Historical application marker survives expiry and later compaction for recovery.
    return body


def process_one():
    from compact import COMPACT_VERSION, learned_state
    from guide import strip_guide
    from renderer import SECTIONS
    from view_builder import CONTEXTS_DIR, SAMPLES_DIR, file_lock, read_view_file, save_history, write_view_file

    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM activity WHERE status='queued' ORDER BY saved_at LIMIT 1").fetchone()
        if not row:
            return False
        received = time.time()
        db.execute("UPDATE activity SET status='processing',received_at=?,error=NULL WHERE event_id=?", (received, row['event_id']))
    row = dict(row)
    started = time.perf_counter()
    event_id, glid, role = row['event_id'], row['glid'], row['role']
    emit('demo_event', 'received', glid, role, event_id=event_id)
    emit('demo_update', 'start', glid, role, event_id=event_id)
    try:
        path = CONTEXTS_DIR / glid / f'{role}.md'
        with file_lock(glid, role):
            source = path if path.exists() else SAMPLES_DIR / glid / f'{role}.md'
            if source.exists():
                meta, body = read_view_file(source)
                if not meta.get('source_refreshed_at') and not meta.get('generated_at'):
                    meta['source_refreshed_at'] = datetime.fromtimestamp(source.stat().st_mtime).astimezone().isoformat()
            else:
                meta = {'schema_version': 2, 'glid': glid, 'role': role, 'context_version': 0}
                body = '# Context\n\n' + '\n\n'.join(f'## {s}\n- Unknown.' for s in SECTIONS[role])
            meta.update(glid=glid, role=role)
            applied = meta.get('demo_applied_events', {})
            if event_id in applied or meta.get('last_demo_event_id') == event_id:
                previous = applied.get(event_id, {})
                version = int(previous.get('version', meta['context_version']))
                published = float(previous.get('published_at', meta.get('last_demo_published_at', time.time())))
                recovered = True
            else:
                recovered = False
                state = learned_state(body, meta)
                version = save_history(path, meta, body) + 1
                meta.update(context_version=version, learned_state=state, compact_version=COMPACT_VERSION,
                            last_demo_event_id=event_id, last_demo_published_at=time.time(),
                            mode='demo_history_updated')
                meta['demo_applied_events'] = {**applied, event_id: {'version': version, 'published_at': meta['last_demo_published_at']}}
                write_view_file(path, meta, strip_guide(body))
                published = time.time()
        processing_ms = round((time.perf_counter() - started) * 1000, 1)
        save_ms = round(max(0, received - row['saved_at']) * 1000 + processing_ms, 1)
        if recovered:
            save_ms = round(max(0, published - row['saved_at']) * 1000, 1)
            processing_ms = round(max(0, published - (row.get('received_at') or received)) * 1000, 1)
        with connect() as db:
            db.execute("UPDATE activity SET status='done',finished_at=?,version=?,save_ms=?,processing_ms=? WHERE event_id=?",
                       (published, version, save_ms, processing_ms, event_id))
        emit('demo_file', 'done', glid, role, event_id=event_id, version=version, save_ms=save_ms, processing_ms=processing_ms)
    except Exception as exc:
        with connect() as db:
            db.execute("UPDATE activity SET status='failed',error=? WHERE event_id=?", (str(exc)[:300], event_id))
        emit('demo_file', 'error', glid, role, event_id=event_id, error=str(exc)[:300])
    return True


def _run():
    while not _stop.is_set():
        try:
            while not _stop.is_set() and process_one():
                pass
        except Exception:
            # Leave queued work durable; retry after the next wakeup.
            pass
        _wake.wait(1)
        _wake.clear()


def start_worker():
    global _thread
    with _worker_lock:
        if _thread and _thread.is_alive():
            return
        with connect() as db:
            db.execute("UPDATE activity SET status='queued' WHERE status='processing'")
        _stop.clear()
        _thread = threading.Thread(target=_run, name='demo-history', daemon=True)
        _thread.start()


def stop_worker():
    _stop.set()
    _wake.set()
    if _thread:
        _thread.join(timeout=5)
