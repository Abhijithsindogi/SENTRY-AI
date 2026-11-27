"""SENTRY AI standard BLE Heart Rate Service bridge (experimental, localhost only).
Run as a script to receive BLE notifications; import router into engine.py.
Does not replace ESP32 telemetry or imported historical records.
"""
import argparse
import os
import asyncio
from datetime import datetime, timezone

HRS = '0000180d-0000-1000-8000-00805f9b34fb'
HRM = '00002a37-0000-1000-8000-00805f9b34fb'


def decode_hr(data):
    """Decode Bluetooth SIG Heart Rate Measurement flags and optional RR intervals."""
    if len(data) < 2:
        raise ValueError('Short heart-rate packet')
    flags = data[0]
    n = 2 if flags & 1 else 1
    offset = 1+n
    if len(data) < offset:
        raise ValueError('Truncated BPM')
    bpm = int.from_bytes(data[1:offset], 'little')
    contact = bool(flags & 2) if flags & 4 else None
    energy = None
    if flags & 8:
        if len(data) < offset+2:
            raise ValueError('Truncated energy field')
        energy = int.from_bytes(data[offset:offset+2], 'little')
        offset += 2
    rr = []
    if flags & 16:
        if len(data) < offset+2 or (len(data)-offset)%2:
            raise ValueError('Invalid RR field')
        rr = [round(int.from_bytes(data[i:i+2], 'little')*1000/1024, 3)
              for i in range(offset, len(data), 2)]
    return {'hr_bpm': bpm if bpm > 0 and contact is not False else None,
            'sensor_contact': contact, 'rr_intervals_ms': rr,
            'energy_expended_kj': energy}


async def stream_once(args, status=None, deliver=None):
    from bleak import BleakScanner, BleakClient
    from bleak.exc import BleakError
    import requests
    def matches(device, adv):
        if args.address:
            return device.address.casefold() == args.address.casefold()
        return args.name.casefold() in (adv.local_name or device.name or '').casefold()
    if status:
        status('scanning')
    print(f'Scanning for {args.address or args.name} (20 seconds)...', flush=True)
    dev = await BleakScanner.find_device_by_filter(matches, timeout=20)
    if dev is None:
        if status:
            status('device_not_found')
        print('Device not found. Check PC Bluetooth and close other apps connected to the band.', flush=True)
        return
    queue = asyncio.Queue(maxsize=30)
    def notify(_, data):
        try:
            packet = decode_hr(bytes(data))
        except ValueError as exc:
            print('Invalid packet:', exc, flush=True)
            return
        packet.update(patient_id=args.patient, source=args.source, mode='live',
                      timestamp=datetime.now(timezone.utc).isoformat(), quality_ok=False)
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(packet)
    def send(packet):
        token = os.getenv('SENTRY_BRIDGE_TOKEN')
        headers = {'Authorization': f'Bearer {token}'} if token else None
        response = requests.post(args.url.rstrip('/')+'/wearables/ble/live', json=packet, headers=headers, timeout=3)
        response.raise_for_status()
    async with BleakClient(dev, timeout=20) as client:
        if status:
            from wearable_capabilities import discover_gatt
            status('discovering_services', gatt_features=discover_gatt(client.services))
        characteristic = client.services.get_characteristic(HRM)
        if characteristic is None or 'notify' not in characteristic.properties:
            if status:
                status('capabilities_only', name=dev.name, address=dev.address,
                       detail='Connected. No supported standard heart-rate notification stream was found.')
                while client.is_connected:
                    await asyncio.sleep(1)
            else:
                print('This device has no supported heart-rate notification stream.', flush=True)
            return
        await client.start_notify(characteristic, notify)
        if status:
            status('connected', name=dev.name, address=dev.address, heart_rate_notifications=True,
                   battery_percent=None)
            battery = client.services.get_characteristic('00002a19-0000-1000-8000-00805f9b34fb')
            if battery and 'read' in battery.properties:
                try:
                    data = await asyncio.wait_for(client.read_gatt_char(battery), timeout=5)
                    if len(data) == 1 and data[0] <= 100:
                        status('connected', battery_percent=data[0])
                except (OSError, TimeoutError, BleakError):
                    pass
        print('Connected; subscribed to 2A37. Waiting for actual BPM notifications...', flush=True)
        while client.is_connected:
            try:
                packet = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                print('No notification in 15s; keep band worn and wait. No data fabricated.', flush=True)
                continue
            print(f"BLE BPM: {packet['hr_bpm']} | contact: {packet['sensor_contact']} | RR count: {len(packet['rr_intervals_ms'])}", flush=True)
            try:
                await asyncio.to_thread(deliver or send, packet)
                print('Backend OK', flush=True)
            except requests.RequestException as exc:
                print('Backend error:', exc, flush=True)
            else:
                if status:
                    status('connected', last_measurement=packet['timestamp'])
        if status:
            status('disconnected')
        print('Band disconnected.', flush=True)


async def stream(args):
    from bleak import BleakScanner
    from bleak.exc import BleakError

    if args.scan:
        devices = await BleakScanner.discover(timeout=8, return_adv=True)
        for device, advertisement in devices.values():
            label = advertisement.local_name or device.name or 'Unnamed device'
            heart_rate = ' (advertises Heart Rate Service)' if HRS in advertisement.service_uuids else ''
            print(f'{device.address}  {label}{heart_rate}', flush=True)
        return

    while True:
        try:
            await stream_once(args)
        except (BleakError, OSError, TimeoutError) as exc:
            print(f'Bluetooth error: {exc}', flush=True)
        print('Retrying Bluetooth discovery in 5 seconds.', flush=True)
        await asyncio.sleep(5)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scan', action='store_true', help='List discovered devices and exit')
    p.add_argument('--name', default='SEKYO FLEX')
    p.add_argument('--address', help='Optional BLE address instead of name')
    p.add_argument('--source', default='sekyo_flex_ble')
    p.add_argument('--patient', default='patient_001')
    p.add_argument('--url', default='http://127.0.0.1:8000')
    a = p.parse_args()
    try:
        asyncio.run(stream(a))
    except KeyboardInterrupt:
        print('Bridge stopped.')
    except Exception as exc:
        p.exit(1, f'BLE bridge stopped: {exc}\n')


if __name__ == '__main__':
    main()
else:
    from fastapi import APIRouter
    from pydantic import BaseModel, Field
    from typing import Literal, Optional

    router = APIRouter(prefix='/wearables/ble', tags=['Live BLE wearables'])
    from measurement_store import latest_sample, save_live

    class BLEReading(BaseModel):
        patient_id: str = Field(min_length=1, max_length=100)
        source: str = Field(min_length=1, max_length=100)
        mode: Literal['live']
        timestamp: datetime
        hr_bpm: Optional[int] = Field(default=None, gt=0, le=65535)
        sensor_contact: Optional[bool] = None
        rr_intervals_ms: list[float] = Field(default_factory=list, max_length=128)
        energy_expended_kj: Optional[int] = None
        quality_ok: Literal[False] = False

    @router.post('/live')
    def receive_ble(reading: BLEReading):
        if reading.sensor_contact is False:
            reading = reading.model_copy(update={'hr_bpm': None})
        save_live('ble', reading, max_age=30)
        return {'status': 'received', 'source': reading.source}

    @router.get('/live')
    def get_ble(patient_id: str = 'patient_001', source: str = 'sekyo_flex_ble'):
        record = latest_sample('ble', patient_id, source)
        if record is None:
            return {'status': 'waiting_for_data', 'patient_id': patient_id, 'source': source,
                    'hr_bpm': None, 'quality_ok': False}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record['timestamp'])).total_seconds()
        status = 'stale' if not 0 <= age <= 30 else 'receiving' if record['hr_bpm'] is not None else 'unavailable'
        return {**record, 'status': status, 'age_seconds': round(age, 1),
                'hr_bpm': record['hr_bpm'] if status == 'receiving' else None,
                'notice': 'Device-reported BLE data; signal quality and clinical accuracy not validated.'}
