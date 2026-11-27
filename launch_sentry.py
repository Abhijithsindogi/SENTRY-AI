"""Start and stop only the SENTRY processes owned by this launcher."""
import argparse
from contextlib import ExitStack
import os
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def stop(child):
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.CTRL_BREAK_EVENT)
        child.wait(timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        child.terminate()
        child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-only', '--demo', dest='backend_only', action='store_true',
                        help='Start the dashboard without opening any wearable ports')
    parser.add_argument('--port', default='COM5', help='ESP32 serial port')
    parser.add_argument('--ble-address', help='Use the CLI BLE bridge instead of browser discovery')
    parser.add_argument('--http-port', type=int, default=8000, help='Loopback HTTP port')
    args = parser.parse_args()
    if not 1024 <= args.http_port <= 65535:
        parser.error('--http-port must be between 1024 and 65535')
    url = f'http://127.0.0.1:{args.http_port}'
    if listening(args.http_port):
        print(f'Port {args.http_port} is already in use. Open the current dashboard or stop its server from its own terminal.')
        return 1
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    with ExitStack() as cleanup:
        def start(*command):
            child = subprocess.Popen([sys.executable, *command], cwd=ROOT, creationflags=flags)
            cleanup.callback(stop, child)
            return child

        server = start('-m', 'uvicorn', 'engine:app', '--host', '127.0.0.1', '--port', str(args.http_port))
        for _ in range(80):
            if server.poll() is not None:
                return server.returncode
            if listening(args.http_port):
                break
            time.sleep(0.25)
        else:
            print('The backend did not become ready.')
            return 1

        print(f'Dashboard {url}/ | Ctrl+C stops this launch.', flush=True)
        if not args.backend_only:
            from serial.tools import list_ports
            if args.port.upper() in {port.device.upper() for port in list_ports.comports()}:
                start('live_ai_bridge.py', '--port', args.port, '--url', url)
            else:
                print(f'{args.port} is unavailable. Dashboard remains available.', flush=True)
            if args.ble_address:
                start('universal_ble_bridge.py', '--address', args.ble_address, '--url', url)
        while server.poll() is None:
            time.sleep(0.5)
        return server.returncode


def interrupted(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGBREAK, interrupted)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('SENTRY launcher stopped.')
