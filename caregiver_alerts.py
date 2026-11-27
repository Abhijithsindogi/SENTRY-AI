"""SENTRY AI local caregiver review prototype. No clinical or emergency alerts.
Import router into engine.py; run this file as a separate polling worker.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
import sqlite3
import time
import json
import os
import uuid
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

DB = Path(os.getenv('SENTRY_DB_PATH', Path(__file__).with_name('sentry_alerts.sqlite3')))
router = APIRouter(tags=['Caregiver review prototype'])

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def database():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute('''CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, created TEXT NOT NULL, mode TEXT NOT NULL,
            patient TEXT NOT NULL, source TEXT NOT NULL, observed TEXT NOT NULL,
            reason TEXT NOT NULL, acknowledged TEXT)''')
        con.execute('''CREATE TABLE IF NOT EXISTS episodes (
            key TEXT PRIMARY KEY, active INTEGER NOT NULL, observed TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS event_reviews (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            read_at TEXT, note TEXT)''')
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('CREATE TABLE IF NOT EXISTS ai_profiles (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS ai_latest (key TEXT PRIMARY KEY, observed REAL, payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS ai_assessments (key TEXT, observed REAL, received REAL, payload TEXT NOT NULL)')
        con.execute('CREATE INDEX IF NOT EXISTS ai_assessments_received ON ai_assessments(received)')
        con.execute('''CREATE TABLE IF NOT EXISTS live_samples (
            channel TEXT NOT NULL, patient TEXT NOT NULL, source TEXT NOT NULL,
            observed REAL NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(channel, patient, source, observed))''')
        con.execute('CREATE INDEX IF NOT EXISTS live_samples_observed ON live_samples(observed)')
        con.execute('''CREATE TABLE IF NOT EXISTS history_samples (
            source TEXT NOT NULL, series TEXT NOT NULL, metric TEXT NOT NULL, day TEXT NOT NULL,
            identity TEXT NOT NULL, value REAL NOT NULL,
            PRIMARY KEY(source, series, metric, identity))''')
        con.execute('CREATE TABLE IF NOT EXISTS local_profiles (role TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        con.execute('''CREATE TABLE IF NOT EXISTS telegram_outbox (
            event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE, status TEXT NOT NULL)''')
        con.execute('CREATE TABLE IF NOT EXISTS family_preferences (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS family_diary (id TEXT PRIMARY KEY, occurred REAL NOT NULL, payload TEXT NOT NULL)')
        con.execute('''CREATE TABLE IF NOT EXISTS family_outbox (
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE, channel TEXT NOT NULL,
            status TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(event_id, channel))''')
        con.execute("""CREATE TABLE IF NOT EXISTS family_manual (
            id TEXT PRIMARY KEY, purpose TEXT NOT NULL, channel TEXT NOT NULL,
            issued TEXT NOT NULL, status TEXT NOT NULL)""")
        con.execute("CREATE TABLE IF NOT EXISTS family_members (id TEXT PRIMARY KEY, name TEXT NOT NULL, relationship TEXT NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS family_provider_receipts (
            attempt TEXT NOT NULL, channel TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(attempt, channel))""")
        yield con
        con.commit()
    finally:
        con.close()

def fresh_stamp(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None or not 0 <= (datetime.now(timezone.utc)-dt).total_seconds() <= 120:
            return None
        return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, TypeError):
        return None

def insert_event(con, mode, patient, source, observed, reason):
    event_id = uuid.uuid4().hex
    con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,NULL)',
                (event_id, now(), mode, patient, source, observed, reason))
    return event_id

def record_device_disconnect():
    """Persist a local-only notification after a previously connected wearable drops."""
    observed = datetime.now(timezone.utc)
    cutoff = (observed - timedelta(minutes=10)).isoformat()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        recent = con.execute("SELECT 1 FROM events WHERE mode='live' AND source='ble_connection' AND created >= ? LIMIT 1",
                             (cutoff,)).fetchone()
        if recent:
            return None
        return insert_event(con, 'live', 'patient_001', 'ble_connection', observed.isoformat(),
                            'The managed BLE wearable disconnected. Readings may be unavailable while it reconnects.')


def inspect_readings(rows):
    """Compatibility entry point for the standalone worker."""
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        return inspect_in_transaction(con, rows)


def inspect_in_transaction(con, rows):
    created = []
    if not isinstance(rows, list):
        return created
    for row in rows:
        if not isinstance(row, dict) or row.get('mode') != 'live' or row.get('source') != 'sentry_wearable' or row.get('context') != 'rest' or row.get('patient_id') != 'patient_001':
            continue
        stamp = fresh_stamp(row.get('timestamp'))
        if stamp is None:
            continue
        status = row.get('status')
        count = row.get('consecutive_windows')
        eligible = status == 'check_in_suggested' and row.get('caregiver_check_in') is True and type(count) is int and count >= 3
        # Only a fresh valid normal result ends an episode. Missing data never clears it.
        clear = status == 'no_deviation_detected' and row.get('caregiver_check_in') is False
        if not (eligible or clear):
            continue
        key = json.dumps([row['patient_id'],row['source'],row['context']])
        prev = con.execute('SELECT * FROM episodes WHERE key=?',(key,)).fetchone()
        if prev and stamp <= prev['observed']:
            continue
        if eligible and (not prev or not prev['active']):
            last = con.execute(
                "SELECT observed FROM events WHERE mode='live' AND patient=? AND source=? ORDER BY observed DESC LIMIT 1",
                (row['patient_id'], row['source']),
            ).fetchone()
            if last and (datetime.fromisoformat(stamp) - datetime.fromisoformat(last['observed'])).total_seconds() < 600:
                continue
            reasons = row.get('reasons')
            msg = '; '.join(str(r)[:200] for r in reasons[:5]) if isinstance(reasons,list) else ''
            created.append(insert_event(con,'live',row['patient_id'],row['source'],stamp,
               f'Experimental persistent deviation ({count} windows). '+(msg or 'Review the readings and signal quality.')))
        con.execute('INSERT OR REPLACE INTO episodes VALUES (?,?,?)',(key,int(eligible),stamp))
    return created

def alerts():
    with database() as con:
        rows = con.execute('SELECT events.*, read_at, note FROM events LEFT JOIN event_reviews ON event_id=id ORDER BY created DESC LIMIT 200').fetchall()
        unread = dict(con.execute("""
            SELECT mode, COUNT(*) FROM events LEFT JOIN event_reviews ON event_id=id
            WHERE read_at IS NULL AND acknowledged IS NULL GROUP BY mode
        """).fetchall())
    return {'events':[dict(r) for r in rows], 'unread': sum(unread.values()), 'unread_by_mode': unread, 'notice':'Local experimental review queue; not a medical or emergency alert service.'}

@router.get('/wearables/alerts')
def alerts_view(request: Request):
    data = alerts()
    if request.state.role == 'patient':
        for event in data['events']:
            event.pop('note', None)
    return data


@router.post('/wearables/alerts/demo')
def demo():
    with database() as con:
        eid = insert_event(con,'simulation','demo_patient','demo_scenario',now(),
          'SIMULATED persistent deviation. Tests the review workflow only; no real patient measurement.')
    return {'id':eid,'mode':'simulation'}

@router.post('/wearables/alerts/{event_id}/acknowledge')
def acknowledge(event_id: str):
    with database() as con:
        result=con.execute('UPDATE events SET acknowledged=COALESCE(acknowledged,?) WHERE id=?',(now(),event_id))
        if not result.rowcount:
            raise HTTPException(404,'Event not found')
    return {'status':'acknowledged'}

class ReviewNote(BaseModel):
    note: str = Field(max_length=1000)


@router.post('/wearables/alerts/{event_id}/read')
def mark_read(event_id: str):
    with database() as con:
        result = con.execute(
            'INSERT INTO event_reviews(event_id, read_at) SELECT id, ? FROM events WHERE id=? '
            'ON CONFLICT(event_id) DO UPDATE SET read_at=COALESCE(read_at, excluded.read_at)',
            (now(), event_id),
        )
        if not result.rowcount:
            raise HTTPException(404, 'Event not found')
    return {'status': 'read'}


@router.put('/wearables/alerts/{event_id}/note')
def save_note(event_id: str, body: ReviewNote):
    with database() as con:
        result = con.execute(
            'INSERT INTO event_reviews(event_id, note) SELECT id, ? FROM events WHERE id=? '
            'ON CONFLICT(event_id) DO UPDATE SET note=excluded.note',
            (body.note.strip(), event_id),
        )
        if not result.rowcount:
            raise HTTPException(404, 'Event not found')
    return {'status': 'saved'}


@router.delete('/wearables/alerts/demo')
def reset_demo():
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        count = con.execute("DELETE FROM events WHERE mode='simulation'").rowcount
        key = json.dumps(['demo_patient', 'guided_demo', 'simulation', 'rest'])
        profile = json.dumps(['demo_patient', 'guided_demo', 'simulation', 'rest', ['hr_bpm']])
        con.execute('DELETE FROM ai_profiles WHERE key=?', (profile,))
        con.execute('DELETE FROM ai_latest WHERE key=?', (key,))
        con.execute('DELETE FROM ai_assessments WHERE key=?', (key,))
    return {'deleted': count}


@router.get('/caregiver')
def caregiver():
    return FileResponse(Path(__file__).with_name('dashboard.html'))


def main():
    import requests
    print('SENTRY caregiver review worker: monitoring /latest every 5 seconds. Local experimental events only.',flush=True)
    while True:
        try:
            r=requests.get('http://127.0.0.1:8000/latest',timeout=3)
            r.raise_for_status()
            ids=inspect_readings(r.json())
            if ids:
                print('New experimental review event(s):',len(ids),flush=True)
        except (requests.RequestException, ValueError, sqlite3.Error) as e:
            print('Worker waiting:',e,flush=True)
        time.sleep(5)

if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Caregiver worker stopped.')
