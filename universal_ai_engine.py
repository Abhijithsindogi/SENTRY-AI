"""SENTRY AI: experimental, separate-source assessments. Local prototype only.
Register: app.include_router(build_router(get_latest)) after existing routes.
Sekyo uses historical distribution, not resting baseline or continuous prediction.
ESP32 retains its original calibrated minute-level engine.
"""
from datetime import datetime, timezone
from functools import lru_cache
import math
import numpy as np
from sklearn.ensemble import IsolationForest
from fastapi import APIRouter, HTTPException
from sekyo_import import OUTPUT, load_history
from universal_ble_bridge import get_ble

NOTICE = ('Experimental comparison of device-reported values, not a diagnosis, '
          'clinical risk score or emergency alert. Activity context and sensor accuracy are unverified.')

@lru_cache(maxsize=4)
def fit_sekyo(version):
    h = load_history()
    grouped = {}
    for row in h['daily_series']['heart_rate_bpm']:
        day = row['record_date']
        # Exact per-slot timestamps are unknown; deduplicate duplicate daily arrays
        # by keeping the first record for each day, rather than inventing timestamps.
        if day not in grouped:
            grouped[day] = [float(v) for v in row['slot_values']
                            if isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) and 30 <= v <= 220]
    days = sorted(d for d, values in grouped.items() if len(values) >= 10)
    # Reserve the most recent sufficiently populated day for an independent replay.
    train = days[:-1]
    test = days[-1] if days else None
    x = np.asarray([v for d in train for v in grouped[d]], dtype=float)
    if len(train) < 3 or len(x) < 30:
        return None
    median = float(np.median(x))
    scale = max(2., float(1.4826*np.median(np.abs(x-median))))
    model = IsolationForest(n_estimators=100, random_state=42, contamination='auto', n_jobs=1)
    model.fit(((x-median)/scale).reshape(-1,1))
    profile = dict(patient_id=h['patient_id'], reference_source=h['source'],
                   reference_type='mixed_context_historical_distribution',
                   training_days=train, training_readings=len(x), median_bpm=median,
                   robust_scale_bpm=round(scale,3), held_out_day=test,
                   held_out_readings=len(grouped[test]), clinical_validation=False)
    return profile, model, median, scale, grouped[test]

def reference():
    try:
        return fit_sekyo(OUTPUT.stat().st_mtime_ns)
    except (OSError, HTTPException, KeyError, TypeError, ValueError):
        return None

def compare_readings(values, fit):
    _, model, median, scale, _ = fit
    values = np.asarray(values, dtype=float)
    deviations = (values - median) / scale
    scores = model.decision_function(deviations.reshape(-1, 1))
    results = []
    for bpm, z, score in zip(values, deviations, scores):
        unusual = abs(z) >= 4 or (abs(z) >= 2 and score < 0)
        results.append(dict(status='experimental_deviation' if unusual else 'no_large_deviation_in_sample',
                            hr_bpm=float(bpm), difference_bpm=round(float(bpm - median), 2),
                            robust_z=round(float(z), 3), model_decision=round(float(score), 4),
                            caregiver_check_in=False))
    return results


def compare(bpm, fit):
    return compare_readings([bpm], fit)[0]


def assess_sekyo(patient_id):
    from wearable_capabilities import ble_strategy, packet_cadence

    live = get_ble(patient_id=patient_id, source='sekyo_flex_ble')
    fit = reference()
    profile = fit[0] if fit and fit[0]['patient_id']==patient_id else None
    result = dict(source='sekyo_flex_ble', mode='live', status=live.get('status'),
                  timestamp=live.get('timestamp'), age_seconds=live.get('age_seconds'),
                  hr_bpm=live.get('hr_bpm'), sensor_contact=live.get('sensor_contact'),
                  quality_ok=False, caregiver_check_in=False, reference=profile, notice=NOTICE)
    cadence, _ = packet_cadence('ble', 'sekyo_flex_ble') if patient_id == 'patient_001' else (None, False)
    result['evidence'] = ble_strategy('sekyo_flex_ble', live, cadence, bool(profile))
    result['evidence'].update(
        contact=live.get('sensor_contact'), motion=None, freshness_limit_seconds=30,
        available_rr_count=len(live.get('rr_intervals_ms') or []),
    )
    if live.get('status') != 'receiving':
        return result
    if not profile:
        result['status']='historical_reference_required'; return result
    bpm = live.get('hr_bpm')
    if live.get('sensor_contact') is not True or not isinstance(bpm,(float,int)) or not 30<=bpm<=220:
        result['status']='insufficient_data'; return result
    try:
        t = datetime.fromisoformat(live['timestamp'])
        if t.tzinfo is None or abs((datetime.now(timezone.utc)-t).total_seconds())>30:
            result['status']='stale_or_future_data'; return result
    except (ValueError, KeyError, TypeError):
        result['status']='insufficient_data'; return result
    result.update(compare(bpm,fit))
    return result

def build_router(get_latest):
    router = APIRouter(prefix='/wearables/ai', tags=['Universal AI prototype'])
    @router.get('/summary')
    def summary(patient_id: str='patient_001'):
        esp = [dict(row) for row in get_latest() if row.get('patient_id')==patient_id
               and row.get('source')=='sentry_wearable' and row.get('mode')=='live']
        for row in esp:
            try:
                stamp=datetime.fromisoformat(row['timestamp'])
                if stamp.tzinfo is None or abs((datetime.now(timezone.utc)-stamp).total_seconds())>120:
                    row.update(status='stale', caregiver_check_in=False)
            except (ValueError, KeyError, TypeError):
                row.update(status='unknown_freshness', caregiver_check_in=False)
        return {'patient_id':patient_id,'esp32':esp or [{'status':'waiting_for_data','caregiver_check_in':False}],
                'sekyo':assess_sekyo(patient_id),'notice':NOTICE}
    @router.get('/sekyo/replay')
    def replay(patient_id: str='patient_001'):
        fit=reference()
        if not fit or fit[0]['patient_id']!=patient_id:
            return {'status':'historical_reference_required','notice':NOTICE}
        readings = compare_readings(fit[-1], fit)
        return {'mode':'historical_replay','reference':fit[0],
                'deviation_count':sum(r['status']=='experimental_deviation' for r in readings),
                'readings':readings,'notice':NOTICE+' No clinical labels; replay is not an accuracy test.'}
    return router
