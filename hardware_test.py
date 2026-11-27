
import serial
import serial.tools.list_ports
import time

BAUD_RATE = 115200

ports = list(serial.tools.list_ports.comports())

if not ports:
    print("No COM ports found. Connect your ESP32.")
    exit()

print("Available COM ports:")

for i, port in enumerate(ports):
    print(f"{i}: {port.device} - {port.description}")

if len(ports) == 1:
    PORT = ports[0].device
else:
    choice = int(input("Select port index: "))
    PORT = ports[choice].device

try:
    with serial.Serial(PORT, BAUD_RATE, timeout=2) as esp32:
        time.sleep(2)

        print(f"ESP32 connected successfully on {PORT}!")
        print("Receiving live sensor data...")
        print("-" * 50)

        while True:
            data = esp32.readline().decode(
                "utf-8", errors="ignore"
            ).strip()

            if data:
                print(data)

except serial.SerialException as e:
    print(f"Connection error: {e}")

except KeyboardInterrupt:
    print("ESP32 disconnected.")
