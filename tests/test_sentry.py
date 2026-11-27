"""Software checks using isolated SQLite and synthetic packet fixtures."""
import importlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException

import caregiver_alerts as caregiver
import engine
from live_ai_bridge import minute_summary, parse
from sekyo_import import import_export
from universal_ai_engine import reference
from universal_ble_bridge import BLEReading, decode_hr, get_ble, receive_ble


class SentryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        original = caregiver.DB
        caregiver.DB = Path(temp.name) / 'sentry.sqlite3'
        self.addCleanup(setattr, caregiver, 'DB', original)

    def test_persisted_calibration_and_live_check_in(self):
        current = datetime.now(timezone.utc) - timedelta(seconds=1)

        def window(minutes, bpm, **changes):
            values = dict(patient_id='patient_001', source='sentry_wearable',
                          context='rest', mode='live', quality_ok=True, hr_bpm=bpm,
                          timestamp=current + timedelta(seconds=55 * minutes))
            return engine.Reading(**(values | changes))

        rows = [window(i - 33, 72 + i % 3) for i in range(30)]
        engine.baseline(engine.Calibration(readings=rows))
        importlib.reload(engine)
        self.assertEqual(engine.get_baselines()[0]['windows'], 30)
        for i in (-2, -1, 0):
            result = engine.reading(window(i, 105))
        self.assertEqual(result['status'], 'check_in_suggested')
        self.assertEqual(result['consecutive_windows'], 3)
        self.assertEqual(caregiver.inspect_readings(engine.get_latest()), [])
        event, = caregiver.alerts()['events']
        self.assertEqual(event['mode'], 'live')
        self.assertEqual(caregiver.alerts()['unread'], 1)
        caregiver.mark_read(event['id'])
        caregiver.save_note(event['id'], caregiver.ReviewNote(note='Reviewed fixture'))
        caregiver.acknowledge(event['id'])
        self.assertEqual(caregiver.alerts()['unread'], 0)
        self.assertEqual(caregiver.alerts()['events'][0]['note'], 'Reviewed fixture')

        simulated = caregiver.demo()
        caregiver.save_note(simulated['id'], caregiver.ReviewNote(note='Simulation fixture'))
        self.assertEqual(caregiver.reset_demo(), {'deleted': 1})
        self.assertEqual(len(caregiver.alerts()['events']), 1)
        with caregiver.database() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM event_reviews').fetchone()[0], 1)

    def test_missing_stale_duplicate_and_feature_specific_baseline(self):
        current = datetime.now(timezone.utc) - timedelta(seconds=1)
        base = dict(patient_id='fixture', source='fixture', context='rest', mode='live',
                    quality_ok=True, hr_bpm=73)
        early = [engine.Reading(**base, timestamp=current - timedelta(minutes=40-i))
                 for i in range(30)]
        engine.baseline(engine.Calibration(readings=early))
        stale = engine.Reading(**base, timestamp=current - timedelta(minutes=5))
        self.assertEqual(engine.reading(stale)['status'], 'stale_or_future_data')
        self.assertEqual(engine.get_latest(), [])
        missing = engine.Reading(**(base | dict(quality_ok=False, hr_bpm=None)),
                                 timestamp=current - timedelta(minutes=1))
        self.assertEqual(engine.reading(missing)['status'], 'insufficient_data')
        extra = engine.Reading(**base, timestamp=current, motion_rms=0.1)
        self.assertEqual(engine.reading(extra)['status'], 'baseline_required')
        with self.assertRaises(HTTPException) as err:
            engine.reading(extra)
        self.assertEqual(err.exception.status_code, 409)

    def test_serial_and_ble_packets(self):
        row = parse('BPM: N/A | IR: 123 | ACC: 1.01 | Motion: STILL | EMG ADC: 400')
        self.assertIsNone(row['hr_bpm'])
        self.assertEqual(row['bioamp_adc'], 400)
        self.assertIsNone(parse('invalid'))
        diagnostic = parse('BPM: N/A | IR: 125 | ACC: N/A | Motion: ERROR | ECG ADC: 400 | FIFO Drops: 3')
        self.assertIsNone(diagnostic['acceleration'])
        self.assertEqual(diagnostic['fifo_drops'], 3)
        self.assertEqual(decode_hr(b'\x06\x4b')['hr_bpm'], 75)
        self.assertIsNone(decode_hr(b'\x04\x4b')['hr_bpm'])
        with self.assertRaises(ValueError):
            decode_hr(b'\x10\x4b')
        timestamp = datetime.now(timezone.utc) - timedelta(seconds=1)
        packet = BLEReading(patient_id='fixture', source='test_ble', mode='live',
                            timestamp=timestamp, hr_bpm=75, sensor_contact=True)
        receive_ble(packet)
        self.assertEqual(get_ble('fixture', 'test_ble')['status'], 'receiving')
        with self.assertRaises(HTTPException):
            receive_ble(packet)
        with self.assertRaises(HTTPException):
            receive_ble(packet.model_copy(update={'timestamp': timestamp - timedelta(seconds=31)}))

    def test_uploaded_bench_serial_lines(self):
        lines = [
            'BPM: 64.52 | IR: 107085 | ACC: 9.75 | Motion: STILL | ECG ADC: 1631 | Red: ON | Blue: OFF | Buzzer: DISABLED | FIFO Drops: 0',
            'BPM: 64.52 | IR: 106920 | ACC: 9.73 | Motion: STILL | ECG ADC: 1628 | Red: OFF | Blue: ON | Buzzer: DISABLED | FIFO Drops: 0',
            'BPM: N/A | IR: 107471 | ACC: 9.72 | Motion: STILL | ECG ADC: 1678 | Red: OFF | Blue: ON | Buzzer: DISABLED | FIFO Drops: 0',
        ]
        results = [parse(line) for line in lines]
        self.assertEqual([row['hr_bpm'] for row in results], [64.52, 64.52, None])
        self.assertEqual([row['ir_raw'] for row in results], [107085, 106920, 107471])
        self.assertTrue(all(row['fifo_drops'] == 0 and not row['quality_ok'] for row in results))
        enriched = parse(lines[-1] + ' | Pulse State: INCONSISTENT | IR Span: 531 | Beats: 9 | Interval Samples: 121 | Early: 0 | Late: 0 | Inconsistent: 1')
        self.assertEqual(enriched['pulse_state'], 'INCONSISTENT')
        self.assertEqual(enriched['ir_span'], 531)
        self.assertEqual(enriched['interval_samples'], 121)
        self.assertIsNone(enriched['hr_bpm'])

    def test_calibration_rejects_fifo_loss(self):
        sample = dict(motion='STILL', hr_bpm=72, fifo_drops=2)
        rows = [sample] * 60
        self.assertTrue(minute_summary(rows, True)['quality_ok'])
        rows[-1] = {**sample, 'fifo_drops': 3}
        self.assertIsNone(minute_summary(rows, True)['hr_bpm'])

    def test_original_archive_and_chronological_replay(self):
        archive, = Path(__file__).resolve().parents[1].glob('DaHalo_HealthExport_*.zip')
        historical = import_export(archive)
        self.assertEqual(historical['mode'], 'historical_import')
        profile, *_ = reference()
        self.assertTrue(profile['training_days'])
        self.assertLess(max(profile['training_days']), profile['held_out_day'])
        self.assertGreater(profile['held_out_readings'], 0)

    def test_registered_application_routes(self):
        routes = engine.app.openapi()['paths']
        for path in ('/', '/caregiver', '/telemetry', '/latest', '/baseline', '/readings',
                     '/wearables/sekyo/history', '/wearables/sekyo/summary',
                     '/wearables/ble/live', '/wearables/ai/summary', '/wearables/ai/sekyo/replay',
                     '/wearables/alerts/demo'):
            self.assertIn(path, routes)
        self.assertEqual(engine.dashboard().path, caregiver.caregiver().path)


if __name__ == '__main__':
    unittest.main()


