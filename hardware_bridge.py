
import serial
import serial.tools.list_ports
import requests
import time
from datetime import datetime

BAUD_RATE = 115200
API_URL = "http://127.0.0.1:8000/telemetry"

ports = list(serial.tools.list_ports.comports())

if not ports:
    print("No ESP32 detected!")
    exit()

PORT = ports[0].device


def parse_data(line):
    try:
        fields = {}

        for item in line.split("|"):
            if ":" in item:
                key, value = item.split(":", 1)
                fields[key.strip()] = value.strip()

        bpm = fields.get("BPM", "N/A")

        return {
            "patient_id": "patient_001",
            "source": "sentry_wearable",
            "mode": "live",
            "timestamp": datetime.now().astimezone().isoformat(),
            "hr_bpm": None if bpm == "N/A" else float(bpm),
            "ir_raw": int(fields["IR"]),
            "acceleration": float(fields["ACC"]),
            "motion": fields["Motion"],
            "bioamp_adc": int(fields["ECG ADC"]),
            "emg_rms": None,
            "quality_ok": False
        }

    except (ValueError, KeyError):
        return None


try:
    with serial.Serial(PORT, BAUD_RATE, timeout=2) as esp32:

        time.sleep(2)

        print(f"ESP32 connected on {PORT}")
        print("SENTRY AI Live Bridge Started")

        last_sent = 0

        while True:

            line = esp32.readline().decode(
                "utf-8", errors="ignore"
            ).strip()

            data = parse_data(line)

            if data and time.monotonic() - last_sent >= 1:

                last_sent = time.monotonic()

                try:
                    response = requests.post(
                        API_URL,
                        json=data,
                        timeout=3
                    )

                    response.raise_for_status()

                    print(
                        f"Data sent | "
                        f"BPM: {data['hr_bpm']} | "
                        f"Motion: {data['motion']} | "
                        f"BioAmp: {data['bioamp_adc']}"
                    )

                except requests.RequestException as e:
                    print("API connection error:", e)

except serial.SerialException as e:
    print("Serial error:", e)

except KeyboardInterrupt:
    print("Hardware bridge stopped.")
