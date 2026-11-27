"""Single server-managed standard BLE heart-rate connection."""
import argparse
import asyncio
import os
import sqlite3
from contextlib import suppress

from bleak import BleakScanner
from bleak.exc import BleakError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from universal_ble_bridge import BLEReading, HRS, get_ble, receive_ble, stream_once

router = APIRouter(prefix='/devices', tags=['Wearable connections'])
state = {'status': 'idle'}
discovered = {}
task = None
stopping = False
scan_lock = asyncio.Lock()


def update(status, **fields):
    if status == 'scanning':
        for name in ('battery_percent', 'heart_rate_notifications', 'detail', 'gatt_features'):
            state.pop(name, None)
    state.update(status=status, **fields)


def deliver(packet):
    try:
        receive_ble(BLEReading.model_validate(packet))
    except HTTPException as exc:
        if exc.status_code not in {400, 409}:
            raise


class DeviceChoice(BaseModel):
    address: str = Field(min_length=1, max_length=100)


@router.get('/status')
async def connection_status():
    snapshot = dict(state)
    if snapshot.get('source'):
        snapshot['measurement'] = await asyncio.to_thread(get_ble, 'patient_001', snapshot['source'])
    return snapshot


@router.post('/scan')
async def scan():
    if stopping or (task and not task.done()) or scan_lock.locked():
        raise HTTPException(409, 'Disconnect the managed wearable or wait for the scan to finish')
    async with scan_lock:
        try:
            results = await BleakScanner.discover(timeout=6, return_adv=True)
        except (BleakError, OSError, TimeoutError) as exc:
            raise HTTPException(503, str(exc)) from exc
        discovered.clear()
        for device, advertisement in results.values():
            discovered[device.address] = {
                'address': device.address,
                'name': advertisement.local_name or device.name or 'Unnamed device',
                'advertises_heart_rate': HRS in advertisement.service_uuids,
            }
        return list(discovered.values())


async def monitor(args):
    from caregiver_alerts import record_device_disconnect

    linked = False

    def report(status, **fields):
        nonlocal linked
        if status in {'connected', 'capabilities_only'}:
            linked = True
        update(status, **fields)

    try:
        while True:
            linked = False
            try:
                await stream_once(args, status=report, deliver=deliver)
            except (BleakError, OSError, TimeoutError) as exc:
                update('connection_error', detail=str(exc))
            except sqlite3.Error:
                update('connection_error', detail='Local measurement storage is temporarily unavailable.')
            if linked and state['status'] in {'disconnected', 'connection_error', 'capabilities_only'}:
                try:
                    await asyncio.to_thread(record_device_disconnect)
                except sqlite3.Error:
                    update('connection_error', detail='Connection notice could not be stored.')
            await asyncio.sleep(5)
    finally:
        update('idle')


@router.post('/connect', status_code=202)
async def connect(choice: DeviceChoice):
    global task
    if stopping or scan_lock.locked() or (task and not task.done()):
        raise HTTPException(409, 'Stop the current connection or wait for the scan to finish')
    device = discovered.get(choice.address)
    if not device:
        raise HTTPException(404, 'Scan and select a discovered device first')
    reference_address = os.getenv('SENTRY_SEKYO_BLE_ADDRESS', '')
    source = ('sekyo_flex_ble' if reference_address.casefold() == choice.address.casefold()
              else f'ble:{choice.address}')
    state.clear()
    update('queued', **device, source=source)
    args = argparse.Namespace(address=choice.address, name=device['name'], patient='patient_001',
                              source=source)
    task = asyncio.create_task(monitor(args))
    return dict(state)


@router.post('/disconnect')
async def disconnect():
    global task, stopping
    if stopping:
        return dict(state)
    stopping = True
    try:
        active, task = task, None
        if active:
            active.cancel()
            with suppress(asyncio.CancelledError):
                await active
        state.clear()
        update('idle')
        return dict(state)
    finally:
        stopping = False



@router.get('/capabilities')
def capabilities():
    from wearable_capabilities import (
        ble_inventory, ble_strategy, historical_inventory, packet_cadence, serial_inventory,
    )
    from universal_ai_engine import reference

    snapshot = dict(state)
    source = snapshot.get('source') or 'sekyo_flex_ble'
    sample = get_ble('patient_001', source)
    if snapshot.get('source') and snapshot.get('status') != 'connected':
        sample = {**sample, 'hr_bpm': None, 'status': 'disconnected'}
    cadence, rr_observed = packet_cadence('ble', source)
    fit = reference() if source == 'sekyo_flex_ble' else None
    strategy = ble_strategy(source, sample, cadence, bool(fit and fit[0]['patient_id'] == 'patient_001'))
    return {'ble': ble_inventory(snapshot, sample, cadence, rr_observed), 'serial': serial_inventory(),
            'historical': historical_inventory(), 'strategy': strategy}
