"""Loopback access, optional role accounts, and local profiles."""
import base64
import binascii
import hmac
import json
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from caregiver_alerts import database

router = APIRouter(prefix='/account', tags=['Local profiles'])
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1', 'testserver'}
DEVICE_WRITES = {'/telemetry', '/baseline', '/readings', '/wearables/ble/live'}


def matches(left, right):
    return bool(right) and hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def account_role(request):
    secrets = {role: os.getenv(f'SENTRY_{role.upper()}_PASSWORD', '') for role in ('patient', 'caregiver')}
    if not any(secrets.values()):
        return 'local'
    header = request.headers.get('authorization', '')
    if request.method == 'POST' and request.url.path in DEVICE_WRITES and header.startswith('Bearer '):
        if matches(header[7:], os.getenv('SENTRY_BRIDGE_TOKEN', '')):
            return 'bridge'
    if header.startswith('Basic '):
        try:
            user, password = base64.b64decode(header[6:], validate=True).decode('utf-8').split(':', 1)
        except (ValueError, UnicodeError, binascii.Error):
            return None
        if user in secrets and matches(password, secrets[user]):
            return user
    return None


async def access_control(request, call_next):
    if request.url.hostname not in LOCAL_HOSTS:
        return JSONResponse({'detail': 'SENTRY only accepts local host names'}, status_code=400)
    if request.client and request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
        return JSONResponse({'detail': 'SENTRY only accepts loopback connections'}, status_code=403)
    method, path = request.method, request.url.path
    if method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') in {'cross-site', 'same-site'} or (
            origin and origin != f'{request.url.scheme}://{request.url.netloc}'
        ):
            return JSONResponse({'detail': 'Cross-origin changes are disabled'}, status_code=403)
    role = account_role(request)
    if not role:
        return JSONResponse({'detail': 'Sign in with a configured local account'}, status_code=401,
                            headers={'WWW-Authenticate': 'Basic realm="SENTRY AI", charset="UTF-8"'})
    request.state.role = role
    if method not in {'GET', 'HEAD', 'OPTIONS'} and role != 'local':
        if path.startswith('/wearables/alerts/') and role != 'caregiver':
            return JSONResponse({'detail': 'Caregiver access required'}, status_code=403)
        if path in DEVICE_WRITES and role != 'bridge':
            return JSONResponse({'detail': 'Bridge token required'}, status_code=403)
        if path.startswith(('/history/import/', '/devices/')) and role != 'patient':
            return JSONResponse({'detail': 'Patient access required'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


class Profile(BaseModel):
    display_name: str = Field(default='', max_length=80)
    emergency_contact: str = Field(default='', max_length=120)
    reduced_motion: bool = False
    telegram_opt_in: bool = False


def profile_key(request, role):
    logged_in = request.state.role
    if logged_in != 'local' and role != logged_in:
        raise HTTPException(403, 'Profile belongs to a different role')
    return role


@router.get('')
def account(request: Request):
    return {'role': request.state.role, 'authentication_enabled': request.state.role != 'local'}


@router.get('/profiles/{role}')
def get_profile(request: Request, role: Literal['patient', 'caregiver']):
    key = profile_key(request, role)
    with database() as con:
        row = con.execute('SELECT payload FROM local_profiles WHERE role=?', (key,)).fetchone()
    return json.loads(row['payload']) if row else Profile().model_dump()


@router.put('/profiles/{role}')
def put_profile(request: Request, role: Literal['patient', 'caregiver'], profile: Profile):
    key = profile_key(request, role)
    with database() as con:
        con.execute('INSERT OR REPLACE INTO local_profiles VALUES (?,?)', (key, profile.model_dump_json()))
    return {'status': 'saved'}
