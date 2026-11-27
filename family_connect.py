"""Local family preferences, diary entries, and opt-in notification outbox."""
import asyncio
from datetime import date, datetime, time, timedelta, timezone
import io
import json
import logging
import os
import re
from typing import Literal
from uuid import uuid4
from xml.sax.saxutils import escape

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from caregiver_alerts import database, now
import twilio_trial

router = APIRouter(prefix='/family', tags=['Family Connect'])
PHONE = r'^\+[1-9][0-9]{7,14}$'
MESSAGE = 'A SENTRY check-in is available. Please open your local dashboard or contact your family member.'
logger = logging.getLogger(__name__)


class Preferences(BaseModel):
    label: str = Field(default='', max_length=80)
    phone: str = Field(default='', pattern=r'^(?:\+[1-9][0-9]{7,14})?$')
    recipient_consented: bool = False
    sms: bool = False
    voice: bool = False
    staged: bool = False
    backup_phone: str = Field(default='', pattern=r'^(?:\+[1-9][0-9]{7,14})?$')
    backup_consented: bool = False


class Entry(BaseModel):
    kind: Literal['medication', 'symptom']
    title: str = Field(min_length=1, max_length=120)
    notes: str = Field(default='', max_length=1000)
    status: Literal['taken', 'missed', 'recorded']
    occurred_at: datetime


def patient_access(request):
    if request.state.role not in {'patient', 'local'}:
        raise HTTPException(403, 'Patient access required')


def settings():
    sid = os.getenv('SENTRY_TWILIO_ACCOUNT_SID', '')
    token = os.getenv('SENTRY_TWILIO_AUTH_TOKEN', '')
    sender = os.getenv('SENTRY_TWILIO_FROM', '')
    if re.fullmatch(r'AC[0-9a-fA-F]{32}', sid) and token and re.fullmatch(PHONE, sender):
        return sid, token, sender
    return None


def preferences(con):
    row = con.execute('SELECT payload FROM family_preferences WHERE id=1').fetchone()
    return Preferences.model_validate_json(row['payload']) if row else Preferences()


@router.get('/preferences')
def get_preferences(request: Request):
    patient_access(request)
    with database() as con:
        value = preferences(con)
    return {'preferences': value, 'provider_configured': bool(settings()),
            'trial_only': twilio_trial.trial_only()}


@router.put('/preferences')
def put_preferences(request: Request, value: Preferences):
    patient_access(request)
    if (value.sms or value.voice) and not (value.phone and value.recipient_consented):
        raise HTTPException(422, 'A phone number and confirmed recipient consent are required')
    if value.backup_phone and (not value.backup_consented or value.backup_phone == value.phone):
        raise HTTPException(422, 'Supply a distinct backup number and confirm the backup recipient consent')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        if value != preferences(con):
            con.execute("UPDATE family_outbox SET status='canceled' WHERE status='queued'")
        con.execute('INSERT OR REPLACE INTO family_preferences VALUES (1,?)', (value.model_dump_json(),))
    return {'status': 'saved'}


def diary_rows(start=None, end=None):
    if start and end and start > end:
        raise HTTPException(422, 'Start date must be on or before end date')
    earliest = datetime.combine(start or date.min, time.min, timezone.utc).timestamp()
    latest = datetime.combine(end or date.max, time.max, timezone.utc).timestamp()
    with database() as con:
        rows = con.execute(
            'SELECT id, payload FROM family_diary WHERE occurred >= ? AND occurred <= ? '
            'ORDER BY occurred DESC, id LIMIT 250', (earliest, latest),
        ).fetchall()
    return [{'id': row['id'], **json.loads(row['payload'])} for row in rows]


@router.get('/diary')
def get_diary(start: date | None = None, end: date | None = None):
    return diary_rows(start, end)


@router.post('/diary', status_code=201)
def add_diary(request: Request, entry: Entry):
    patient_access(request)
    if entry.occurred_at.tzinfo is None or entry.occurred_at > datetime.now(timezone.utc):
        raise HTTPException(422, 'Supply a past or current timestamp with timezone')
    if not entry.title.strip() or (entry.kind == 'symptom') != (entry.status == 'recorded'):
        raise HTTPException(422, 'Supply a title and a status appropriate to the entry type')
    entry_id = uuid4().hex
    with database() as con:
        con.execute('INSERT INTO family_diary VALUES (?,?,?)',
                    (entry_id, entry.occurred_at.timestamp(), entry.model_dump_json()))
    return {'id': entry_id}


@router.get('/diary.pdf')
def diary_pdf(start: date | None = None, end: date | None = None):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    output = io.BytesIO()
    styles = getSampleStyleSheet()
    document = [Paragraph('SENTRY Family Connect diary', styles['Title']),
                Paragraph('Self-reported local entries. Up to 250 entries.', styles['Normal']),
                Paragraph(f'UTC range: {start or "earliest"} to {end or "latest"}', styles['Normal'])]
    for row in diary_rows(start, end):
        fields = [row['occurred_at'], row['kind'], row['status'], row['title'], row['notes']]
        document.extend([Spacer(1, 8), Paragraph(' | '.join(escape(field) for field in fields if field),
                                                styles['Normal'])])
    SimpleDocTemplate(output, title='SENTRY Family Diary').build(document)
    return Response(output.getvalue(), media_type='application/pdf',
                    headers={'Content-Disposition': 'attachment; filename="sentry-family-diary.pdf"'})


@router.get('/caregiver-context/{event_id}')
def caregiver_context(request: Request, event_id: str):
    if request.state.role not in {'local', 'caregiver'}:
        raise HTTPException(403, 'Caregiver access required')
    with database() as con:
        event = con.execute('SELECT mode, patient, source, observed FROM events WHERE id=?', (event_id,)).fetchone()
        if not event:
            raise HTTPException(404, 'Check-in not found')
        if event['mode'] != 'live' or event['patient'] != 'patient_001':
            return {'entries': [], 'assessment': None, 'mode': event['mode']}
        observed = datetime.fromisoformat(event['observed'])
        start = (observed - timedelta(hours=24)).timestamp()
        key = json.dumps([event['patient'], event['source'], 'live', 'rest'])
        assessment = con.execute('SELECT payload FROM ai_assessments WHERE key=? AND observed=? LIMIT 1',
                                 (key, observed.timestamp())).fetchone()
        entries = con.execute('SELECT payload FROM family_diary WHERE occurred BETWEEN ? AND ? '
                              'ORDER BY occurred DESC LIMIT 10', (start, observed.timestamp())).fetchall()
    return {'entries': [json.loads(row['payload']) for row in entries],
            'assessment': json.loads(assessment['payload']) if assessment else None, 'mode': 'live'}


class FamilyMember(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    relationship: Literal['child', 'parent', 'guardian', 'relative', 'other']


@router.get('/members')
def family_members(request: Request):
    patient_access(request)
    with database() as con:
        return [dict(row) for row in con.execute('SELECT id, name, relationship FROM family_members ORDER BY name, id')]


@router.post('/members', status_code=201)
def add_family_member(request: Request, member: FamilyMember):
    patient_access(request)
    name = member.name.strip()
    if not name:
        raise HTTPException(422, 'Supply a display name')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT COUNT(*) FROM family_members').fetchone()[0] >= 50:
            raise HTTPException(422, 'The family directory supports up to 50 entries')
        if con.execute('SELECT 1 FROM family_members WHERE name=? COLLATE NOCASE AND relationship=?',
                       (name, member.relationship)).fetchone():
            raise HTTPException(409, 'This family member is already registered')
        identifier = uuid4().hex
        con.execute('INSERT INTO family_members VALUES (?,?,?)', (identifier, name, member.relationship))
    return {'id': identifier}


@router.delete('/members/{identifier}')
def delete_family_member(request: Request, identifier: str):
    patient_access(request)
    with database() as con:
        if not con.execute('DELETE FROM family_members WHERE id=?', (identifier,)).rowcount:
            raise HTTPException(404, 'Family member not found')
    return {'status': 'removed'}


@router.post('/simulation/{event_id}')
def simulate_notifications(request: Request, event_id: str):
    if request.state.role not in {'local', 'caregiver'}:
        raise HTTPException(403, 'Caregiver access required')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        event = con.execute("SELECT acknowledged FROM events WHERE id=? AND mode='simulation'", (event_id,)).fetchone()
        if not event:
            raise HTTPException(404, 'Simulated review not found')
        if event['acknowledged']:
            raise HTTPException(409, 'This simulated review was acknowledged')
        con.executemany("INSERT OR IGNORE INTO family_outbox VALUES (?,?,'simulated',?)",
                        [(event_id, channel, now()) for channel in ('sms', 'voice', 'backup_sms')])
    return {'status': 'simulated', 'channels': ['sms', 'voice', 'backup_sms']}


@router.get('/deliveries')
def deliveries():
    with database() as con:
        rows = con.execute('SELECT event_id, channel, status, family_outbox.created, events.mode FROM family_outbox JOIN events ON events.id=event_id ORDER BY family_outbox.created DESC LIMIT 50').fetchall()
        manual = con.execute('SELECT id, purpose, channel, status, issued AS created FROM family_manual ORDER BY issued DESC LIMIT 50').fetchall()
        receipts = {(row['attempt'], row['channel']): json.loads(row['payload'])
                    for row in con.execute('SELECT * FROM family_provider_receipts')}
    result = sorted([dict(row) for row in [*rows, *manual]], key=lambda row: row['created'], reverse=True)[:50]
    for row in result:
        row['receipt'] = receipts.get((row.get('id') or row.get('event_id'), row['channel']))
    return result


def trial_automation():
    return os.getenv('SENTRY_TWILIO_TRIAL_AUTOMATION') == '1'


def validated_event(con, identifier):
    row = con.execute("SELECT source, patient, observed FROM events WHERE id=? AND mode='live' AND acknowledged IS NULL",
                      (identifier,)).fetchone()
    if not row or row['source'] != 'sentry_wearable':
        return False
    observed = datetime.fromisoformat(row['observed']).timestamp()
    key = json.dumps([row['patient'], row['source'], 'live', 'rest'])
    assessment = con.execute('SELECT payload FROM ai_assessments WHERE key=? AND observed=?',
                             (key, observed)).fetchone()
    if not assessment:
        return False
    result = json.loads(assessment['payload'])
    evidence = result.get('evidence') or {}
    window = evidence.get('window') or {}
    return (result.get('quality_ok') is True and result.get('caregiver_check_in') is True
            and result.get('status') == 'check_in_suggested'
            and type(result.get('consecutive_windows')) is int and result['consecutive_windows'] >= 3
            and window.get('independent_opt_in') is True and window.get('motion') == 'still'
            and window.get('fifo_uninterrupted') is True)


def queue(con, event_ids):
    trial = twilio_trial.trial_only()
    if not event_ids or not settings() or (trial and not trial_automation()):
        return
    if trial:
        event_ids = [identifier for identifier in event_ids if validated_event(con, identifier)]
    value = preferences(con)
    if not value.recipient_consented or not value.phone:
        return
    channels = [channel for channel in ('sms', 'voice') if getattr(value, channel)]
    if channels and value.backup_phone and value.backup_consented:
        channels.append('backup_sms')
    for channel in channels:
        con.executemany(
            "INSERT OR IGNORE INTO family_outbox SELECT id, ?, 'queued', ? FROM events WHERE id=? AND mode='live'",
            [(channel, now(), event_id) for event_id in event_ids],
        )


def send_one():
    configured = settings()
    trial = twilio_trial.trial_only()
    if not configured or (trial and not trial_automation()):
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("""UPDATE family_outbox SET status='expired' WHERE status='queued'
                     AND (created < ? OR event_id IN (SELECT id FROM events WHERE acknowledged IS NOT NULL))""",
                    (cutoff,))
        value = preferences(con)
        clock = datetime.now(timezone.utc)
        row = con.execute("""SELECT event_id, channel FROM family_outbox JOIN events ON event_id=id
                           WHERE family_outbox.status='queued' AND mode='live'
                           AND (channel NOT IN ('voice','backup_sms')
                                OR (channel='voice' AND family_outbox.created <= ?)
                                OR (channel='backup_sms' AND family_outbox.created <= ?))
                           ORDER BY family_outbox.created LIMIT 1""",
                          ((clock - timedelta(minutes=2 if trial or value.staged else 0)).isoformat(),
                           (clock - timedelta(minutes=5)).isoformat())).fetchone()
        if not row:
            return
        channel = row['channel']
        key = (row['event_id'], channel)
        backup = channel == 'backup_sms'
        number = value.backup_phone if backup else value.phone
        allowed = (
            number and (value.backup_consented if backup else value.recipient_consented)
            and ((value.sms or value.voice) if backup else getattr(value, channel))
        )
        if trial:
            selected = os.getenv('SENTRY_TWILIO_FAMILY_TO', '')
            allowed = allowed and validated_event(con, key[0]) and (backup or number == selected)
        con.execute('UPDATE family_outbox SET status=? WHERE event_id=? AND channel=?',
                    ('attempted' if allowed else 'canceled', *key))
    if not allowed:
        return
    if trial:
        try:
            blocker = twilio_trial.eligibility(configured, number, channel)
        except (requests.RequestException, ValueError):
            blocker = 'Provider preflight unavailable'
        with database() as con:
            current = preferences(con)
            expected = current.backup_phone if backup else current.phone
            still_consented = current.backup_consented if backup else current.recipient_consented
            stored_event = con.execute('SELECT created FROM events WHERE id=?', (key[0],)).fetchone()
            recent = stored_event and datetime.now(timezone.utc) - datetime.fromisoformat(stored_event['created']) <= timedelta(minutes=10)
            if blocker or not recent or not still_consented or expected != number or not validated_event(con, key[0]):
                con.execute("UPDATE family_outbox SET status=? WHERE event_id=? AND channel=?",
                            ('ineligible' if blocker else 'canceled', *key))
                return
    status, receipt = unpack_provider(send_provider(configured, number, channel, MESSAGE))
    with database() as con:
        con.execute('UPDATE family_outbox SET status=? WHERE event_id=? AND channel=?', (status, *key))
        save_receipt(con, *key, receipt)


def send_provider(configured, number, channel, message):
    sid, token, sender = configured
    voice = channel == 'voice'
    resource = 'Calls' if voice else 'Messages'
    fields = {'To': number, 'From': sender}
    fields.update({'Twiml': f'<Response><Say>{escape(message)}</Say></Response>', 'Record': 'false'}
                  if voice else {'Body': message})
    if twilio_trial.trial_only():
        fields = twilio_trial.trial_fields(number, sender, channel, os.getenv(
            'SENTRY_TWILIO_TRIAL_SMS_TEMPLATE', 'sms_appointment_reminders'))
    try:
        http_status, body = twilio_trial.provider_request(configured, 'POST', f'/{resource}.json', data=fields)
        receipt = twilio_trial.result_fields(http_status, body)
        status = 'accepted' if http_status == 201 and body.get('sid') else 'failed'
    except requests.RequestException:
        logger.warning('Family notification provider did not confirm acceptance')
        return {'status': 'unknown', 'receipt': None}
    return {'status': status, 'receipt': receipt}


def unpack_provider(value):
    if isinstance(value, str):
        return value, None
    return value['status'], value['receipt']


def save_receipt(con, identifier, channel, receipt):
    if receipt is not None:
        con.execute('INSERT OR REPLACE INTO family_provider_receipts VALUES (?,?,?)',
                    (identifier, channel, json.dumps(receipt)))


class ManualDelivery(BaseModel):
    channel: Literal['sms', 'voice']
    purpose: Literal['test', 'routine'] = 'test'
    confirmed: Literal[True]


@router.post('/manual-delivery')
def manual_delivery(request: Request, delivery: ManualDelivery):
    patient_access(request)
    if request.state.role != 'patient':
        raise HTTPException(403, 'Configure and sign in with a patient account to send a manual update')
    configured = settings()
    if not configured:
        raise HTTPException(503, 'SMS and voice provider configuration is missing')
    if delivery.purpose == 'routine' and delivery.channel != 'sms':
        raise HTTPException(422, 'Routine updates use SMS')
    trial = twilio_trial.trial_only()
    number = twilio_trial.trial_recipient() if trial else None
    if trial:
        if delivery.channel == 'voice' and os.getenv('SENTRY_TWILIO_EXISTING_CALL_PENDING') == '1':
            raise HTTPException(409, 'An existing Console call was reported. Reconcile that call before considering another')
        if delivery.purpose != 'test' or not re.fullmatch(PHONE, number):
            raise HTTPException(422, 'Trial mode only allows confirmed tests to the configured test recipient')
        try:
            blocker = twilio_trial.eligibility(configured, number, delivery.channel)
        except requests.RequestException as exc:
            raise HTTPException(502, 'The provider could not be reached for trial verification') from exc
        if blocker:
            raise HTTPException(422, blocker)
    key = ('trial_test' if trial else delivery.purpose, delivery.channel)
    claim_time = datetime.now(timezone.utc)
    minimum_gap = timedelta(days=1) if delivery.purpose == 'routine' else timedelta(minutes=2)
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        value = preferences(con)
        if not trial and not (value.phone and value.recipient_consented and getattr(value, delivery.channel)):
            raise HTTPException(422, 'Enable this channel and confirm recipient consent first')
        latest = con.execute('SELECT issued FROM family_manual WHERE purpose=? AND channel=? ORDER BY issued DESC LIMIT 1', key).fetchone()
        if latest and (trial or claim_time - datetime.fromisoformat(latest['issued']) < minimum_gap):
            raise HTTPException(429, 'A recent attempt already exists for this type of update')
        identifier = uuid4().hex
        con.execute('INSERT INTO family_manual VALUES (?,?,?,?,?)',
                    (identifier, *key, claim_time.isoformat(), 'attempted'))
    message = ('This is a SENTRY connection test.' if delivery.purpose == 'test'
               else 'This is a routine SENTRY update from your family member.')
    status, receipt = unpack_provider(send_provider(configured, number if trial else value.phone, delivery.channel, message))
    with database() as con:
        con.execute('UPDATE family_manual SET status=? WHERE id=?', (status, identifier))
        save_receipt(con, identifier, delivery.channel, receipt)
    return {'status': status, 'receipt': receipt, 'id': identifier}


@router.get('/trial-preflight')
def trial_preflight(request: Request):
    if request.state.role != 'patient':
        raise HTTPException(403, 'Patient sign-in required')
    configured = settings()
    if not configured or not twilio_trial.trial_only():
        raise HTTPException(503, 'Twilio trial session is not configured')
    try:
        active = twilio_trial.account_active(configured)
        numbers = {}
        for name, number in [('test', twilio_trial.trial_recipient()),
                             ('family', os.getenv('SENTRY_TWILIO_FAMILY_TO', ''))]:
            if number:
                numbers[name] = {
                    'last_four': number[-4:],
                    'listed_verified': twilio_trial.verified_number(configured, number) if active else None,
                    'sms_trial_country_supported': number.startswith('+1'),
                    'voice_console_confirmed': number.startswith('+91') and os.getenv('SENTRY_TWILIO_TRIAL_VOICE_IN') == '1',
                }
    except requests.RequestException as exc:
        raise HTTPException(502, 'Twilio preflight could not reach the provider') from exc
    return {'active_trial': active, 'recipients': numbers,
            'free_units_console_confirmed': os.getenv('SENTRY_TWILIO_FREE_UNITS_CONFIRMED') == '1'}


def reconcile_trial_call(configured, sid, number):
    """Import a Console test receipt without dispatching a second call."""
    if not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid):
        raise ValueError('Expected a Call SID beginning with CA')
    if not twilio_trial.account_active(configured):
        raise ValueError('Twilio did not confirm an active Trial account')
    status, body = twilio_trial.provider_request(configured, 'GET', f'/Calls/{sid}.json')
    if (status != 200 or body.get('sid') != sid or body.get('account_sid') != configured[0]
            or body.get('to') != number or body.get('from') != configured[2]):
        raise ValueError(f'Call account, sender or test recipient could not be verified (HTTP {status})')
    receipt = twilio_trial.result_fields(status, body)
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        previous = con.execute("SELECT id FROM family_manual WHERE purpose='trial_test' AND channel='voice' LIMIT 1").fetchone()
        identifier = previous['id'] if previous else uuid4().hex
        if previous:
            row = con.execute("SELECT payload FROM family_provider_receipts WHERE attempt=? AND channel='voice'", (identifier,)).fetchone()
            if row and json.loads(row['payload']).get('sid') != sid:
                raise ValueError('A different voice test is already recorded')
        else:
            con.execute("INSERT INTO family_manual VALUES (?, 'trial_test', 'voice', ?, 'accepted')",
                        (identifier, now()))
        save_receipt(con, identifier, 'voice', receipt)
    return receipt


class ExistingCall(BaseModel):
    sid: str = Field(pattern=r'^CA[0-9a-fA-F]{32}$')


@router.post('/trial-existing-call')
def import_trial_call(request: Request, record: ExistingCall):
    if request.state.role != 'patient':
        raise HTTPException(403, 'Patient sign-in required')
    configured = settings()
    if not configured or not twilio_trial.trial_only():
        raise HTTPException(503, 'Twilio trial session is not configured')
    try:
        return reconcile_trial_call(configured, record.sid, twilio_trial.trial_recipient())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(502, 'Twilio call status could not be reached') from exc


@router.post('/provider-status/{attempt}/{channel}')
def refresh_provider_status(request: Request, attempt: str, channel: Literal['sms', 'voice', 'backup_sms']):
    if request.state.role not in {'patient', 'caregiver'}:
        raise HTTPException(403, 'Sign-in required')
    configured = settings()
    if not configured:
        raise HTTPException(503, 'Provider configuration is missing')
    with database() as con:
        row = con.execute('SELECT payload FROM family_provider_receipts WHERE attempt=? AND channel=?',
                          (attempt, channel)).fetchone()
    previous = json.loads(row['payload']) if row else {}
    sid = previous.get('sid')
    if not sid or not twilio_trial.SID.fullmatch(sid):
        raise HTTPException(404, 'No provider SID is stored for this attempt')
    resource = 'Calls' if channel == 'voice' else 'Messages'
    try:
        status, body = twilio_trial.provider_request(configured, 'GET', f'/{resource}/{sid}.json')
    except requests.RequestException as exc:
        raise HTTPException(502, 'Twilio status is unavailable') from exc
    if status != 200 or body.get('sid') != sid:
        raise HTTPException(502, f'Twilio did not return status for this SID (HTTP {status})')
    receipt = twilio_trial.result_fields(status, body)
    with database() as con:
        save_receipt(con, attempt, channel, receipt)
    return receipt


async def worker():
    while True:
        try:
            await asyncio.to_thread(send_one)
        except Exception:
            logger.error('Family Connect outbox processing failed')
        await asyncio.sleep(5)
