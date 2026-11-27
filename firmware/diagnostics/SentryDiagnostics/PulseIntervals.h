#pragma once
#include <stdint.h>

// Experimental bench display gate for a 100 Hz sample stream.
class PulseIntervals {
 public:
  void reset() {
    hasBeat = false;
    count = 0;
    next = 0;
  }

  const char* state = "WAITING";
  uint32_t lastInterval = 0;
  uint32_t early = 0;
  uint32_t late = 0;
  uint32_t inconsistent = 0;

  float observe(uint32_t sample) {
    if (!hasBeat) {
      state = "FIRST_BEAT";
      hasBeat = true;
      last = sample;
      return 0;
    }
    const uint32_t interval = sample - last;
    lastInterval = interval;
    if (interval < 28) {
      ++early;
      state = "EARLY_TRIGGER";
      return current();
    }
    last = sample;
    if (interval > 200) {
      ++late;
      state = "LATE_INTERVAL";
      count = 0;
      next = 0;
      return 0;
    }
    values[next] = interval;
    next = (next + 1) % 3;
    if (count < 3) ++count;
    const float result = current();
    if (count < 3) state = "COLLECTING";
    else if (result > 0) state = "CONSISTENT";
    else {
      ++inconsistent;
      state = "INCONSISTENT";
    }
    return result;
  }

 private:
  float current() const {
    if (count < 3) return 0;
    uint32_t low = values[0], high = values[0];
    for (uint8_t i = 1; i < 3; ++i) {
      if (values[i] < low) low = values[i];
      if (values[i] > high) high = values[i];
    }
    const uint32_t median = values[0] + values[1] + values[2] - low - high;
    if ((high - low) * 5 > median) return 0;
    return 6000.0f / median;
  }

  bool hasBeat = false;
  uint8_t count = 0;
  uint8_t next = 0;
  uint32_t last = 0;
  uint32_t values[3] = {};
};
