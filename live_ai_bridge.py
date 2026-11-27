"""SENTRY AI: local serial telemetry and experimental minute-level AI bridge.
USB bench testing only with all body-attached BioAmp electrodes disconnected.
"""
import argparse
import os
import math
import statistics
import time
from datetime import datetime

import requests
import serial
from serial.tools import list_ports

BASE = 'http://127.0.0.1:8000'
PATIENT = 'patient_001'
SOURCE = 'sentry_wearable'
WINDOW = 60


def now():
    return datetime.now().astimezone().isoformat()


def parse(line):
    fields = dict(tuple(s.strip() for s in part.split(':', 1))
                  for part in line.split('|') if ':' in part)
    try:
        bpm = float(fields.get('BPM', 'nan')) if fields.get('BPM') != 'N/A' else None
        if bpm is not None and (not math.isfinite(bpm) or bpm <= 0):
            bpm = None
        adc = fields.get('EMG ADC', fields.get('ECG ADC'))
        acceleration = float(fields['ACC']) if fields['ACC'] != 'N/A' else None
        if acceleration is not None and not math.isfinite(acceleration):
            acceleration = None
        numeric_diagnostics = {
            'fifo_drops': 'FIFO Drops', 'ir_span': 'IR Span', 'beat_triggers': 'Beats',
            'interval_samples': 'Interval Samples', 'early_triggers': 'Early',
            'late_intervals': 'Late', 'inconsistent_intervals': 'Inconsistent',
        }
        diagnostics = {key: int(fields[label]) for key, label in numeric_diagnostics.items() if label in fields}
        if 'Pulse State' in fields:
            diagnostics['pulse_state'] = fields['Pulse State']
        return dict(patient_id=PATIENT, source=SOURCE, mode='live',
                    timestamp=now(), hr_bpm=bpm, ir_raw=int(fields['IR']),
                    acceleration=acceleration, motion=fields['Motion'].upper(),
                    bioamp_adc=int(adc) if adc is not None else None,
                    emg_rms=None, quality_ok=False, **diagnostics)
    except (ValueError, KeyError, TypeError):
        return None


def minute_summary(rows, allow_bpm):
    # Bench-only gate: numeric values and coverage do not establish signal quality.
    # No motion RMS or EMG RMS can be recovered from this serial format.
    valid = [r['hr_bpm'] for r in rows if r['hr_bpm'] is not None]
    rest = bool(rows) and all(r['motion'] == 'STILL' for r in rows)
    bpm = statistics.median(valid) if valid else None
    # Firmware currently sends roughly 2 messages/s. Require coverage and valid BPM.
    drops = [row['fifo_drops'] for row in rows if row.get('fifo_drops') is not None]
    uninterrupted = not drops or drops[0] == drops[-1]
    good = bool(allow_bpm and rest and uninterrupted and len(rows) >= 60 and len(valid) >= 0.9*len(rows))
    return dict(patient_id=PATIENT, source=SOURCE, mode='live', context='rest',
                timestamp=now(), hr_bpm=bpm if good else None,
                motion_rms=None, emg_rms=None, quality_ok=good,
                window_evidence=dict(total_packets=len(rows), bpm_packets=len(valid),
                                     motion='still' if rest else 'movement_or_unknown' if rows else 'no_samples',
                                     fifo_uninterrupted=uninterrupted, independent_opt_in=allow_bpm))


def post(path, data):
    try:
        token = os.getenv('SENTRY_BRIDGE_TOKEN')
        headers = {'Authorization': f'Bearer {token}'} if token else None
        r = requests.post(BASE+path, json=data, headers=headers, timeout=3)
        if not r.ok:
            print(f'{path}: HTTP {r.status_code} {r.text[:300]}', flush=True)
            return None
        return r.json()
    except requests.RequestException as exc:
        print(f'{path}: {exc}', flush=True)
        return None


def feedback_command(result, summary):
    if not result or not summary.get('quality_ok') or result.get('mode') != 'live':
        return b'SENTRY WAIT\n'
    if result.get('timestamp') != summary.get('timestamp'):
        try:
            if datetime.fromisoformat(result['timestamp']) != datetime.fromisoformat(summary['timestamp']):
                return b'SENTRY WAIT\n'
        except (KeyError, TypeError, ValueError):
            return b'SENTRY WAIT\n'
    if result.get('status') == 'check_in_suggested' and result.get('caregiver_check_in') is True:
        return b'SENTRY REVIEW\n'
    if result.get('status') == 'no_deviation_detected':
        return b'SENTRY OK\n'
    return b'SENTRY WAIT\n'


def main():
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM5')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--calibrate', action='store_true', help='Collect 30 nonoverlapping resting minute windows')
    parser.add_argument('--bench-validated-bpm', action='store_true', help='Opt in only after separately checking BPM and its signal quality; experimental only')
    parser.add_argument('--feedback', action='store_true', help='Enable LED/OLED messages for the separate feedback firmware')
    parser.add_argument('--url', default=BASE, help='Loopback SENTRY backend URL')
    args = parser.parse_args()
    BASE = args.url.rstrip('/')
    if args.feedback and args.calibrate:
        parser.error('Feedback is disabled during calibration.')
    if args.calibrate and not args.bench_validated_bpm:
        parser.error('Calibration requires --bench-validated-bpm after verifying BPM independently.')
    print('USB bench test only; disconnect all body-attached electrodes.', flush=True)
    print('Ports:', ', '.join(p.device for p in list_ports.comports()), flush=True)
    calibration = []
    calibrated = not args.calibrate
    rows = []
    try:
        with serial.Serial(args.port, args.baud, timeout=0.5, write_timeout=1) as dev:
            time.sleep(2)
            dev.reset_input_buffer()
            start = time.monotonic()
            last = start - 1
            last_sample = start
            last_drops = None
            valid_now = False
            feedback = b'SENTRY WAIT\n'
            print(f'Connected {args.port}; live telemetry and 60-second AI windows.', flush=True)
            while True:
                line = dev.readline().decode('utf-8', errors='ignore').strip()
                data = parse(line) if line else None
                t = time.monotonic()
                if data is None and args.feedback and feedback != b'SENTRY WAIT\n' and t - last_sample > 3:
                    feedback = b'SENTRY WAIT\n'
                    dev.write(feedback)
                if data is not None:
                    drops = data.get('fifo_drops')
                    valid_now = (
                        data['hr_bpm'] is not None and data['motion'] == 'STILL'
                        and 50000 <= data['ir_raw'] <= 250000
                        and (last_drops is None or drops == last_drops)
                    )
                    if args.feedback and feedback != b'SENTRY WAIT\n' and not valid_now:
                        feedback = b'SENTRY WAIT\n'
                        dev.write(feedback)
                    last_sample = t
                    last_drops = drops
                    rows.append(data)
                    if t-last >= 1:
                        last = t
                        if args.calibrate:
                            data['calibration_windows'] = len(calibration)
                        if post('/telemetry', data) is not None:
                            pulse_state = data.get('pulse_state')
                            diagnostic = (f" | Pulse: {pulse_state} | IR span: {data.get('ir_span')} | "
                                          f"Interval: {data.get('interval_samples')} samples") if pulse_state else ''
                            print(f"Telemetry OK | BPM: {data['hr_bpm']} | IR: {data['ir_raw']} | "
                                  f"Motion: {data['motion']} | ADC: {data['bioamp_adc']} | "
                                  f"FIFO drops: {data.get('fifo_drops')}{diagnostic}", flush=True)
                if t-start < WINDOW:
                    continue
                summary = minute_summary(rows, args.bench_validated_bpm)
                rows = []
                start = time.monotonic()
                if not calibrated:
                    if summary['quality_ok']:
                        calibration.append(summary)
                        print(f'Calibration: {len(calibration)}/30 valid resting windows', flush=True)
                        if len(calibration) >= 30:
                            result = post('/baseline', {'readings': calibration})
                            if result and result.get('status') == 'calibrated_demo':
                                calibrated = True
                                print('Experimental baseline ready:', result, flush=True)
                            else:
                                print('Calibration rejected; stopping. Inspect server error.', flush=True)
                                return
                    else:
                        print('Calibration paused: insufficient verified resting BPM.', flush=True)
                else:
                    result = post('/readings', summary)
                    if args.feedback:
                        feedback = (feedback_command(result, summary)
                                    if valid_now and time.monotonic() - last_sample <= 3
                                    else b'SENTRY WAIT\n')
                        dev.write(feedback)
                    print('AI minute:', result, flush=True)
    except serial.SerialException as exc:
        print(f'Serial bridge could not use {args.port}: {exc}. The dashboard can still run.', flush=True)
    except KeyboardInterrupt:
        print('Serial bridge stopped.', flush=True)


if __name__ == '__main__':
    main()
