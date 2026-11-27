import unittest
from unittest.mock import patch

import twilio_trial


class TwilioTrialTests(unittest.TestCase):
    def test_template_and_voice_payload(self):
        fields = twilio_trial.trial_fields('+12025550123', '+12025550100', 'sms',
                                           'sms_appointment_reminders')
        self.assertEqual(fields['Body'], 'sms_appointment_reminders')
        self.assertNotIn('ContentSid', fields)
        voice = twilio_trial.trial_fields('+919999999991', '+12025550100', 'voice', None)
        self.assertEqual(voice['Url'], twilio_trial.VOICE_TEMPLATE)
        self.assertNotIn('Twiml', voice)
        with self.assertRaises(ValueError):
            twilio_trial.trial_fields('+12025550123', '+12025550100', 'sms', 'free text')

    def test_country_verification_and_trial_gates(self):
        number = '+919999999991'
        env = {'SENTRY_TWILIO_TRIAL_VOICE_IN': '1', 'SENTRY_TWILIO_FREE_UNITS_CONFIRMED': '1'}
        with patch.dict('os.environ', env), patch.object(twilio_trial, 'account_active', return_value=True), patch.object(twilio_trial, 'verified_number', return_value=True):
            self.assertIn('US destinations', twilio_trial.eligibility(None, number, 'sms'))
            self.assertIsNone(twilio_trial.eligibility(None, number, 'voice'))
        with patch.object(twilio_trial, 'account_active', return_value=False):
            self.assertIn('active Trial', twilio_trial.eligibility(None, number, 'voice'))

    def test_response_omits_sensitive_fields(self):
        msg_sid = 'SM' + 'a' * 32
        result = twilio_trial.result_fields(201, {'sid': msg_sid, 'status': 'queued',
                                                'to': '+12025550123', 'body': 'test'})
        self.assertEqual(result, {'sid': msg_sid, 'http_status': 201,
                                  'provider_status': 'queued', 'error_code': None})


    def test_configured_speech_recognition_template(self):
        with patch.dict('os.environ', {'SENTRY_TWILIO_TRIAL_VOICE_URL': twilio_trial.SPEECH_RECOGNITION_TEMPLATE}):
            fields = twilio_trial.trial_fields('+919999999991', '+12025550100', 'voice', None)
        self.assertEqual(fields['Url'], twilio_trial.SPEECH_RECOGNITION_TEMPLATE)
