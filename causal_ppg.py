"""Causal PPG reference replay with a one-sample peak confirmation."""
import argparse
from collections import deque
import json
from pathlib import Path
from time import perf_counter_ns

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from ppg_experiment import load_local, metrics, plot_pdf, runs


def detect(raw, eligible, fs):
    filtered = np.full(len(raw), np.nan)
    markers = {}
    band = butter(2, [.5, 5], btype='bandpass', fs=fs, output='sos')
    trend = butter(2, .35, fs=fs, output='sos')
    alpha = 1 - np.exp(-1 / fs)
    delay = round(.30 * fs)
    duration = round(2 * fs)
    trend_state, band_state = sosfilt_zi(trend), sosfilt_zi(band)
    for start, end in runs(eligible):
        values = raw[start:end]
        center, _ = sosfilt(trend, values, zi=trend_state * values[0])
        centered = values - center
        output, _ = sosfilt(band, centered, zi=band_state * centered[0])
        filtered[start:end] = output
        last = None
        intervals = deque(maxlen=3)
        amplitude = noise = 0.0
        valley = 0.0
        for j in range(2, len(output)):
            previous, peak, newest = output[j - 2:j + 1]
            amplitude += alpha * (abs(peak) - amplitude)
            noise += alpha * (abs(centered[j - 1] - peak) - noise)
            valley = min(valley, previous)
            position = start + j
            if last is not None and position - last > 3 * fs:
                intervals.clear()
                last = None
            if j < fs or peak <= 0 or peak <= previous or peak < newest:
                continue
            if peak - valley < max(1.1 * amplitude, 3 * noise, 1e-9):
                continue
            if last is not None and position - last < delay:
                continue
            interval = 0 if last is None else position - last
            if last is None:
                status = 'first_beat'
            elif interval > duration:
                intervals.clear()
                status = 'late'
            else:
                intervals.append(interval)
                status = 'collecting'
                if len(intervals) == 3:
                    ordered = sorted(intervals)
                    status = 'accepted' if 5 * (ordered[-1] - ordered[0]) <= ordered[1] else 'inconsistent'
            markers[position] = status, interval
            last = position
            valley = newest
    return filtered, markers


def evaluate(raw, eligible, fs):
    start = perf_counter_ns()
    filtered, decisions = detect(raw, eligible, fs)
    elapsed = perf_counter_ns() - start
    report, bpm = metrics(decisions, eligible, fs)
    report.update(
        nominal_peak_confirmation_ms=1000 / fs,
        batch_processing_us_per_input_sample=round(elapsed / len(raw) / 1000, 3),
    )
    return filtered, decisions, report, bpm


def evaluate_benchmark(directory):
    summaries = {}
    for file in sorted(directory.glob('*_Signals.csv')):
        records = np.genfromtxt(file, delimiter=',', names=True, encoding='utf-8')
        numeric = np.genfromtxt(file.with_name(file.name.replace('_Signals', '_Numerics')),
                                delimiter=',', names=True, encoding='utf-8')
        signal = records['PLETH']
        _, _, report, bpm = evaluate(signal, np.isfinite(signal), 125)
        reference = numeric['HR']
        indexes = np.rint(numeric['Time_s'] * 125).astype(int)
        valid = (indexes >= 0) & (indexes < len(bpm)) & np.isfinite(reference) & (reference > 0)
        values = bpm[indexes[valid]]
        observed = np.isfinite(values)
        report['reference_pairs'] = int(np.count_nonzero(observed))
        report['median_absolute_difference_bpm'] = (
            round(float(np.median(np.abs(values[observed] - reference[valid][observed]))), 2)
            if observed.any() else None
        )
        summaries[file.stem] = report
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--bidmc-dir', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = {'esp32': {}, 'bidmc': {}}
    for file in args.captures:
        raw, eligible, original, _ = load_local(file)
        filtered, decisions, report, _ = evaluate(raw, eligible, 100)
        plot_pdf(args.output / (file.stem + '.pdf'), raw, filtered, original, decisions, eligible, 100)
        result['esp32'][file.name] = {'recorded': metrics(original, eligible, 100)[0], 'causal': report}
    if args.bidmc_dir:
        result['bidmc'] = evaluate_benchmark(args.bidmc_dir)
    summary = json.dumps(result, indent=2)
    (args.output / 'metrics.json').write_text(summary, encoding='utf-8')
    print(summary)


if __name__ == '__main__':
    main()
