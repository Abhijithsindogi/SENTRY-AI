"""Free-trial requests, verification and redacted Twilio results."""
import os
import re

import requests

TRIAL_SMS = frozenset({
    'sms_2fa', 'sms_appointment_reminders', 'sms_order_confirmation',
    'sms_delivery_updates', 'sms_customer_support', 'sms_marketing_promotions',
    'sms_event_notifications', 'sms_account_alerts', 'sms_feedback_surveys', 'sms_internal_alerts',
})
VOICE_TEMPLATE = 'https://webhooks.twilio.com/v1/Voice/Template/voice_text_to_speech'
SPEECH_RECOGNITION_TEMPLATE = 'https://webhooks.twilio.com/v1/Voice/Template/voice_speech_recognition'
VOICE_TEMPLATES = {VOICE_TEMPLATE, SPEECH_RECOGNITION_TEMPLATE}
API = 'https://api.twilio.com/2010-04-01/Accounts'
SID = re.compile(r'(?:SM|MM|CA)[0-9a-fA-F]{32}')


def trial_only():
    return os.getenv('SENTRY_TWILIO_TRIAL_ONLY') == '1'


def trial_recipient():
    return os.getenv('SENTRY_TWILIO_TEST_TO', '')


def trial_fields(number, sender, channel, template):
    fields = {'To': number, 'From': sender}
    if channel == 'voice':
        url = os.getenv('SENTRY_TWILIO_TRIAL_VOICE_URL', VOICE_TEMPLATE)
        if url not in VOICE_TEMPLATES:
            raise ValueError('Unsupported trial voice template URL')
        fields['Url'] = url
    elif template in TRIAL_SMS:
        fields['Body'] = template
    else:
        raise ValueError('Unsupported trial SMS template')
    return fields


def provider_request(configured, method, suffix, **kwargs):
    if os.getenv('SENTRY_TWILIO_REGION', 'US1').upper() != 'US1':
        raise ValueError('Only Twilio US1 is configured')
    sid, token, _ = configured
    transport = requests.post if method == 'POST' else requests.get
    response = transport(f'{API}/{sid}{suffix}', auth=(sid, token),
                         timeout=(3, 5), allow_redirects=False, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def result_fields(http_status, body):
    sid = body.get('sid')
    return {
        'http_status': http_status,
        'sid': sid if isinstance(sid, str) and SID.fullmatch(sid) else None,
        'provider_status': body['status'] if isinstance(body.get('status'), str) else None,
        'error_code': body.get('error_code') or body.get('code'),
    }


def account_active(configured):
    status, body = provider_request(configured, 'GET', '.json')
    return status == 200 and body.get('sid') == configured[0] and body.get('type') == 'Trial' and body.get('status') == 'active'


def verified_number(configured, number):
    status, body = provider_request(configured, 'GET', '/OutgoingCallerIds.json',
                                    params={'PhoneNumber': number, 'PageSize': 1})
    if status != 200:
        return None
    return any(item.get('phone_number') == number for item in body.get('outgoing_caller_ids', []))


def eligibility(configured, number, channel):
    if not account_active(configured):
        return 'The provider did not confirm an active Trial account'
    if verified_number(configured, number) is not True:
        return 'The provider verified-number list did not confirm this recipient'
    if channel != 'voice' and not number.startswith('+1'):
        return 'The published Try out SMS trial requires verified US destinations'
    if channel == 'voice' and (not number.startswith('+91') or os.getenv('SENTRY_TWILIO_TRIAL_VOICE_IN') != '1'):
        return 'Confirm India as the trial sign-up country, the assigned voice sender, and available free voice minutes in Console'
    if os.getenv('SENTRY_TWILIO_FREE_UNITS_CONFIRMED') != '1':
        return 'Confirm free trial units and the assigned sender in Console before attempting a send'
    return None
