# ESP32 bench sketches

`original/SentryOriginal/SentryOriginal.ino` preserves the supplied sketch. `bench/SentryBench/SentryBench.ino` is an opt-in diagnostic build. Use the same board selection as the original sketch and the installed Adafruit SSD1306, Adafruit GFX, and SparkFun MAX3010x libraries.

The candidate keeps the same baud rate, reported pins, base telemetry fields, contact thresholds and sensor sampling configuration. The OLED is transferred in chunks so the pulse FIFO can be polled between writes. The BPM display waits for three consistent intervals after contact settles. FIFO losses invalidate the current estimate and increment the diagnostic counter. This gate is experimental and can hide BPM when the waveform is inconsistent.

For a USB bench test, keep all BioAmp body electrodes disconnected. Close the Python bridge and Serial Monitor before uploading. Use Serial Monitor at 115200 baud or the Python bridge after upload, one at a time. Capture a short interval without a finger, then capture 60 seconds with a steady light touch. Compare with an independent reference at rest. Inspect BPM availability, IR, FIFO drops and OLED readability. Do not opt into baseline calibration based solely on the appearance of numeric BPM. If the display or serial stream regresses, restore the archived original sketch with the same board and port settings.

The red and blue LEDs still run the original indicator test. The buzzer stays disabled. Backend feedback is intentionally outside this diagnostic build.

## Pulse dropout diagnostics

The uploaded bench build produced two reported 64.52 BPM values followed by one unavailable value, with IR near 107,000 and no FIFO drops in the supplied samples. The logs confirm that the new format ran on the board. A stable DC IR reading indicates light level, not usable pulsatile signal or BPM accuracy.

The running bench sketch can withdraw BPM after contact-range failure, FIFO interruption, a long interval, three intervals with a spread exceeding 20% of their median, or three seconds without another detector trigger. A short trigger is rejected without advancing the interval anchor, but it still refreshes the detector-trigger timer. The provided log does not contain enough data to distinguish these paths. The new `diagnostics/SentryDiagnostics/SentryDiagnostics.ino` keeps the original rate calculation and adds pulse state, per-report IR span, cumulative beat triggers and early/late/inconsistent counters. Its diagnostic output distinguishes detector timeout from interval rejection. No baseline calibration should be started until BPM and signal quality are independently verified.

## Raw waveform capture

The separate `waveform/SentryWaveform/SentryWaveform.ino` stays idle until it receives the capture command. It records 30 seconds of 100 Hz nominal, unaveraged IR samples in ESP32 RAM, then transfers the frame at 115200 baud. No OLED or serial writes take place in the sampling loop. Motion is sampled at approximately 10 Hz and stored as last known state. The buzzer pin stays in input mode. This diagnostic does not feed the live telemetry bridge or calibration.

Each row contains a sensor sequence, nominal sample time, elapsed host-controller FIFO poll time, raw IR, beat trigger, decision, interval length in samples, motion, IR contact-range flag, and cumulative FIFO drop events. The MAX30102 FIFO provides no individual hardware timestamp. Samples read in the same poll share its measured time. The nominal axis uses the configured 100 Hz timing and keeps gaps where sample counts are known to be lost. FIFO drop events count discarded batches or batch mismatches, not necessarily every individual lost sample. Motion is -1 unknown, 0 still, and 1 moving. Contact is the bench IR range check. Accepted points passed the experimental interval gate, not independent physiological validation. First-beat, settling and collecting markers remain ungraded.

Close Arduino Serial Monitor and stop any bridge using COM5. With all BioAmp body electrodes disconnected, upload the waveform sketch using the same ESP32 board and port as the working sketch. From the project root, run:

```powershell
.\.venv\Scripts\python.exe capture_waveform.py --port COM5 --output waveform-01.csv
```

Rest your finger lightly on the sensor, press Enter, and keep still until the transfer finishes. Keep an independent resting pulse reference if one is available. The script saves a CSV and an SVG next to it. Use a new filename per run. Open the SVG in a browser. Green dots mark accepted triggers, red crosses mark rejected triggers, and amber dots mark ungraded triggers. Hover to see interval details. Restore the working SentryBench or SentryDiagnostics sketch before resuming the serial telemetry bridge. Keep calibration disabled.

## Offline detector comparison

Run the experiment from the project root with a fresh output directory:

```powershell
.\.venv\Scripts\python.exe ppg_experiment.py waveform-01.csv --output reports/next-ppg-comparison
```

The detector subtracts a 0.35 Hz low-pass trend, applies a 0.5–5 Hz zero-phase bandpass, and finds positive peaks at least 300 ms apart. The prominence threshold adapts to the local bandpassed amplitude and out-of-band residual. Processing is split on gaps, missing contact, and non-still motion. The three-interval 20% consistency check, interval limits, and lack-of-trigger timeout remain in the experiment. BPM availability is reconstructed from recorded or proposed gate decisions under the same contact/motion eligibility mask, and is not a firmware uptime measurement.

The local report includes separate raw/filtered traces with markers, JSON metrics, and proposed marker CSV. The optional `--bidmc-dir` accepts matched `bidmc_NN_Signals.csv` and `bidmc_NN_Numerics.csv` files from [PhysioNet BIDMC v1.0.0](https://physionet.org/content/bidmc/1.0.0/). The independent run used records 01–03 at 125 Hz and compared available estimates against the time-aligned monitor ECG-derived HR numerics. The public files have no ESP32 motion/contact fields and were not imported into the health application. Reference numerics are not beat annotations. Dataset attribution: Pimentel et al., [Towards a Robust Estimation of Respiratory Rate from Pulse Oximeters](https://doi.org/10.1109/TBME.2016.2613124), and the PhysioNet dataset and its Open Data Commons Attribution license linked above.

Zero-phase filtering uses samples on both sides of each point. These results support a causal replay experiment after collecting independent MAX30102 captures with reference readings. They do not support uploading a firmware replacement yet.

## Causal replay and LED/OLED feedback

Run the causal experiment with one or more captures and an unused output directory:

```powershell
.\.venv\Scripts\python.exe causal_ppg.py waveform-01.csv waveform-02.csv --output reports/ppg-causal-follow-up --bidmc-dir "$env:TEMP/sentry-bidmc"
```

The causal filter and peak decision use only samples already received. Confirming a local maximum requires one new sample (10 ms at ESP32 nominal rate). That is not total event latency: causal filters may have phase delay, and the 3-interval gate needs startup history. Per-sample timing in the JSON is a desktop batch average, not on-device latency. The candidate performed worse on the first ESP32 capture. Do not flash it or enable calibration based on the three BIDMC traces.

`feedback/SentryFeedback/SentryFeedback.ino` is a separate LED/OLED-only prototype using the current bench detector. With `--feedback`, the bridge sends only the result of the current live, quality-eligible assessment. The firmware returns to waiting when the host sends a wait message or its command expires after 75 seconds. The LEDs are off in waiting, blue for an available no-deviation result and red for review requested. The OLED labels the state. The buzzer stays disabled. This build is separate from waveform acquisition. Finish independent signal verification before any live feedback trial.

For the implemented 100 Hz trend-subtraction plus bandpass transfer function, calculated group delay is approximately 216 ms at 60 BPM, 111 ms at 90 BPM, and 85 ms at 120 BPM. This is frequency-response analysis, not measured beat-to-output latency.

For the feedback sketch's physical indicator and timeout test, see the main README. It exercises the local serial protocol with the buzzer disabled and body electrodes disconnected. Complete the independent optical waveform capture before switching builds.

The feedback sketch uses SENTRY AI branding. Leave the waveform firmware installed until any outstanding measurement is complete. COM5 discovery does not establish ownership. The launcher leaves the port owner alone if the serial bridge cannot open it, and the backend remains available.
