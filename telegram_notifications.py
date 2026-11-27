"""Opt-in Telegram delivery for newly created live check-ins."""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from caregiver_alerts import database

MESSAGE = 'A check-in is available. Please open your local SENTRY dashboard to review it.'
logger = logging.getLogger(__name__)


def config():
    token = os.getenv('SENTRY_TELEGRAM_BOT_TOKEN', '')
    chat = os.getenv('SENTRY_TELEGRAM_CHAT_ID', '')
    if re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token) and re.fullmatch(r'-?[0-9]+', chat):
        return token, chat
    return None


def consent(con):
    row = con.execute("SELECT payload FROM local_profiles WHERE role='patient'").fetchone()
    return bool(row and json.loads(row['payload']).get('telegram_opt_in'))


def queue_notifications(con, ids):
    if ids and config() and consent(con):
        con.executemany("INSERT OR IGNORE INTO telegram_outbox VALUES (?, 'queued')",
                        [(event_id,) for event_id in ids])


def deliver_one():
    settings = config()
    if not settings:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("""
            UPDATE telegram_outbox SET status='expired' WHERE status='queued'
            AND event_id IN (SELECT id FROM events WHERE created < ? OR acknowledged IS NOT NULL)
        """, (cutoff,))
        if not consent(con):
            con.execute("UPDATE telegram_outbox SET status='canceled' WHERE status='queued'")
            return
        row = con.execute("""
            SELECT event_id FROM telegram_outbox JOIN events ON event_id=id
            WHERE status='queued' AND mode='live' ORDER BY created LIMIT 1
        """).fetchone()
        if not row:
            return
        event_id = row['event_id']
        # Claim before sending to avoid duplicates after an ambiguous network failure or restart.
        con.execute("UPDATE telegram_outbox SET status='attempted' WHERE event_id=?", (event_id,))
    token, chat_id = settings
    status = 'failed'
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                 json={'chat_id': chat_id, 'text': MESSAGE, 'protect_content': True},
                                 timeout=5, allow_redirects=False)
        if response.ok and response.json().get('ok') is True:
            status = 'sent'
    except (requests.RequestException, ValueError):
        logger.warning('Telegram check-in delivery failed')
    with database() as con:
        con.execute('UPDATE telegram_outbox SET status=? WHERE event_id=?', (status, event_id))


async def telegram_worker():
    while True:
        if config():
            try:
                await asyncio.to_thread(deliver_one)
            except Exception:
                logger.error('Telegram queue processing failed')
        await asyncio.sleep(5)
