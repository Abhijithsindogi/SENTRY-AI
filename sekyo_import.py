"""Import Da Halo ZIP exports into SENTRY AI as historical, unvalidated records.
CLI: python sekyo_import.py path_to_export.zip
FastAPI: from sekyo_import import router; app.include_router(router)
No raw profile, device identifiers or GPS data are copied. No clinical scoring.
"""
import argparse
import csv
import io
import json
import math
import statistics
import zipfile
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path(__file__).resolve().with_name('sekyo_history.json')
SERIES = {
    'heart_rate_bpm': ('timing_heart_rate_entity.csv', 'HEART_RATE'),
    'hrv_reported': ('timing_hrv_entity.csv', 'HRV_STR'),
    'spo2_percent': ('timing_blood_oxygen_entity.csv', 'BLOOD_OXYGEN_STR'),
    'temperature_reported': ('timing_temperature_entity.csv', 'TEMPERATURE_STR'),
    'skin_temperature_reported': ('timing_temperature_entity.csv', 'SKIN_TEMPERATURE_STR'),
    'step_slots': ('timing_steps_entity.csv', 'STEPS'),
}
POINTS = {
    'heart_rate_bpm': ('heart_rate_entity.csv', 'HEART_RATE'),
    'hrv_reported': ('hrv_entity.csv', 'HRV'),
    'spo2_percent': ('blood_oxygen_entity.csv', 'BLOOD_OXYGEN'),
    'stress_reported': ('stress_entity.csv', 'STRESS'),
}


def rows(z, name):
    if name not in z.namelist():
        return []
    if z.getinfo(name).file_size > 20_000_000:
        raise ValueError(f'Unexpectedly large CSV: {name}')
    return list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))


def num(x):
    try:
        n = float(x)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def date_field(row, key='DATE'):
    val = row.get(key)
    if not val:
        return None
    try:
        return datetime.fromisoformat(val).isoformat()
    except ValueError:
        return None


def array(x, zero_missing):
    try:
        a = json.loads(x)
    except (TypeError, ValueError):
        return []
    if not isinstance(a, list):
        return []
    v = [num(y) for y in a]
    return [None if zero_missing and y == 0 else y for y in v]


def stats(values):
    v = [x for x in values if x is not None]
    return {'count': len(v), 'median': round(statistics.median(v), 2) if v else None,
            'min': min(v) if v else None, 'max': max(v) if v else None}


def import_export(path):
    history = {'schema_version': 1, 'patient_id': 'patient_001', 'source': 'sekyo_flex',
               'mode': 'historical_import', 'quality_ok': False,
               'imported_at': datetime.now(timezone.utc).isoformat(),
               'limitations': ['Device measurements are not independently validated.',
                 'Array index time-of-day mapping and HRV/temperature units are unverified.',
                 'Zero vital values treated as missing; zero step slots retained with unknown wear status.',
                 'DATE is the vendor record date, not an inferred measurement time. Separate readings preserve vendor DATE and RAW_TIME.',
                 'No live alerts, training or clinical inference from this import.'],
               'daily_series': {}, 'separate_readings': {}, 'daily_activity': [],
               'sleep_records': [], 'exercise_records': []}
    with zipfile.ZipFile(path) as z:
        if 'timing_heart_rate_entity.csv' not in z.namelist():
            raise ValueError('Expected Da Halo heart-rate CSV not found in ZIP.')
        for metric,(name,key) in SERIES.items():
            entries = []
            for row in rows(z,name):
                ts = date_field(row)
                if not ts:
                    continue
                vals = array(row.get(key), metric != 'step_slots')
                entries.append({'record_date': ts[:10], 'vendor_date': ts,
                                'slot_values': vals, 'slot_count': len(vals),
                                'nonzero_count': sum(v is not None and v > 0 for v in vals)})
            history['daily_series'][metric] = sorted(entries,key=lambda r:r['vendor_date'])
        for metric,(name,key) in POINTS.items():
            entries = []
            for row in rows(z,name):
                ts, v = date_field(row), num(row.get(key))
                if ts and v is not None and (v > 0 or metric == 'stress_reported'):
                    entries.append({'vendor_date':ts,'vendor_raw_time':date_field(row,'RAW_TIME'),'value':v})
            history['separate_readings'][metric] = sorted(entries,key=lambda r:r['vendor_date'])
        for row in rows(z,'sport.csv'):
            ts=date_field(row)
            if ts:
                history['daily_activity'].append({'record_date':ts[:10], 'vendor_date':ts,'steps':num(row.get('STEPS')), 'vendor_update_date':date_field(row,'UPDATE_DATE')})
        for row in rows(z,'sleep_entity.csv'):
            ts=date_field(row)
            if ts:
                stages={k.lower():num(row.get(k)) for k in ('DEEP','LIGHT','REM','AWAKE')}
                history['sleep_records'].append({'vendor_date':ts,'record_date':ts[:10], 'stages_reported':stages,'nonzero_sleep_stage':any((stages[k] or 0)>0 for k in ('deep','light','rem'))})
        for row in rows(z,'gpsrun.csv'):
            start=date_field(row,'START_DATE'); end=date_field(row,'END_DATE')
            if start:
                history['exercise_records'].append({'vendor_start':start,'vendor_end':end,'steps':num(row.get('STEP'))})
    for key in ('daily_activity','sleep_records'):
        history[key].sort(key=lambda r:r['vendor_date'])
    history['exercise_records'].sort(key=lambda r:r['vendor_start'])
    history['summary'] = {'automatic_nonzero_counts': {k:sum(r['nonzero_count'] for r in v) for k,v in history['daily_series'].items()},
        'separate_reading_counts': {k:len(v) for k,v in history['separate_readings'].items()},
        'daily_activity_records':len(history['daily_activity']), 'sleep_records':len(history['sleep_records']),
        'nonzero_sleep_stage_records':sum(r['nonzero_sleep_stage'] for r in history['sleep_records']),
        'exercise_records':len(history['exercise_records']),
        'daily_heart_rate': [{'record_date':r['record_date'], **stats(r['slot_values'])} for r in history['daily_series']['heart_rate_bpm']]}
    return history


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('zip_path',type=Path)
    a=p.parse_args()
    try:
        data=import_export(a.zip_path)
        tmp=OUTPUT.with_suffix('.tmp')
        tmp.write_text(json.dumps(data,indent=2,allow_nan=False),encoding='utf-8')
        tmp.replace(OUTPUT)
    except (OSError,ValueError,zipfile.BadZipFile,csv.Error) as exc:
        p.exit(1,f'Import failed: {exc}\n')
    print('Sekyo FLEX historical import successful.')
    print(json.dumps(data['summary'],indent=2))
    print('Saved:', OUTPUT)
    print('Historical import only. Live acquisition and clinical validation are pending.')


if __name__ == '__main__':
    main()
else:
    from fastapi import APIRouter, HTTPException
    router=APIRouter(prefix='/wearables/sekyo',tags=['Sekyo historical import'])

    def load_history():
        try:
            return json.loads(OUTPUT.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            raise HTTPException(status_code=404,detail='Run sekyo_import.py with a Da Halo export first.')

    @router.get('/history')
    def sekyo_history():
        return load_history()

    @router.get('/summary')
    def sekyo_summary():
        h=load_history()
        return {k:h[k] for k in ('patient_id','source','mode','quality_ok','imported_at','summary','limitations')}
