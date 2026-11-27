from datetime import datetime, timedelta, timezone
import random
import json
import urllib.request

URL='http://127.0.0.1:8000'
rng=random.Random(42)
start=datetime.now(timezone.utc)-timedelta(minutes=40)
def row(i, hr, motion, quality=True):
    return dict(patient_id='demo-01', source='virtual-watch', mode='simulation', context='rest', timestamp=(start+timedelta(minutes=i)).isoformat(), quality_ok=quality, hr_bpm=hr, motion_rms=motion)
def post(path, data):
    req=urllib.request.Request(URL+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req) as response:
        return json.load(response)
print(post('/baseline', {'readings':[row(i, rng.gauss(72,2),max(.001,rng.gauss(.05,.01))) for i in range(30)]}))
for i,hr in enumerate([72,73,91,94,95,96],30):
    out=post('/readings',row(i,hr,.05))
    print(hr,out['status'],out['consecutive_windows'],out['reasons'])
print('Poor signal:',post('/readings',row(36,140,.05,False))['status'])
