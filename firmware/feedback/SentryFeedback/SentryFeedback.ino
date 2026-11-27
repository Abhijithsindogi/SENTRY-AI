#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "MAX30105.h"
#include "heartRate.h"
#include <math.h>
#include "PulseIntervals.h"

#define MPU_ADDR 0x68
#define BIOAMP 36
#define BUZZER 25
#define RED_LED 26
#define BLUE_LED 33


Adafruit_SSD1306 display(
  128, 64, &Wire, -1, 400000, 400000
);

MAX30105 sensor;

bool oledOK = false;
bool pulseOK = false;
bool motionOK = false;
bool motionReady = false;
bool moving = false;
enum FeedbackState : uint8_t { WAITING, NO_DEVIATION, CHECK_IN };
FeedbackState feedbackState = WAITING;
unsigned long lastFeedback = 0;
constexpr unsigned long FEEDBACK_TIMEOUT = 75000;
char command[24];
uint8_t commandLength = 0;
bool commandOverflow = false;

float bpm = 0;
float acc = 0;
float gyro = 0;

long irValue = 0;
int ecg = 0;

unsigned long lastUpdate = 0;
unsigned long lastMotion = 0;
unsigned long lastECG = 0;

unsigned long sampleCount = 0;
PulseIntervals intervals;
unsigned long fifoDrops = 0;
unsigned long validContactSamples = 0;
uint8_t oledAddress = 0;
uint16_t oledOffset = 1024;
unsigned long lastBeatTime = 0;
unsigned long detectedBeats = 0;
long irMinimum = 0;
long irMaximum = 0;
bool irWindowReady = false;

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

// Only current host assessments drive the indicators in this build.
void setFeedback(FeedbackState state) {
  feedbackState = state;
  digitalWrite(RED_LED, state == CHECK_IN ? HIGH : LOW);
  digitalWrite(BLUE_LED, state == NO_DEVIATION ? HIGH : LOW);
}

void pollFeedback() {
  for (uint8_t count = 0; count < 16 && Serial.available(); ++count) {
    const char byte = Serial.read();
    if (byte == '\n') {
      command[commandLength] = '\0';
      if (!commandOverflow) {
        if (strcmp(command, "SENTRY WAIT") == 0) setFeedback(WAITING);
        if (strcmp(command, "SENTRY OK") == 0) {
          setFeedback(NO_DEVIATION);
          lastFeedback = millis();
        }
        if (strcmp(command, "SENTRY REVIEW") == 0) {
          setFeedback(CHECK_IN);
          lastFeedback = millis();
        }
      }
      commandLength = 0;
      commandOverflow = false;
    } else if (commandLength < sizeof(command) - 1) {
      command[commandLength++] = byte;
    } else {
      commandOverflow = true;
    }
  }
  if (feedbackState != WAITING && millis() - lastFeedback > FEEDBACK_TIMEOUT)
    setFeedback(WAITING);
}

void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22, 400000);

  pinMode(RED_LED, OUTPUT);
  pinMode(BLUE_LED, OUTPUT);

  pinMode(BUZZER, INPUT);

  pinMode(BIOAMP, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(BIOAMP, ADC_11db);

  setFeedback(WAITING);

  // Initialize OLED
  if (deviceFound(0x3C)) {
    oledAddress = 0x3C;
    oledOK = display.begin(SSD1306_SWITCHCAPVCC, oledAddress, true, false);
  }
  else if (deviceFound(0x3D)) {
    oledAddress = 0x3D;
    oledOK = display.begin(SSD1306_SWITCHCAPVCC, oledAddress, true, false);
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

  Serial.println("SENTRY AI");
  Serial.println("----------------------");

  Serial.print("OLED: ");
  Serial.println(oledOK ? "OK" : "FAILED");

  Serial.print("MAX30102: ");
  Serial.println(pulseOK ? "OK" : "FAILED");

  Serial.print("Motion Sensor: ");
  Serial.println(motionOK ? "OK" : "FAILED");

  Serial.println("BioAmp ADC: Ready");

  Serial.println("Buzzer: Disabled");

  if (oledOK) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);

    display.println("SENTRY AI");
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

  if (pulseOK)
    sensor.clearFIFO();

}

// Set an explicit page/column range for each chunk. Each write returns
// control to the optical polling loop before the next display chunk.
void sendOledChunk() {
  if (!oledOK || oledOffset >= 1024) return;
  const uint8_t page = oledOffset / 128;
  const uint8_t column = oledOffset % 128;
  Wire.beginTransmission(oledAddress);
  Wire.write(0x00);
  const uint8_t address[] = {0x21, column, uint8_t(column + 15), 0x22, page, page};
  Wire.write(address, sizeof(address));
  if (Wire.endTransmission() != 0) {
    oledOffset = 1024;
    return;
  }
  Wire.beginTransmission(oledAddress);
  Wire.write(0x40);
  Wire.write(display.getBuffer() + oledOffset, 16);
  if (Wire.endTransmission() == 0) oledOffset += 16;
  else oledOffset = 1024;
}

void invalidatePulse() {
  bpm = 0;
  validContactSamples = 0;
  intervals.reset();
}

void pollPulse() {
  if (!pulseOK) return;
  const uint16_t received = sensor.check();
  if (received >= 4) {
    while (sensor.available()) sensor.nextSample();
    ++fifoDrops;
    invalidatePulse();
    intervals.state = "FIFO_LOSS";
    return;
  }
  while (sensor.available()) {
    sensor.nextSample();
    irValue = sensor.getFIFOIR();
    ++sampleCount;
    if (!irWindowReady) {
      irMinimum = irMaximum = irValue;
      irWindowReady = true;
    }
    if (irValue < irMinimum) irMinimum = irValue;
    if (irValue > irMaximum) irMaximum = irValue;
    const bool beat = checkForBeat(irValue);
    if (irValue < 50000 || irValue > 250000) {
      invalidatePulse();
      intervals.state = "CONTACT_RANGE";
      continue;
    }
    if (validContactSamples < 100) ++validContactSamples;
    if (beat && validContactSamples >= 100) {
      ++detectedBeats;
      bpm = intervals.observe(sampleCount);
      lastBeatTime = millis();
    }
  }
  if (validContactSamples >= 100 && millis() - lastBeatTime > 3000) {
    intervals.state = "DETECTOR_TIMEOUT";
    bpm = 0;
    intervals.reset();
  }
}

void loop() {
  pollPulse();
  sendOledChunk();

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

  pollFeedback();

  // 5. Update OLED and Serial Monitor
  if (millis() - lastUpdate >= 500) {

    lastUpdate = millis();

    if (oledOK && oledOffset >= 1024) {

      display.clearDisplay();
      display.setTextSize(1);
      display.setTextColor(SSD1306_WHITE);

      display.setCursor(0, 0);
      display.println("SENTRY AI");

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

      // Host assessment status
      display.setCursor(0, 54);
      display.println(feedbackState == CHECK_IN ? "REVIEW CHECK-IN" :
                      feedbackState == NO_DEVIATION ? "AI: NO DEVIATION" : "AI: WAITING");

      oledOffset = 0;
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
    Serial.print(feedbackState == CHECK_IN ? "ON" : "OFF");

    Serial.print(" | Blue: ");
    Serial.print(feedbackState == NO_DEVIATION ? "ON" : "OFF");

    Serial.print(" | Buzzer: ");

    Serial.print("DISABLED");
    Serial.print(" | FIFO Drops: ");
    Serial.print(fifoDrops);
    Serial.print(" | Pulse State: ");
    Serial.print(intervals.state);
    Serial.print(" | IR Span: ");
    Serial.print(irWindowReady ? irMaximum - irMinimum : 0);
    irWindowReady = false;
    Serial.print(" | Beats: ");
    Serial.print(detectedBeats);
    Serial.print(" | Interval Samples: ");
    Serial.print(intervals.lastInterval);
    Serial.print(" | Early: ");
    Serial.print(intervals.early);
    Serial.print(" | Late: ");
    Serial.print(intervals.late);
    Serial.print(" | Inconsistent: ");
    Serial.println(intervals.inconsistent);
  }
}
