"""Local SENTRY AI readiness check. Read-only; no fabricated data or external requests."""
from datetime import datetime, timezone
from urllib.request import urlopen
from urllib.error import HTTPError, URLError
import json

BASE = 'http://127.0.0.1:8000'
failures = 0

def report(label, state, detail=''):
    global failures
    if state == 'FAIL':
        failures += 1
    print(f'[{state:4}] {label}: {detail}')

def get(path, html=False):
    try:
        with urlopen(BASE + path, timeout=5) as r:
            body = r.read()
            if html:
                if 'text/html' not in r.headers.get('Content-Type', ''):
                    raise ValueError('Expected HTML')
                return body.decode('utf-8', errors='replace')
            return json.loads(body)
    except (HTTPError, URLError, OSError, ValueError) as e:
        report(path, 'FAIL', str(e))
        return None

def age(stamp):
    try:
        t = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if t.tzinfo is None:
            return None
        return (datetime.now(timezone.utc)-t).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return None

def main():
    print('SENTRY AI | LOCAL DEMO READINESS\n')
    for path, label in [('/', 'Main dashboard'), ('/caregiver', 'Caregiver dashboard')]:
        if get(path, html=True) is not None:
            report(label, 'PASS', 'HTML served')
    t = get('/telemetry')
    if isinstance(t, dict):
        a = age(t.get('timestamp'))
        fresh = a is not None and 0 <= a <= 10
        report('ESP32 telemetry', 'PASS' if fresh else 'WAIT', 'fresh' if fresh else 'waiting or stale; check live_ai_bridge.py')
        report('ESP32 BPM', 'PASS' if fresh and t.get('hr_bpm') is not None else 'WAIT', 'numeric reading received (not accuracy validation)' if fresh and t.get('hr_bpm') is not None else 'unavailable; check finger contact')
    b = get('/wearables/ble/live')
    if isinstance(b, dict):
        status = b.get('status', 'unknown')
        report('Sekyo Bluetooth', 'PASS' if status == 'receiving' and b.get('hr_bpm') is not None else 'WAIT', str(status) + '; intermittent notifications are possible')
    h = get('/wearables/sekyo/summary')
    if isinstance(h, dict):
        report('Sekyo historical API', 'PASS', 'summary received')
    u = get('/wearables/ai/summary')
    if isinstance(u, dict):
        report('Universal AI', 'PASS' if 'esp32' in u and 'sekyo' in u else 'FAIL', 'response received')
        for row in u.get('esp32', []):
            report('ESP32 AI assessment', 'INFO', str(row.get('status', 'unknown')))
        report('Sekyo AI assessment', 'INFO', str(u.get('sekyo', {}).get('status', 'unknown')))
    r = get('/wearables/ai/sekyo/replay')
    if isinstance(r, dict):
        report('Sekyo historical replay', 'PASS' if r.get('mode') == 'historical_replay' else 'WAIT', 'held-out replay available (not clinical accuracy)' if r.get('mode') == 'historical_replay' else str(r.get('status', 'unknown')))
    e = get('/wearables/alerts')
    if isinstance(e, dict):
        events = e.get('events', [])
        sims = [x for x in events if x.get('mode') == 'simulation']
        ack = sum(bool(x.get('acknowledged')) for x in sims)
        report('Caregiver history', 'PASS', f'{len(sims)} simulation event(s); {ack} acknowledged')
        if not ack:
            report('Caregiver demo', 'WAIT', 'Create simulated check-in and acknowledge on /caregiver')
    print(f'\nHTTP/integration failures: {failures}. WAIT means a feature needs attention, not a health result.')
    print('No data changed. Clinical accuracy and background worker operation are not established by this check.')
    return bool(failures)

if __name__ == '__main__':
    raise SystemExit(main())
