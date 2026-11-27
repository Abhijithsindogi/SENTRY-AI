"""Prompt locally for Twilio credentials and run an isolated trial-only server."""
import argparse
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import warnings

import requests

from twilio_trial import TRIAL_SMS, SPEECH_RECOGNITION_TEMPLATE, VOICE_TEMPLATES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sender', required=True)
    parser.add_argument('--test-to', required=True)
    parser.add_argument('--family-to', required=True)
    parser.add_argument('--template', choices=sorted(TRIAL_SMS), default='sms_appointment_reminders')
    parser.add_argument('--voice-url', default=SPEECH_RECOGNITION_TEMPLATE, choices=sorted(VOICE_TEMPLATES))
    parser.add_argument('--reconcile-call', action='store_true', help='Look up an existing test Call SID without dialing')
    parser.add_argument('--enable-validated-automation', action='store_true')
    parser.add_argument('--http-port', type=int, default=8001)
    args = parser.parse_args()
    if not all(re.fullmatch(r'\+[1-9][0-9]{7,14}', value)
               for value in (args.sender, args.test_to, args.family_to)):
        parser.error('Use international E.164 format for the sender and recipients.')
    if not sys.stdin.isatty():
        parser.error('Run in an interactive Windows terminal. Credentials require hidden input.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        sid = getpass.getpass('Twilio LIVE Account SID (hidden): ').strip()
        token = getpass.getpass('Newly rotated LIVE Auth Token (hidden): ').strip()
        password = getpass.getpass('Choose a temporary SENTRY patient password (hidden): ')
        existing = getpass.getpass('Existing test Call SID (hidden, Enter to skip): ').strip() if args.reconcile_call else ''
    if not re.fullmatch(r'AC[0-9a-fA-F]{32}', sid) or not token or len(password) < 8:
        parser.error('Check the Account SID format and use a patient password of at least 8 characters.')
    free = input('Console confirms active trial, free units available and assigned sender? [y/N] ').strip().lower() == 'y'
    voice = input('Console confirms India as sign-up country and this assigned sender for Voice to India? [y/N] ').strip().lower() == 'y'
    env = dict(os.environ)
    env.update(
        SENTRY_TWILIO_ACCOUNT_SID=sid, SENTRY_TWILIO_AUTH_TOKEN=token,
        SENTRY_TWILIO_FROM=args.sender, SENTRY_TWILIO_TEST_TO=args.test_to,
        SENTRY_TWILIO_FAMILY_TO=args.family_to, SENTRY_TWILIO_TRIAL_ONLY='1',
        SENTRY_TWILIO_TRIAL_SMS_TEMPLATE=args.template,
        SENTRY_TWILIO_TRIAL_VOICE_URL=args.voice_url, SENTRY_TWILIO_REGION='US1',
        SENTRY_TWILIO_TRIAL_AUTOMATION=str(int(args.enable_validated_automation)),
        SENTRY_TWILIO_EXISTING_CALL_PENDING=str(int(args.reconcile_call)),
        SENTRY_TWILIO_FREE_UNITS_CONFIRMED=str(int(free)),
        SENTRY_TWILIO_TRIAL_VOICE_IN=str(int(voice)),
        SENTRY_PATIENT_PASSWORD=password, SENTRY_TELEGRAM_BOT_TOKEN='',
    )
    if existing:
        from family_connect import reconcile_trial_call

        try:
            receipt = reconcile_trial_call((sid, token, args.sender), existing, args.test_to)
            print(f"Twilio status for the existing call: {receipt['provider_status']} (HTTP {receipt['http_status']})")
        except (ValueError, requests.RequestException):
            print('Existing call lookup could not be confirmed. New test calls remain blocked in this session.')
    print(f'Opening trial-only SENTRY at http://127.0.0.1:{args.http_port}/')
    print('Sign in as patient, open Family and select Check Twilio trial eligibility. No send occurs during preflight.')
    return subprocess.call([sys.executable, 'launch_sentry.py', '--demo', '--http-port', str(args.http_port)],
                           cwd=Path(__file__).resolve().parent, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
