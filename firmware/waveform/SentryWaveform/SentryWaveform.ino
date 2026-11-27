#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include "PulseIntervals.h"
#include <math.h>

#define MPU_ADDR 0x68
MAX30105 sensor;
PulseIntervals intervals;
float acc = 0;
float gyro = 0;
bool moving = false;
bool motionReady = false;
bool pulseReady = false;

constexpr uint32_t CAPTURE_US = 30000000;
constexpr uint32_t PERIOD_US = 10000;
constexpr uint16_t MAX_ROWS = 3200;

enum Decision : uint8_t {
  NONE, NO_CONTACT, SETTLING, FIRST_BEAT, COLLECTING,
  ACCEPTED, EARLY, LATE, INCONSISTENT
};
const char* const DECISION_NAMES[] = {
  "none", "no_contact", "settling", "first_beat", "collecting",
  "accepted", "early", "late", "inconsistent"
};

struct Sample {
  uint32_t sequence;
  uint32_t pollUs;
  uint32_t ir;
  uint16_t interval;
  uint16_t fifoDrops;
  uint8_t decision;
  bool trigger;
  bool contact;
  int8_t motion;
};
Sample rows[MAX_ROWS];

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


Decision classifyTrigger() {
  const char* state = intervals.state;
  if (strcmp(state, "FIRST_BEAT") == 0) return FIRST_BEAT;
  if (strcmp(state, "COLLECTING") == 0) return COLLECTING;
  if (strcmp(state, "CONSISTENT") == 0) return ACCEPTED;
  if (strcmp(state, "EARLY_TRIGGER") == 0) return EARLY;
  if (strcmp(state, "LATE_INTERVAL") == 0) return LATE;
  return INCONSISTENT;
}

void capture() {
  if (!pulseReady) {
    Serial.println("ERROR MAX30102 unavailable");
    return;
  }
  Serial.println("BEGIN SENTRY_WAVEFORM_V1,100,30");
  Serial.flush();
  sensor.clearFIFO();
  intervals.reset();
  uint32_t sequence = 0;
  uint16_t size = 0;
  uint16_t fifoDrops = 0;
  uint16_t contactSamples = 0;
  uint32_t lastTriggerUs = 0;
  uint32_t lastMotionUs = 0;
  int8_t motion = -1;
  const uint32_t start = micros();

  // Serial and OLED remain idle through the acquisition loop.
  while (micros() - start < CAPTURE_US && size < MAX_ROWS) {
    const uint16_t received = sensor.check();
    const uint32_t pollUs = micros() - start;
    if (received >= 4 || sensor.available() != received) {
      while (sensor.available()) sensor.nextSample();
      sequence += received;
      ++fifoDrops;
      intervals.reset();
      contactSamples = 0;
    }
    while (sensor.available() && size < MAX_ROWS) {
      sensor.nextSample();
      const uint32_t ir = sensor.getFIFOIR();
      ++sequence;
      const bool trigger = checkForBeat(ir);
      const bool contact = ir >= 50000 && ir <= 250000;
      Decision decision = NONE;
      uint16_t interval = 0;
      if (!contact) {
        intervals.reset();
        contactSamples = 0;
        if (trigger) decision = NO_CONTACT;
      } else {
        if (contactSamples < 100) ++contactSamples;
        if (trigger && contactSamples < 100) decision = SETTLING;
        if (trigger && contactSamples >= 100) {
          intervals.observe(sequence);
          lastTriggerUs = pollUs;
          decision = classifyTrigger();
          if (decision != FIRST_BEAT) interval = intervals.lastInterval;
        }
      }
      if (pollUs - lastTriggerUs > 3000000) intervals.reset();
      rows[size++] = {sequence, pollUs, ir, interval, fifoDrops, decision, trigger, contact, motion};
    }

    if (motionReady && pollUs - lastMotionUs >= 100000) {
      motion = readMotion() ? (moving ? 1 : 0) : -1;
      lastMotionUs = pollUs;
    }
    delay(1);
  }

  Serial.println("sample,nominal_us,poll_us,ir,trigger,decision,interval_samples,motion,contact,fifo_drop_events");
  for (uint16_t i = 0; i < size; ++i) {
    const Sample& row = rows[i];
    Serial.printf("%lu,%lu,%lu,%lu,%u,%s,%u,%d,%u,%u\n",
                  (unsigned long)row.sequence, (unsigned long)((row.sequence - 1) * PERIOD_US),
                  (unsigned long)row.pollUs, (unsigned long)row.ir, unsigned(row.trigger),
                  DECISION_NAMES[row.decision], unsigned(row.interval), int(row.motion),
                  unsigned(row.contact), unsigned(row.fifoDrops));
  }
  Serial.printf("END %u,%u\n", unsigned(size), unsigned(fifoDrops));
}

void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22, 400000);
  pinMode(25, INPUT);
  pulseReady = sensor.begin(Wire, I2C_SPEED_FAST);
  if (pulseReady) {
    sensor.setup(30, 1, 2, 100, 411, 4096);
    sensor.setPulseAmplitudeRed(0x0A);
    sensor.setPulseAmplitudeIR(0x1F);
  }
  motionReady = writeReg(0x6B, 0x00);
  delay(100);
  motionReady = writeReg(0x6C, 0x00) && motionReady;
  motionReady = writeReg(0x1C, 0x00) && motionReady;
  motionReady = writeReg(0x1B, 0x00) && motionReady;
  Serial.println("READY SENTRY_WAVEFORM_V1");
}

void loop() {
  if (Serial.available() && Serial.read() == 'C') capture();
  delay(1);
}
