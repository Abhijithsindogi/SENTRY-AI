"""Local hackathon prototype, not a medical device."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
import json
import os
from functools import lru_cache
from pathlib import Path
from threading import RLock
import numpy as np
from pydantic import BaseModel, Field
from sklearn.ensemble import IsolationForest
from fastapi import FastAPI, HTTPException

from telegram_notifications import queue_notifications, telegram_worker
import family_connect


@asynccontextmanager
async def lifespan(_app):
    workers = [asyncio.create_task(telegram_worker()), asyncio.create_task(family_connect.worker())]
    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        from device_api import disconnect
        await disconnect()


app = FastAPI(title='SENTRY AI — prototype', lifespan=lifespan)
FEATURES = ('hr_bpm', 'motion_rms', 'emg_rms')
from caregiver_alerts import database, inspect_in_transaction, router as caregiver_router

state_lock = RLock()
RETENTION_DAYS = max(1, int(os.getenv('SENTRY_ASSESSMENT_RETENTION_DAYS', '30')))


@lru_cache(maxsize=32)
def fit_model(training_json):
    values = np.asarray(json.loads(training_json), dtype=float)
    return IsolationForest(n_estimators=100, contamination='auto', random_state=42).fit(values)


def stream_key(reading):
    return json.dumps(reading.key())


def profile_key(reading, features):
    return json.dumps((*reading.key(), features))

class WindowEvidence(BaseModel):
    total_packets: int = Field(ge=0, le=10000)
    bpm_packets: int = Field(ge=0, le=10000)
    motion: Literal['still', 'movement_or_unknown', 'no_samples']
    fifo_uninterrupted: bool
    independent_opt_in: bool


class Reading(BaseModel):
    patient_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    mode: Literal['live', 'simulation']
    context: Literal['rest', 'active', 'emg_check']
    timestamp: datetime
    quality_ok: bool
    window_evidence: WindowEvidence | None = None
    hr_bpm: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    motion_rms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    emg_rms: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)

    def key(self):
        return (self.patient_id, self.source, self.mode, self.context)

class Calibration(BaseModel):
    readings: list[Reading]


def timestamp(r):
    if r.timestamp.tzinfo is None:
        raise HTTPException(400, 'Timestamp must include timezone')
    return r.timestamp.timestamp()


def available(r):
    # EMG is only interpreted in a repeatable, standardized activity.
    return tuple(f for f in FEATURES if getattr(r, f) is not None and (f != 'emg_rms' or r.context == 'emg_check'))

@app.post('/baseline')
def baseline(data: Calibration):
    rows = data.readings
    if not 30 <= len(rows) <= 1440:
        raise HTTPException(400, 'Provide 30 to 1440 good, representative calibration windows')
    key = rows[0].key()
    fs = available(rows[0])
    if 'hr_bpm' not in fs:
        raise HTTPException(400, 'Heart rate is required for this starter')
    if any(r.key() != key or not r.quality_ok or available(r) != fs for r in rows):
        raise HTTPException(400, 'Use one patient/source/mode/context and matching valid features')
    times = [timestamp(r) for r in rows]
    if any(b <= a for a,b in zip(times,times[1:])):
        raise HTTPException(400, 'Calibration windows must have unique increasing timestamps')
    if rows[0].mode == 'live':
        if times[-1] > datetime.now(timezone.utc).timestamp():
            raise HTTPException(400, 'Calibration cannot include future observations')
        if any(b - a < 50 for a, b in zip(times, times[1:])):
            raise HTTPException(400, 'Use non-overlapping minute windows for live calibration')
    x = np.array([[getattr(r,f) for f in fs] for r in rows])
    med = np.median(x, axis=0)
    # Demo noise floors, not clinical limits; tune to each calibrated sensor.
    floors = {'hr_bpm': 2.0, 'motion_rms': 0.02, 'emg_rms': 0.01}
    scale = np.maximum(1.4826*np.median(np.abs(x-med),axis=0), [floors[f] for f in fs])
    training = json.dumps(((x - med) / scale).tolist(), allow_nan=False)
    fit_model(training)
    profile = dict(features=fs, median=med.tolist(), scale=scale.tolist(), training=training,
                   last=times[-1], ewma=0.0, streak=0, windows=len(rows))
    with state_lock, database() as con:
        con.execute('INSERT OR REPLACE INTO ai_profiles VALUES (?,?)',
                    (profile_key(rows[0], fs), json.dumps(profile)))
        con.execute('DELETE FROM ai_latest WHERE key=?', (stream_key(rows[0]),))
    return {'status':'calibrated_demo', 'windows':len(rows), 'baseline':dict(zip(fs, med.tolist()))}

@app.post('/readings')
def reading(r: Reading):
    t = timestamp(r)
    with state_lock, database() as con:
        con.execute('BEGIN IMMEDIATE')
        result = assess_reading(con, r, t)
        event_ids = inspect_in_transaction(con, [result])
        queue_notifications(con, event_ids)
        family_connect.queue(con, event_ids)
    return result


def assess_reading(con, r, t):
    key = stream_key(r)
    features = available(r)
    profile_id = profile_key(r, features)
    record = con.execute('SELECT payload FROM ai_profiles WHERE key=?', (profile_id,)).fetchone()
    p = json.loads(record['payload']) if record else None
    previous = con.execute('SELECT observed, payload FROM ai_latest WHERE key=?', (key,)).fetchone()
    clock = datetime.now(timezone.utc).timestamp()
    result = {'patient_id': r.patient_id, 'source': r.source, 'mode': r.mode, 'context': r.context,
              'timestamp': r.timestamp.isoformat(), 'caregiver_check_in': False,
              'quality_ok': r.quality_ok, 'measurements': {f: getattr(r, f) for f in features}}

    result['evidence'] = {
        'signals': list(features),
        'cadence_seconds': 60,
        'freshness_limit_seconds': 120 if r.mode == 'live' else None,
        'age_seconds_at_assessment': round(clock - t, 2) if r.mode == 'live' else None,
        'required_persistence': 3,
        'window': r.window_evidence.model_dump() if r.window_evidence else None,
        'model': 'Personal feature-matched Isolation Forest and robust deviations',
    }

    def finish(status, **details):
        result.update(status=status, **details)
        payload = json.dumps(result, allow_nan=False)
        if p is not None:
            con.execute('UPDATE ai_profiles SET payload=? WHERE key=?',
                        (json.dumps(p, allow_nan=False), profile_id))
        con.execute('INSERT OR REPLACE INTO ai_latest VALUES (?,?,?)', (key, t, payload))
        con.execute('INSERT INTO ai_assessments VALUES (?,?,?,?)', (key, t, clock, payload))
        con.execute('DELETE FROM ai_assessments WHERE received < ?', (clock - RETENTION_DAYS * 86400,))
        return result

    if r.mode == 'live' and not 0 <= clock - t <= 120:
        return {**result, 'status': 'stale_or_future_data'}
    if previous and t <= previous['observed']:
        raise HTTPException(409, 'Duplicate or out-of-order observation')
    if not r.quality_ok or 'hr_bpm' not in features:
        return finish('insufficient_data', reasons=['The window did not pass signal-quality and usable BPM checks'])
    if not p:
        return finish('baseline_required', reasons=['No personal baseline matched the device, context and available features'])
    gap = t - p['last']
    if gap <= 0:
        raise HTTPException(409, 'Duplicate or out-of-order observation')
    if gap < 50:
        raise HTTPException(400, 'Send one non-overlapping minute summary at a time')
    p['last'] = t
    last_result = json.loads(previous['payload']) if previous else None
    if gap > 90 or (last_result and (
        set(last_result['measurements']) != set(features) or not last_result['quality_ok']
    )):
        p['streak'] = 0
        p['ewma'] = 0.0
    x = np.asarray([getattr(r, f) for f in features])
    z = (x - p['median']) / p['scale']
    distance = float(np.mean(np.abs(z)))
    p['ewma'] = 0.3 * distance + 0.7 * p['ewma']
    decision = float(fit_model(p['training']).decision_function(z.reshape(1, -1))[0])
    conditions = {
        'Isolation Forest and mean deviation passed the anomaly gate': decision < 0 and distance >= 2,
        'At least one feature reached four robust deviation units': np.max(np.abs(z)) >= 4,
        'Smoothed deviation reached the trend threshold': p['ewma'] >= 2.5,
    }
    candidate = any(conditions.values())
    result['evidence']['baseline_windows'] = p['windows']
    result['evidence']['observed_gap_seconds'] = round(gap, 2)
    result['evidence']['matched_conditions'] = [label for label, met in conditions.items() if met]
    result['evidence']['signed_deviations'] = {
        feature: round(float(value), 3) for feature, value in zip(features, z)
    }
    p['streak'] = p['streak'] + 1 if candidate else 0
    reasons = [f'{f}: {v:.2f} vs baseline {b:.2f}'
               for f, v, b, d in zip(features, x, p['median'], z) if abs(d) >= 2]
    return finish('check_in_suggested' if p['streak'] >= 3 else 'observe' if candidate else 'no_deviation_detected',
                  caregiver_check_in=p['streak'] >= 3, anomaly_distance=round(distance, 3),
                  model_decision=round(decision, 3), trend=round(p['ewma'], 3),
                  consecutive_windows=p['streak'], reasons=reasons,
                  baseline=dict(zip(features, p['median'])),
                  notice='Experimental anomaly flag, not clinical risk or diagnosis')


@app.get('/baseline')
def get_baselines():
    with database() as con:
        rows = con.execute('SELECT key, payload FROM ai_profiles').fetchall()
    summaries = []
    for row in rows:
        patient, source, mode, context, features = json.loads(row['key'])
        profile = json.loads(row['payload'])
        summaries.append(dict(patient_id=patient, source=source, mode=mode, context=context,
                              windows=profile['windows'], baseline=dict(zip(features, profile['median']))))
    return summaries


@app.get('/latest')
def get_latest():
    with database() as con:
        rows = con.execute('SELECT payload FROM ai_latest ORDER BY observed DESC').fetchall()
    return [json.loads(row['payload']) for row in rows]

from measurement_store import SerialReading, latest_sample, save_live


@app.post('/telemetry')
def receive_telemetry(data: SerialReading):
    save_live('serial', data, max_age=30)
    return {'status': 'received', 'data': data.model_dump(mode='json')}


@app.get('/telemetry')
def get_telemetry(patient_id: str = 'patient_001', source: str = 'sentry_wearable'):
    return latest_sample('serial', patient_id, source) or {}


from sekyo_import import router
app.include_router(router)
from fastapi.responses import FileResponse

@app.get("/")
def dashboard():
    return FileResponse(
        Path(__file__).parent / "dashboard.html"
    )
from universal_ble_bridge import router as ble_router
app.include_router(ble_router)
from universal_ai_engine import build_router
app.include_router(build_router(get_latest))
app.include_router(caregiver_router)

@app.get('/caregiver-ui.js', include_in_schema=False)
def caregiver_ui():
    return FileResponse(Path(__file__).with_name('caregiver-ui.js'), media_type='text/javascript')

from history_api import router as history_router
app.include_router(history_router)

from local_account import access_control, router as account_router
app.middleware('http')(access_control)
app.include_router(account_router)

from device_api import router as device_router
app.include_router(device_router)

@app.get('/workspace-ui.js', include_in_schema=False)
def workspace_ui():
    return FileResponse(Path(__file__).with_name('workspace-ui.js'), media_type='text/javascript')


@app.post('/wearables/alerts/demo/run')
def run_guided_demo():
    from caregiver_alerts import insert_event, now

    start = datetime.now(timezone.utc) - timedelta(minutes=40)

    def synthetic(index, bpm):
        return Reading(patient_id='demo_patient', source='guided_demo', mode='simulation',
                       context='rest', timestamp=start + timedelta(minutes=index),
                       quality_ok=True, hr_bpm=bpm)

    with state_lock:
        calibration = baseline(Calibration(readings=[synthetic(i, 71 + i % 3) for i in range(30)]))
        trajectory = [reading(synthetic(i, bpm)) for i, bpm in enumerate([72, 73, 96, 99, 102], 30)]
        if not trajectory[-1]['caregiver_check_in']:
            raise HTTPException(500, 'Simulation failed to reach a persistent deviation')
        with database() as con:
            event_id = insert_event(con, 'simulation', 'demo_patient', 'guided_demo', now(),
                                    'SIMULATION. The model flagged three consecutive synthetic deviations.')
    return {'mode': 'simulation', 'id': event_id, 'calibration': calibration, 'trajectory': trajectory}

@app.get('/measurements/live')
def live_history(channel: Literal['serial', 'ble'] = 'serial', patient_id: str = 'patient_001',
                 source: str = 'sentry_wearable', limit: int = 100):
    if not 1 <= limit <= 1000:
        raise HTTPException(422, 'Limit must be between 1 and 1000')
    with database() as con:
        rows = con.execute(
            'SELECT payload FROM live_samples WHERE channel=? AND patient=? AND source=? ORDER BY observed DESC LIMIT ?',
            (channel, patient_id, source, limit),
        ).fetchall()
    return {'mode': 'live', 'items': [json.loads(row['payload']) for row in rows]}


app.include_router(family_connect.router)


@app.get("/family-ui.js", include_in_schema=False)
def family_ui():
    return FileResponse(Path(__file__).with_name("family-ui.js"), media_type="text/javascript")

@app.get('/research/{comparison}.pdf')
def research_pdf(comparison: Literal['offline', 'causal', 'causal-followup']):
    reports = {
        'offline': ('ppg-with-independent-recordings', 'comparison.pdf'),
        'causal': ('ppg-causal', 'waveform-01.pdf'),
        'causal-followup': ('ppg-causal-two-captures', 'waveform-02.pdf'),
    }
    directory, file = reports[comparison]
    path = Path(__file__).parent / 'reports' / directory / file
    if not path.is_file():
        raise HTTPException(404, 'This research plot is available only with the original local research files')
    return FileResponse(path, media_type='application/pdf')


@app.get('/language-ui.js', include_in_schema=False)
def language_ui():
    return FileResponse(Path(__file__).with_name('language-ui.js'), media_type='text/javascript')
