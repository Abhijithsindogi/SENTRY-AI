#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "MAX30105.h"
#include "heartRate.h"
#include <math.h>

#define MPU_ADDR 0x68
#define BIOAMP 36
#define BUZZER 25
#define RED_LED 26
#define BLUE_LED 33

// Enable ONLY after confirming safe buzzer wiring
const bool ENABLE_BUZZER = false;

Adafruit_SSD1306 display(
  128, 64, &Wire, -1, 400000, 400000
);

MAX30105 sensor;

bool oledOK = false;
bool pulseOK = false;
bool motionOK = false;
bool motionReady = false;
bool moving = false;
bool testAlert = false;

float bpm = 0;
float acc = 0;
float gyro = 0;

long irValue = 0;
int ecg = 0;

unsigned long lastUpdate = 0;
unsigned long lastMotion = 0;
unsigned long lastECG = 0;
unsigned long lastTest = 0;
unsigned long alertStart = 0;

unsigned long sampleCount = 0;
unsigned long lastBeatSample = 0;
unsigned long lastBeatTime = 0;

// I2C device detection
bool deviceFound(byte address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

// Write motion sensor register
bool writeReg(byte reg, byte value) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

// Read signed 16-bit value
int16_t read16() {
  uint8_t h = Wire.read();
  uint8_t l = Wire.read();
  return (int16_t)((h << 8) | l);
}

// Read motion sensor
bool readMotion() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);

  if (Wire.endTransmission(false) != 0)
    return false;

  if (Wire.requestFrom((uint8_t)MPU_ADDR,
                       (uint8_t)14) != 14)
    return false;

  float x = read16() / 16384.0 * 9.81;
  float y = read16() / 16384.0 * 9.81;
  float z = read16() / 16384.0 * 9.81;

  read16(); // Skip temperature

  float gx = read16() / 131.0;
  float gy = read16() / 131.0;
  float gz = read16() / 131.0;

  acc = sqrt(x*x + y*y + z*z);
  gyro = sqrt(gx*gx + gy*gy + gz*gz);

  moving = fabs(acc - 9.81) > 1.5 ||
           gyro > 20.0;

  return true;
}

// LED and buzzer control
void setAlert(bool state) {
  testAlert = state;

  digitalWrite(RED_LED, state ? HIGH : LOW);
  digitalWrite(BLUE_LED, state ? LOW : HIGH);

  if (ENABLE_BUZZER) {
    digitalWrite(BUZZER, state ? HIGH : LOW);
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22);

  pinMode(RED_LED, OUTPUT);
  pinMode(BLUE_LED, OUTPUT);

  if (ENABLE_BUZZER) {
    pinMode(BUZZER, OUTPUT);
    digitalWrite(BUZZER, LOW);
  } else {
    pinMode(BUZZER, INPUT);
  }

  pinMode(BIOAMP, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(BIOAMP, ADC_11db);

  setAlert(false);

  // Initialize OLED
  if (deviceFound(0x3C)) {
    oledOK = display.begin(
      SSD1306_SWITCHCAPVCC, 0x3C, true, false);
  }
  else if (deviceFound(0x3D)) {
    oledOK = display.begin(
      SSD1306_SWITCHCAPVCC, 0x3D, true, false);
  }

  // Initialize MAX30102
  pulseOK = sensor.begin(Wire, I2C_SPEED_FAST);

  if (pulseOK) {
    sensor.setup(30, 1, 2, 100, 411, 4096);
    sensor.setPulseAmplitudeRed(0x0A);
    sensor.setPulseAmplitudeIR(0x1F);
  }

  // Initialize MPU6500-compatible sensor
  motionReady = writeReg(0x6B, 0x00);
  delay(100);

  motionReady = writeReg(0x6C, 0x00) && motionReady;
  motionReady = writeReg(0x1C, 0x00) && motionReady;
  motionReady = writeReg(0x1B, 0x00) && motionReady;

  if (motionReady)
    motionOK = readMotion();

  Serial.println("VITALSYNC AI");
  Serial.println("----------------------");

  Serial.print("OLED: ");
  Serial.println(oledOK ? "OK" : "FAILED");

  Serial.print("MAX30102: ");
  Serial.println(pulseOK ? "OK" : "FAILED");

  Serial.print("Motion Sensor: ");
  Serial.println(motionOK ? "OK" : "FAILED");

  Serial.println("BioAmp ADC: Ready");

  Serial.print("Buzzer: ");
  Serial.println(ENABLE_BUZZER ?
                 "Enabled" : "Disabled");

  if (oledOK) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);

    display.println("VITALSYNC AI");
    display.println("---------------------");

    display.print("OLED: ");
    display.println("OK");

    display.print("MAX30102: ");
    display.println(pulseOK ? "OK" : "FAIL");

    display.print("Motion: ");
    display.println(motionOK ? "OK" : "FAIL");

    display.println("BioAmp ADC Ready");
    display.display();
  }

  // Startup indicator test
  setAlert(true);
  delay(200);
  setAlert(false);

  if (pulseOK)
    sensor.clearFIFO();

  lastTest = millis();
}

void loop() {

  // 1. Read optical heart rate sensor
  if (pulseOK) {
    uint16_t received = sensor.check();

    // Discard backlog if sample timing is unreliable
    if (received >= 4) {
      while (sensor.available())
        sensor.nextSample();

      bpm = 0;
      lastBeatSample = 0;
    }

    while (sensor.available()) {
      irValue = sensor.getFIFOIR();
      sensor.nextSample();
      sampleCount++;

      bool beat = checkForBeat(irValue);

      if (irValue < 50000 || irValue > 250000) {
        bpm = 0;
        lastBeatSample = 0;
      }
      else if (beat) {

        if (lastBeatSample != 0) {
          unsigned long interval =
            sampleCount - lastBeatSample;

          if (interval > 0) {
            float rate = 6000.0 / interval;

            if (rate > 30 && rate < 220) {
              bpm = bpm == 0 ? rate :
                    0.7 * bpm + 0.3 * rate;
            }
          }
        }

        lastBeatSample = sampleCount;
        lastBeatTime = millis();
      }
    }

    if (millis() - lastBeatTime > 3000) {
      bpm = 0;
      lastBeatSample = 0;
    }
  }

  // 2. Read BioAmp raw test signal
  if (millis() - lastECG >= 4) {
    lastECG = millis();
    ecg = analogRead(BIOAMP);
  }

  // 3. Read motion sensor
  if (motionReady &&
      millis() - lastMotion >= 100) {

    lastMotion = millis();
    motionOK = readMotion();
  }

  // 4. Test red LED and buzzer every 5 sec
  if (millis() - lastTest >= 5000) {
    lastTest = millis();
    alertStart = millis();
    setAlert(true);
  }

  // Stop test alert after 500 ms
  if (testAlert &&
      millis() - alertStart >= 500) {
    setAlert(false);
  }

  // 5. Update OLED and Serial Monitor
  if (millis() - lastUpdate >= 500) {

    lastUpdate = millis();

    if (oledOK) {

      display.clearDisplay();
      display.setTextSize(1);
      display.setTextColor(SSD1306_WHITE);

      display.setCursor(0, 0);
      display.println("VITALSYNC AI");

      display.drawLine(0, 10, 127, 10,
                       SSD1306_WHITE);

      // Heart rate
      display.setCursor(0, 14);
      display.print("HR: ");

      if (!pulseOK)
        display.println("ERROR");
      else if (irValue < 50000)
        display.println("Place Finger");
      else if (irValue > 250000)
        display.println("Adjust Finger");
      else if (bpm == 0)
        display.println("Measuring...");
      else {
        display.print(bpm, 0);
        display.println(" BPM");
      }

      // Motion
      display.setCursor(0, 24);
      display.print("Motion: ");

      if (!motionOK)
        display.println("ERROR");
      else
        display.println(moving ? "MOVING" : "STILL");

      // Acceleration
      display.setCursor(0, 34);
      display.print("ACC: ");

      if (motionOK) {
        display.print(acc, 2);
        display.println(" m/s2");
      } else {
        display.println("N/A");
      }

      // BioAmp raw reading
      display.setCursor(0, 44);
      display.print("ECG ADC: ");
      display.println(ecg);

      // LED and buzzer test status
      display.setCursor(0, 54);
      display.println(testAlert ?
                      "TEST ALERT" : "TEST RUNNING");

      display.display();
    }

    // Serial Monitor output
    Serial.print("BPM: ");

    if (pulseOK && bpm > 0)
      Serial.print(bpm);
    else
      Serial.print("N/A");

    Serial.print(" | IR: ");
    Serial.print(irValue);

    Serial.print(" | ACC: ");

    if (motionOK)
      Serial.print(acc);
    else
      Serial.print("N/A");

    Serial.print(" | Motion: ");

    if (!motionOK)
      Serial.print("ERROR");
    else
      Serial.print(moving ? "MOVING" : "STILL");

    Serial.print(" | ECG ADC: ");
    Serial.print(ecg);

    Serial.print(" | Red: ");
    Serial.print(testAlert ? "ON" : "OFF");

    Serial.print(" | Blue: ");
    Serial.print(testAlert ? "OFF" : "ON");

    Serial.print(" | Buzzer: ");

    if (!ENABLE_BUZZER)
      Serial.println("DISABLED");
    else
      Serial.println(testAlert ? "ON" : "OFF");
  }
}
