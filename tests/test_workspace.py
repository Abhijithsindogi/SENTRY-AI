"""Storage, import, access control, and notification integration checks."""
import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import caregiver_alerts as caregiver
import engine
import family_connect
import device_api
import history_api
import local_account
from measurement_store import SerialReading
import telegram_notifications


class WorkspaceIntegration(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = patch.object(caregiver, 'DB', Path(directory.name) / 'test.sqlite3')
        db.start()
        self.addCleanup(db.stop)

    def test_device_storage_persistence_and_ordering(self):
        stamp = datetime.now(timezone.utc) - timedelta(seconds=1)
        packet = SerialReading(patient_id='patient_001', source='sentry_wearable', mode='live',
                               timestamp=stamp, hr_bpm=None, ir_raw=4200)
        engine.receive_telemetry(packet)
        self.assertEqual(engine.get_telemetry()['ir_raw'], 4200)
        self.assertIsNone(engine.live_history()['items'][0]['hr_bpm'])
        with self.assertRaises(HTTPException) as err:
            engine.receive_telemetry(packet)
        self.assertEqual(err.exception.status_code, 409)
        self.assertEqual(len(engine.live_history()['items']), 1)

    def test_import_duplicates_and_report(self):
        archive, = Path(__file__).resolve().parents[1].glob('DaHalo_HealthExport_*.zip')
        first = history_api.import_samples(archive.read_bytes(), 'zip')
        repeat = history_api.import_samples(archive.read_bytes(), 'zip')
        self.assertGreater(first['inserted'], 0)
        self.assertEqual(repeat['inserted'], 0)
        raw = b'record_date,metric,value,source\n2025-01-01,heart_rate_bpm,71,Fixture\n'
        self.assertEqual(history_api.import_samples(raw, 'csv')['inserted'], 1)
        data = history_api.analytics(start=datetime(2025, 1, 1).date(), end=datetime(2025, 1, 1).date())
        self.assertIn('CSV / Fixture', {row['source'] for row in data['groups']})
        self.assertTrue(history_api.report('pdf').body.startswith(b'%PDF-'))
        self.assertIn(b'historical_import', history_api.report('csv').body)
        with self.assertRaises(ValueError):
            history_api.import_samples(raw.replace(b',71,', b',NaN,'), 'csv')

    def test_guided_demo_and_reset(self):
        result = engine.run_guided_demo()
        self.assertEqual(result['mode'], 'simulation')
        self.assertEqual(result['calibration']['windows'], 30)
        self.assertEqual(result['trajectory'][-1]['consecutive_windows'], 3)
        self.assertTrue(all(item['mode'] == 'simulation' for item in engine.get_latest()))
        caregiver.reset_demo()
        self.assertEqual(engine.get_latest(), [])
        self.assertEqual(engine.get_baselines(), [])

    def test_managed_ble_retries_and_disconnects(self):
        device = type('Device', (), {'address': 'sample', 'name': 'Sekyo fixture'})()
        advert = type('Advertisement', (), {'local_name': None,
                                           'service_uuids': [device_api.HRS]})()

        async def scenario():
            recovered = asyncio.Event()
            calls = 0

            async def stream(args, status, deliver):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError('Fixture radio disconnect')
                status('connected', name=args.name, battery_percent=91)
                recovered.set()
                await asyncio.Event().wait()

            with patch.object(device_api.BleakScanner, 'discover',
                              new=AsyncMock(return_value={'sample': (device, advert)})), \
                 patch.object(device_api, 'stream_once', side_effect=stream):
                items = await device_api.scan()
                self.assertTrue(items[0]['advertises_heart_rate'])
                await device_api.connect(device_api.DeviceChoice(address='sample'))
                try:
                    await asyncio.wait_for(recovered.wait(), 7)
                    status = await device_api.connection_status()
                    self.assertEqual(status['status'], 'connected')
                    self.assertEqual(status['battery_percent'], 91)
                    self.assertEqual(calls, 2)
                finally:
                    await device_api.disconnect()
                self.assertEqual((await device_api.connection_status())['status'], 'idle')
        asyncio.run(scenario())

    def test_role_boundaries(self):
        async def check(path, method, auth=None, origin=None):
            headers = [(b'host', b'127.0.0.1:8000')]
            if auth:
                headers.append((b'authorization', auth.encode()))
            if origin:
                headers.append((b'origin', origin.encode()))
            req = Request({'type': 'http', 'scheme': 'http', 'path': path, 'method': method,
                           'headers': headers, 'client': ('127.0.0.1', 50000)})

            async def ok(request):
                return JSONResponse({'role': request.state.role})
            return (await local_account.access_control(req, ok)).status_code

        patient = 'Basic ' + base64.b64encode(b'patient:fixture-patient').decode()
        caregiver_account = 'Basic ' + base64.b64encode(b'caregiver:fixture-caregiver').decode()
        with patch.dict(os.environ, {'SENTRY_PATIENT_PASSWORD': 'fixture-patient',
                                    'SENTRY_CAREGIVER_PASSWORD': 'fixture-caregiver',
                                    'SENTRY_BRIDGE_TOKEN': 'fixture-bridge'}):
            self.assertEqual(asyncio.run(check('/telemetry', 'GET')), 401)
            self.assertEqual(asyncio.run(check('/telemetry', 'POST', patient)), 403)
            self.assertEqual(asyncio.run(check('/telemetry', 'POST', 'Bearer fixture-bridge')), 200)
            self.assertEqual(asyncio.run(check('/wearables/alerts/demo/run', 'POST', patient)), 403)
            self.assertEqual(asyncio.run(check('/wearables/alerts/demo/run', 'POST', caregiver_account)), 200)
            self.assertEqual(asyncio.run(check('/devices/scan', 'POST', caregiver_account)), 403)
            self.assertEqual(asyncio.run(check('/devices/scan', 'POST', patient, 'http://example.test')), 403)

    def test_telegram_consent_and_payload(self):
        with patch.dict(os.environ, {'SENTRY_TELEGRAM_BOT_TOKEN': '123:fixture',
                                    'SENTRY_TELEGRAM_CHAT_ID': '456'}):
            with caregiver.database() as con:
                event_id = caregiver.insert_event(con, 'live', 'fixture-patient', 'fixture-device',
                                                  caregiver.now(), 'private measurement')
                telegram_notifications.queue_notifications(con, [event_id])
                self.assertEqual(con.execute('SELECT COUNT(*) FROM telegram_outbox').fetchone()[0], 0)
                con.execute('INSERT INTO local_profiles VALUES (?,?)', ('patient', '{"telegram_opt_in":true}'))
                telegram_notifications.queue_notifications(con, [event_id])
            with patch('telegram_notifications.requests.post') as send:
                send.return_value.ok = True
                send.return_value.json.return_value = {'ok': True}
                telegram_notifications.deliver_one()
                telegram_notifications.deliver_one()
                send.assert_called_once()
                self.assertEqual(send.call_args.kwargs['json']['text'], telegram_notifications.MESSAGE)
                self.assertNotIn('fixture-patient', str(send.call_args))
            with caregiver.database() as con:
                self.assertEqual(con.execute('SELECT status FROM telegram_outbox').fetchone()[0], 'sent')


    def test_family_live_only_outbox_and_consent(self):
        prefs = family_connect.Preferences(phone='+12025550123', recipient_consented=True,
                                           sms=True, voice=True)
        with patch.object(family_connect, 'settings', return_value=('AC' + 'a' * 32, 'fixture', '+12025550100')):
            request = Request({'type': 'http'})
            request.state.role = 'patient'
            family_connect.put_preferences(request, prefs)
            with caregiver.database() as con:
                live = caregiver.insert_event(con, 'live', 'patient', 'device', caregiver.now(), 'private')
                family_connect.queue(con, [live, live])
            with patch('family_connect.requests.post') as send:
                send.return_value.status_code = 201
                send.return_value.json.return_value = {'sid': 'fixture'}
                for _ in range(3):
                    family_connect.send_one()
                self.assertEqual(send.call_count, 2)
                self.assertIn('Messages.json', send.call_args_list[0].args[0])
                self.assertIn('Calls.json', send.call_args_list[1].args[0])
                self.assertEqual(send.call_args_list[0].kwargs['data']['Body'], family_connect.MESSAGE)
                self.assertEqual(send.call_args_list[1].kwargs['data']['Record'], 'false')
                self.assertNotIn('private', str(send.call_args_list))
                self.assertEqual({row['status'] for row in family_connect.deliveries()}, {'accepted'})
                engine.run_guided_demo()
                family_connect.send_one()
                self.assertEqual(send.call_count, 2)
            family_connect.put_preferences(request, family_connect.Preferences())
            with caregiver.database() as con:
                other = caregiver.insert_event(con, 'live', 'patient', 'device', caregiver.now(), 'private')
                family_connect.queue(con, [other])
            self.assertEqual(len(family_connect.deliveries()), 2)

    def test_family_diary_pdf_and_role(self):
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        entry = family_connect.Entry(kind='symptom', title='Fixture <sample>', status='recorded',
                                     occurred_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        family_connect.add_diary(request, entry)
        self.assertEqual(family_connect.get_diary()[0]['title'], entry.title)
        self.assertTrue(family_connect.diary_pdf().body.startswith(b'%PDF-'))
        request.state.role = 'caregiver'
        with self.assertRaises(HTTPException) as error:
            family_connect.add_diary(request, entry)
        self.assertEqual(error.exception.status_code, 403)


    def test_manual_phone_delivery_and_acknowledgement_stop_escalation(self):
        prefs = family_connect.Preferences(phone='+12025550123', recipient_consented=True,
                                           sms=True, voice=True, staged=True,
                                           backup_phone='+12025550129', backup_consented=True)
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        with patch.object(family_connect, 'settings', return_value=('AC' + 'a' * 32, 'fixture', '+12025550100')):
            family_connect.put_preferences(request, prefs)
            with caregiver.database() as con:
                event = caregiver.insert_event(con, 'live', 'patient', 'device', caregiver.now(), 'fixture')
                family_connect.queue(con, [event])
            with patch.object(family_connect, 'send_provider', return_value='accepted') as send:
                family_connect.send_one()
                family_connect.send_one()
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[2], 'sms')
                with caregiver.database() as con:
                    old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
                    con.execute('UPDATE family_outbox SET created=?', (old,))
                    con.execute('UPDATE events SET acknowledged=? WHERE id=?', (caregiver.now(), event))
                family_connect.send_one()
                self.assertEqual(send.call_count, 1)
                send.reset_mock()
                with caregiver.database() as con:
                    open_event = caregiver.insert_event(con, 'live', 'patient', 'device', caregiver.now(), 'fixture')
                    family_connect.queue(con, [open_event])
                    con.execute('UPDATE family_outbox SET created=? WHERE event_id=?', (old, open_event))
                for _ in range(3):
                    family_connect.send_one()
                self.assertEqual({call.args[2] for call in send.call_args_list}, {'sms', 'voice', 'backup_sms'})
                backup = next(call for call in send.call_args_list if call.args[2] == 'backup_sms')
                self.assertEqual(backup.args[1], '+12025550129')
                routine = family_connect.ManualDelivery(channel='sms', purpose='routine', confirmed=True)
                self.assertEqual(family_connect.manual_delivery(request, routine)['status'], 'accepted')
                test = family_connect.ManualDelivery(channel='voice', confirmed=True)
                self.assertEqual(family_connect.manual_delivery(request, test)['status'], 'accepted')
                with self.assertRaises(HTTPException) as throttled:
                    family_connect.manual_delivery(request, test)
                self.assertEqual(throttled.exception.status_code, 429)
                request.state.role = 'local'
                with self.assertRaises(HTTPException) as unauthorized:
                    family_connect.manual_delivery(request, test)
                self.assertEqual(unauthorized.exception.status_code, 403)

    def test_feedback_requires_valid_live_result(self):
        from live_ai_bridge import feedback_command
        timestamp = datetime.now(timezone.utc).isoformat()
        result = {'mode': 'live', 'status': 'check_in_suggested', 'timestamp': timestamp, 'caregiver_check_in': True}
        summary = {'quality_ok': True, 'timestamp': timestamp}
        self.assertEqual(feedback_command(result, summary), b'SENTRY REVIEW\n')
        self.assertEqual(feedback_command({**result, 'mode': 'simulation'}, summary), b'SENTRY WAIT\n')
        self.assertEqual(feedback_command(result, {**summary, 'quality_ok': False}), b'SENTRY WAIT\n')


    def test_capability_recognition_and_adaptive_policy(self):
        from types import SimpleNamespace
        from wearable_capabilities import ble_inventory, ble_strategy, discover_gatt, gatt

        characteristics = {gatt(key): SimpleNamespace(properties=modes) for key, modes in [
            ('2a37', ['notify']), ('2a5e', ['indicate']), ('2b41', ['read']),
        ]}
        features = discover_gatt(SimpleNamespace(get_characteristic=characteristics.get))
        self.assertTrue(features['heart_rate'])
        self.assertTrue(features['spo2'])
        self.assertTrue(features['sleep'])
        self.assertFalse(features['steps'])
        sample = {'hr_bpm': 75, 'status': 'receiving', 'sensor_contact': True, 'rr_intervals_ms': []}
        matrix = ble_inventory({'gatt_features': features}, sample, 40, False)['signals']
        self.assertEqual(matrix['heart_rate']['state'], 'observed')
        self.assertEqual(matrix['spo2']['state'], 'detected')
        self.assertEqual(matrix['rr_intervals']['state'], 'unknown')
        self.assertEqual(matrix['hrv']['state'], 'unavailable')
        self.assertEqual(ble_strategy('ble:other', sample, 40, False)['strategy'], 'display_only_no_device_reference')
        self.assertEqual(ble_strategy('sekyo_flex_ble', sample, 40, True)['strategy'], 'sparse_historical_hr_preview')
        self.assertEqual(ble_strategy('sekyo_flex_ble', {**sample, 'status': 'stale'}, 40, True)['uses'], [])

    def test_caregiver_context_is_time_bounded_and_never_enters_simulation(self):
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        observed = datetime.now(timezone.utc) - timedelta(minutes=4)
        for title, stamp in [('before', observed - timedelta(minutes=1)),
                             ('after', observed + timedelta(minutes=1)),
                             ('old', observed - timedelta(days=2))]:
            family_connect.add_diary(request, family_connect.Entry(kind='symptom', title=title, status='recorded', occurred_at=stamp))
        with caregiver.database() as con:
            live = caregiver.insert_event(con, 'live', 'patient_001', 'sentry_wearable', observed.isoformat(), 'fixture')
            simulation = caregiver.insert_event(con, 'simulation', 'patient_001', 'sentry_wearable', observed.isoformat(), 'fixture')
        with self.assertRaises(HTTPException) as denied:
            family_connect.caregiver_context(request, live)
        self.assertEqual(denied.exception.status_code, 403)
        request.state.role = 'caregiver'
        self.assertEqual([item['title'] for item in family_connect.caregiver_context(request, live)['entries']], ['before'])
        self.assertEqual(family_connect.caregiver_context(request, simulation)['entries'], [])


    def test_family_directory_and_simulated_messages_are_local_only(self):
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        member = family_connect.add_family_member(request, family_connect.FamilyMember(name='Test child', relationship='child'))
        self.assertEqual(family_connect.family_members(request)[0]['id'], member['id'])
        with self.assertRaises(HTTPException):
            family_connect.add_family_member(request, family_connect.FamilyMember(name='Test child', relationship='child'))
        family_connect.delete_family_member(request, member['id'])
        self.assertEqual(family_connect.family_members(request), [])
        simulation = engine.run_guided_demo()
        request.state.role = 'caregiver'
        family_connect.simulate_notifications(request, simulation['id'])
        family_connect.simulate_notifications(request, simulation['id'])
        rows = family_connect.deliveries()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['mode'] == 'simulation' and row['status'] == 'simulated' for row in rows))
        with patch.object(family_connect, 'settings', return_value=('AC' + 'a' * 32, 'fixture', '+12025550100')), patch.object(family_connect, 'send_provider') as send:
            family_connect.send_one()
            send.assert_not_called()
        caregiver.acknowledge(simulation['id'])
        with self.assertRaises(HTTPException):
            family_connect.simulate_notifications(request, simulation['id'])
        caregiver.reset_demo()
        self.assertEqual(family_connect.deliveries(), [])

    def test_disconnect_notice_is_local_and_deduplicated(self):
        event = caregiver.record_device_disconnect()
        self.assertIsNotNone(event)
        self.assertIsNone(caregiver.record_device_disconnect())
        self.assertEqual(caregiver.alerts()['events'][0]['source'], 'ble_connection')
        self.assertEqual(family_connect.deliveries(), [])


    def test_trial_voice_claim_and_provider_status(self):
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        call_sid = 'CA' + 'b' * 32
        configured = ('AC' + 'a' * 32, 'fixture', '+12025550100')
        number = '+919999999991'
        with patch.dict(os.environ, {'SENTRY_TWILIO_TRIAL_ONLY': '1', 'SENTRY_TWILIO_TEST_TO': number}), patch.object(family_connect, 'settings', return_value=configured), patch('twilio_trial.eligibility', return_value=None):
            with patch('twilio_trial.provider_request', return_value=(201, {'sid': call_sid, 'status': 'queued'})) as provider:
                delivery = family_connect.ManualDelivery(channel='voice', confirmed=True)
                result = family_connect.manual_delivery(request, delivery)
                self.assertEqual(result['receipt']['sid'], call_sid)
                with self.assertRaises(HTTPException) as repeated:
                    family_connect.manual_delivery(request, delivery)
                self.assertEqual(repeated.exception.status_code, 429)
                provider.assert_called_once()
            with patch('twilio_trial.provider_request', return_value=(200, {'sid': call_sid, 'status': 'completed'})):
                self.assertEqual(family_connect.refresh_provider_status(request, result['id'], 'voice')['provider_status'], 'completed')
            self.assertEqual(family_connect.deliveries()[0]['receipt']['provider_status'], 'completed')


    def test_existing_console_call_is_reconciled_without_a_post(self):
        configured = ('AC' + 'a' * 32, 'fixture', '+12025550100')
        sid = 'CA' + 'b' * 32
        to = '+919999999991'
        body = {'sid': sid, 'account_sid': configured[0], 'to': to, 'from': configured[2], 'status': 'queued'}
        with patch('twilio_trial.account_active', return_value=True), patch('twilio_trial.provider_request', return_value=(200, body)) as transport:
            self.assertEqual(family_connect.reconcile_trial_call(configured, sid, to)['provider_status'], 'queued')
            family_connect.reconcile_trial_call(configured, sid, to)
            self.assertTrue(all(call.args[1] == 'GET' for call in transport.call_args_list))
        deliveries = family_connect.deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]['receipt']['sid'], sid)
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        with patch.dict(os.environ, {'SENTRY_TWILIO_TRIAL_ONLY': '1', 'SENTRY_TWILIO_TEST_TO': to}), patch.object(family_connect, 'settings', return_value=configured), patch('twilio_trial.eligibility', return_value=None):
            with self.assertRaises(HTTPException) as duplicate:
                family_connect.manual_delivery(request, family_connect.ManualDelivery(channel='voice', confirmed=True))
            self.assertEqual(duplicate.exception.status_code, 429)

    def test_trial_automation_validates_evidence_and_enforces_timers(self):
        import json
        request = Request({'type': 'http'})
        request.state.role = 'patient'
        prefs = family_connect.Preferences(phone='+12025550123', recipient_consented=True,
                                           sms=True, voice=True, backup_phone='+12025550129', backup_consented=True)
        family_connect.put_preferences(request, prefs)
        configured = ('AC' + 'a' * 32, 'fixture', '+12025550100')
        env = {'SENTRY_TWILIO_TRIAL_ONLY': '1', 'SENTRY_TWILIO_TRIAL_AUTOMATION': '1',
               'SENTRY_TWILIO_FAMILY_TO': prefs.phone}
        with patch.dict(os.environ, env), patch.object(family_connect, 'settings', return_value=configured):
            timestamp = datetime.now(timezone.utc)
            with caregiver.database() as con:
                event = caregiver.insert_event(con, 'live', 'patient_001', 'sentry_wearable', timestamp.isoformat(), 'fixture')
                family_connect.queue(con, [event])
                self.assertEqual(con.execute('SELECT COUNT(*) FROM family_outbox').fetchone()[0], 0)
                key = json.dumps(['patient_001', 'sentry_wearable', 'live', 'rest'])
                assessment = {'quality_ok': True, 'status': 'check_in_suggested', 'caregiver_check_in': True,
                              'consecutive_windows': 3, 'evidence': {'window': {'independent_opt_in': True,
                                  'motion': 'still', 'fifo_uninterrupted': True}}}
                con.execute('INSERT INTO ai_assessments (key, observed, received, payload) VALUES (?,?,?,?)',
                            (key, timestamp.timestamp(), timestamp.timestamp(), json.dumps(assessment)))
                family_connect.queue(con, [event])
                family_connect.queue(con, [event])
            with patch('twilio_trial.eligibility', return_value=None), patch.object(family_connect, 'send_provider', return_value='accepted') as transport:
                family_connect.send_one()
                family_connect.send_one()
                self.assertEqual([call.args[2] for call in transport.call_args_list], ['sms'])
                with caregiver.database() as con:
                    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
                    con.execute('UPDATE family_outbox SET created=? WHERE event_id=?', (old, event))
                family_connect.send_one()
                family_connect.send_one()
                self.assertEqual([call.args[2] for call in transport.call_args_list], ['sms', 'voice'])
                with caregiver.database() as con:
                    old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
                    con.execute('UPDATE family_outbox SET created=? WHERE event_id=?', (old, event))
                family_connect.send_one()
                family_connect.send_one()
                self.assertEqual([call.args[2] for call in transport.call_args_list], ['sms', 'voice', 'backup_sms'])
