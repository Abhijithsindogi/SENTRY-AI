"""Evidence-based signal inventory for the installed wearable adapters."""
from datetime import datetime, timezone
import json
from statistics import median

from fastapi import HTTPException

from caregiver_alerts import database
from measurement_store import latest_sample
from sekyo_import import load_history

SIGNALS = ('heart_rate', 'rr_intervals', 'hrv', 'spo2', 'motion', 'sleep',
           'skin_temperature', 'steps', 'optical_ir', 'raw_electrode_adc', 'battery')


def gatt(short):
    return f'0000{short}-0000-1000-8000-00805f9b34fb'


def discover_gatt(collection):
    def supported(short, modes):
        item = collection.get_characteristic(gatt(short))
        return bool(item and set(item.properties) & modes)

    return {
        'heart_rate': supported('2a37', {'notify'}),
        'spo2': supported('2a5e', {'indicate'}) or supported('2a5f', {'notify'}),
        'steps': supported('2b40', {'notify', 'indicate', 'read'}),
        'sleep': supported('2b41', {'notify', 'indicate', 'read'}) or supported('2b42', {'notify', 'indicate', 'read'}),
        'motion': supported('2b3c', {'notify', 'indicate', 'read'}),
        'battery': supported('2a19', {'read'}),
    }


def inventory():
    return {signal: {'state': 'unknown', 'detail': 'No evidence from this adapter'} for signal in SIGNALS}


def put(matrix, signal, state, detail):
    matrix[signal] = {'state': state, 'detail': detail}


def packet_cadence(channel, source):
    with database() as con:
        rows = con.execute(
            'SELECT observed, payload FROM live_samples WHERE channel=? AND patient=? AND source=? '
            'ORDER BY observed DESC LIMIT 12', (channel, 'patient_001', source),
        ).fetchall()
    gaps = [newer['observed'] - older['observed'] for newer, older in zip(rows, rows[1:])]
    cadence = round(median(gaps), 2) if gaps else None
    rr_observed = any(any(value > 0 for value in json.loads(row['payload']).get('rr_intervals_ms', []))
                      for row in rows)
    return cadence, rr_observed


def ble_inventory(connection, sample, cadence, rr_observed):
    matrix = inventory()
    detected = connection.get('gatt_features', {})
    for signal, found in detected.items():
        if found:
            detail = 'Standard GATT characteristic discovered. Live decoding is unavailable.'
            if signal in {'heart_rate', 'battery'}:
                detail = 'Standard GATT characteristic discovered. Waiting for a usable value.'
            put(matrix, signal, 'detected', detail)
    if sample.get('status') == 'receiving' and sample.get('hr_bpm') is not None:
        put(matrix, 'heart_rate', 'observed', 'A fresh heart-rate packet contains a value')
    elif sample.get('timestamp') and sample.get('status') == 'stale':
        put(matrix, 'heart_rate', 'stale', 'Latest heart-rate packet is out of date')
    if rr_observed:
        rr_values = sample.get('rr_intervals_ms', [])
        current = sample.get('status') == 'receiving' and any(value > 0 for value in rr_values)
        put(matrix, 'rr_intervals', 'observed' if current else 'previously_observed',
            'Optional RR values have appeared in heart-rate packets')
    if connection.get('battery_percent') is not None:
        put(matrix, 'battery', 'observed', 'Read from the device battery characteristic')
    put(matrix, 'hrv', 'unavailable', 'Live HRV estimation is unvalidated; RR values alone do not enable scoring')
    return {'name': connection.get('name') or 'Commercial BLE wearable', 'signals': matrix,
            'packet_interval_median_seconds': cadence}


def serial_inventory():
    sample = latest_sample('serial') or {}
    matrix = inventory()
    mapping = {'heart_rate': 'hr_bpm', 'motion': 'motion', 'optical_ir': 'ir_raw',
               'raw_electrode_adc': 'bioamp_adc'}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(sample['timestamp'])).total_seconds()
    except (ValueError, TypeError, KeyError):
        age = None
    for signal, field in mapping.items():
        if sample.get(field) is not None and sample.get(field) != 'ERROR':
            quality = 'Raw ADC only' if signal == 'raw_electrode_adc' else 'Experimental serial measurement'
            put(matrix, signal, 'observed' if age is not None and 0 <= age <= 10 else 'stale', quality)
    for signal in ('rr_intervals', 'hrv', 'spo2', 'sleep', 'skin_temperature', 'steps'):
        put(matrix, signal, 'unavailable', 'The current serial protocol does not provide this signal')
    return {'name': 'Custom ESP32 wearable', 'signals': matrix,
            'packet_interval_median_seconds': packet_cadence('serial', 'sentry_wearable')[0]}


def historical_inventory():
    matrix = inventory()
    try:
        history = load_history()
    except HTTPException:
        history = {}
    summary = history.get('summary', {})
    counts = summary.get('automatic_nonzero_counts', {})
    separate = summary.get('separate_reading_counts', {})
    for signal, column in {'heart_rate': 'heart_rate_bpm', 'hrv': 'hrv_reported',
                           'spo2': 'spo2_percent', 'skin_temperature': 'skin_temperature_reported',
                           'steps': 'step_slots'}.items():
        if counts.get(column) or separate.get(column):
            put(matrix, signal, 'historical', 'Present in imported Da Halo records')
    if summary.get('nonzero_sleep_stage_records'):
        put(matrix, 'sleep', 'historical', 'Present in imported sleep summaries')
    return {'name': 'Imported Da Halo history', 'signals': matrix}


def ble_strategy(source, sample, cadence, has_reference):
    result = {'strategy': 'wait_for_fresh_bpm', 'uses': [], 'cadence_seconds': cadence,
              'persistence': 'Single sample; no caregiver alert', 'reference_available': has_reference,
              'cadence_class': 'unknown' if cadence is None else 'sparse' if cadence > 30 else 'observed_recently'}
    if sample.get('status') != 'receiving' or sample.get('hr_bpm') is None:
        return result
    if sample.get('sensor_contact') is not True or not 30 <= sample['hr_bpm'] <= 220:
        return {**result, 'strategy': 'wait_for_usable_contact_and_rate'}
    if source == 'sekyo_flex_ble' and has_reference:
        strategy = 'sparse_historical_hr_preview' if cadence is not None and cadence > 30 else 'historical_hr_comparison'
        return {**result, 'strategy': strategy, 'uses': ['heart_rate']}
    return {**result, 'strategy': 'display_only_no_device_reference', 'uses': ['heart_rate']}
