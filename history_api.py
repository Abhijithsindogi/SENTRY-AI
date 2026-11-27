"""Local historical imports, aggregation, and downloadable reports."""
import csv
from datetime import date, timedelta
import io
import json
import statistics
import zipfile
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from caregiver_alerts import database
from sekyo_import import SERIES, POINTS, import_export, load_history, num

router = APIRouter(prefix='/history', tags=['History and reports'])
METRICS = sorted(set(SERIES) | set(POINTS) | {
    'steps', 'exercise_steps', 'sleep_deep_reported', 'sleep_light_reported',
    'sleep_rem_reported', 'sleep_awake_reported',
})
MAX_UPLOAD = 20_000_000


def vendor_samples(history):
    source = history['source']

    def record(series, metric, day, identity, value):
        if value is not None:
            return source, series, metric, day, json.dumps(identity), value

    for metric, rows in history['daily_series'].items():
        for row in rows:
            for index, value in enumerate(row['slot_values']):
                sample = record('vendor_slots', metric, row['record_date'],
                                [row['vendor_date'], index, value], value)
                if sample:
                    yield sample
    for metric, rows in history['separate_readings'].items():
        for row in rows:
            stamp = row['vendor_date']
            yield record('vendor_readings', metric, stamp[:10],
                         [stamp, row['vendor_raw_time'], row['value']], row['value'])
    for row in history['daily_activity']:
        sample = record('vendor_activity', 'steps', row['record_date'], row['vendor_date'], row['steps'])
        if sample:
            yield sample
    for row in history['sleep_records']:
        for stage, value in row['stages_reported'].items():
            sample = record('vendor_sleep', f'sleep_{stage}_reported', row['record_date'], row['vendor_date'], value)
            if sample:
                yield sample
    for row in history['exercise_records']:
        stamp = row['vendor_start']
        sample = record('vendor_exercise', 'exercise_steps', stamp[:10],
                        [stamp, row['vendor_end']], row['steps'])
        if sample:
            yield sample


def csv_samples(content):
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')), strict=True)
    fields = {'record_date', 'metric', 'value', 'source'}
    if reader.fieldnames is None or not fields.issubset(reader.fieldnames):
        raise ValueError('CSV requires record_date, metric, value and source columns')
    for index, row in enumerate(reader, 2):
        try:
            day = date.fromisoformat(row['record_date']).isoformat()
            metric = row['metric']
            value = num(row['value'])
            source = row['source'].strip()
            if metric not in METRICS or value is None or not source or len(source) > 100:
                raise ValueError()
            if metric == 'heart_rate_bpm' and value <= 0 or metric == 'spo2_percent' and not 0 < value <= 100:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError(f'Invalid measurement on CSV line {index}') from None
        identity = json.dumps([day, value])
        yield 'CSV / ' + source, 'csv_readings', metric, day, identity, value


def import_samples(content, kind):
    samples = list(vendor_samples(import_export(io.BytesIO(content))) if kind == 'zip' else csv_samples(content))
    if not samples:
        raise ValueError('The upload contains no usable historical measurements')
    with database() as con:
        con.executemany('INSERT OR IGNORE INTO history_samples VALUES (?,?,?,?,?,?)', samples)
        inserted = con.total_changes
    return {'inserted': inserted, 'duplicates': len(samples) - inserted}


@router.post('/import/{kind}')
async def upload(kind: Literal['csv', 'zip'], request: Request):
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > MAX_UPLOAD:
            raise HTTPException(413, 'Upload limit is 20 MB')
        buffer.extend(chunk)
    try:
        return await run_in_threadpool(import_samples, bytes(buffer), kind)
    except (ValueError, csv.Error, zipfile.BadZipFile, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def filtered_rows(metric, start, end):
    if metric not in METRICS:
        raise HTTPException(422, 'Unsupported metric')
    if start and end and start > end:
        raise HTTPException(422, 'Start date must be on or before end date')
    low, high = start.isoformat() if start else '0001-01-01', end.isoformat() if end else '9999-12-31'
    with database() as con:
        rows = con.execute(
            'SELECT * FROM history_samples WHERE metric=? AND day BETWEEN ? AND ? ORDER BY day',
            (metric, low, high),
        ).fetchall()
    samples = {tuple(row)[:5]: row['value'] for row in rows}
    try:
        original = load_history()
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        for source, series, name, day, identity, value in vendor_samples(original):
            if name == metric and low <= day <= high:
                samples.setdefault((source, series, name, day, identity), value)
    return samples


def bucket(day, grouping):
    if grouping == 'month':
        return day[:7]
    if grouping == 'week':
        value = date.fromisoformat(day)
        return (value - timedelta(days=value.weekday())).isoformat()
    return day


@router.get('/metrics')
def metrics():
    return METRICS


@router.get('/analytics')
def analytics(metric: str = 'heart_rate_bpm', start: date | None = None, end: date | None = None,
              grouping: Literal['day', 'week', 'month'] = 'day'):
    groups = {}
    for (source, series, _, day, _), value in filtered_rows(metric, start, end).items():
        groups.setdefault((source, series, bucket(day, grouping)), []).append(value)
    return {'mode': 'historical_import', 'metric': metric, 'groups': [
        dict(source=source, series=series, period=period, count=len(values), minimum=min(values),
             maximum=max(values), median=statistics.median(values), average=statistics.fmean(values))
        for (source, series, period), values in sorted(groups.items())
    ]}


def csv_cell(value):
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + value
    return value


@router.get('/report/{kind}')
def report(kind: Literal['csv', 'pdf'], metric: str = 'heart_rate_bpm',
           start: date | None = None, end: date | None = None,
           grouping: Literal['day', 'week', 'month'] = 'day'):
    data = analytics(metric, start, end, grouping)
    columns = ('source', 'series', 'period', 'count', 'minimum', 'maximum', 'median', 'average')
    if kind == 'csv':
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(('mode', 'metric', *columns))
        writer.writerows(('historical_import', metric, *(csv_cell(row[key]) for key in columns))
                         for row in data['groups'])
        content = stream.getvalue()
        media = 'text/csv'
    else:
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
        from xml.sax.saxutils import escape

        stream = io.BytesIO()
        style = getSampleStyleSheet()
        rows = [columns]
        for row in data['groups']:
            rows.append([Paragraph(escape(str(row[key])), style['Normal']) if key in ('source', 'series')
                         else f"{row[key]:.2f}" if isinstance(row[key], float) else str(row[key])
                         for key in columns])
        table = LongTable(rows, colWidths=[85, 83, 63, 40, 51, 51, 48, 51], repeatRows=1)
        table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                                   ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        SimpleDocTemplate(stream, title='SENTRY AI Historical Health Report').build([
            Paragraph('SENTRY AI Historical Health Report', style['Title']),
            Paragraph(f'{metric} | {start or "Earliest"} to {end or "Latest"} | By {grouping}', style['Normal']),
            Paragraph('Device-reported historical data. See import notes for vendor timestamp and unit limitations.',
                      style['Normal']),
            Spacer(1, 12),
            table if data['groups'] else Paragraph('No readings match this filter.', style['Normal']),
        ])
        content = stream.getvalue()
        media = 'application/pdf'
    return Response(content, media_type=media, headers={
        'Content-Disposition': f'attachment; filename="sentry-history.{kind}"', 'Cache-Control': 'no-store'
    })
