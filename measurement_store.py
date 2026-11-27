"""Validated local acquisition storage with bounded live retention."""
import json
import os
import time
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from caregiver_alerts import database

RETENTION_SECONDS = max(1, int(os.getenv('SENTRY_SENSOR_RETENTION_DAYS', '30'))) * 86400


class SerialReading(BaseModel):
    patient_id: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    mode: Literal['live']
    timestamp: datetime
    hr_bpm: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ir_raw: int | None = Field(default=None, ge=0)
    acceleration: float | None = Field(default=None, allow_inf_nan=False)
    motion: str | None = Field(default=None, max_length=50)
    bioamp_adc: int | None = Field(default=None, ge=0)
    emg_rms: None = None
    quality_ok: Literal[False] = False
    fifo_drops: int | None = Field(default=None, ge=0)
    pulse_state: str | None = Field(default=None, max_length=40)
    ir_span: int | None = Field(default=None, ge=0)
    beat_triggers: int | None = Field(default=None, ge=0)
    interval_samples: int | None = Field(default=None, ge=0)
    early_triggers: int | None = Field(default=None, ge=0)
    late_intervals: int | None = Field(default=None, ge=0)
    inconsistent_intervals: int | None = Field(default=None, ge=0)
    calibration_windows: int | None = Field(default=None, ge=0, le=30)


def save_live(channel, reading, max_age):
    stamp = reading.timestamp
    if stamp.tzinfo is None:
        raise HTTPException(400, 'Timestamp must include timezone')
    observed = stamp.timestamp()
    received = time.time()
    if not 0 <= received - observed <= max_age:
        raise HTTPException(400, 'Measurement timestamp is outside the freshness window')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        previous = con.execute(
            'SELECT observed FROM live_samples WHERE channel=? AND patient=? AND source=? ORDER BY observed DESC LIMIT 1',
            (channel, reading.patient_id, reading.source),
        ).fetchone()
        if previous and observed <= previous['observed']:
            raise HTTPException(409, 'Duplicate or out-of-order measurement')
        con.execute('INSERT INTO live_samples VALUES (?,?,?,?,?)', (
            channel, reading.patient_id, reading.source, observed, reading.model_dump_json(),
        ))
        con.execute('DELETE FROM live_samples WHERE observed < ?', (received - RETENTION_SECONDS,))


def latest_sample(channel, patient='patient_001', source='sentry_wearable'):
    with database() as con:
        row = con.execute(
            'SELECT payload FROM live_samples WHERE channel=? AND patient=? AND source=? ORDER BY observed DESC LIMIT 1',
            (channel, patient, source),
        ).fetchone()
    return json.loads(row['payload']) if row else None
