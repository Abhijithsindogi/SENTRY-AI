"""Offline PPG experiment. Never writes to application telemetry or calibration."""
import argparse
import csv
import json
from collections import Counter, deque
from pathlib import Path

import numpy as np
from reportlab.pdfgen import canvas
from scipy.ndimage import median_filter
from scipy.signal import butter, find_peaks, sosfiltfilt

REJECTED = {'early', 'late', 'inconsistent', 'no_contact'}


def runs(mask):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False]))
    return zip(edges[::2], edges[1::2])


def propose(signal, eligible, fs):
    filtered = np.full(len(signal), np.nan)
    decisions = {}
    cutoff = int(round(fs * .5))
    lowpass = butter(2, .35, fs=fs, output='sos')
    bandpass = butter(2, [.5, 5], btype='bandpass', fs=fs, output='sos')
    for start, end in runs(eligible):
        if end - start < 3 * fs:
            continue
        segment = signal[start:end]
        detrended = segment - sosfiltfilt(lowpass, segment)
        band = sosfiltfilt(bandpass, detrended)
        filtered[start:end] = band
        envelope = median_filter(abs(band), size=int(3 * fs) | 1, mode='nearest')
        residual = median_filter(abs(detrended - band), size=int(fs) | 1, mode='nearest')
        floor = np.maximum.reduce((.9 * envelope, 4 * residual, np.full(len(band), 1e-9)))
        peaks, _ = find_peaks(band, prominence=floor, distance=int(np.ceil(.30 * fs)))
        intervals = deque(maxlen=3)
        anchor = None
        for index in peaks:
            if index < cutoff or index >= len(band) - cutoff:
                continue
            position = start + index
            interval = 0 if anchor is None else position - anchor
            if anchor is None:
                status = 'first_beat'
            elif interval < .28 * fs:
                status = 'early'
            elif interval > 2 * fs:
                intervals.clear()
                status = 'late'
            else:
                intervals.append(interval)
                status = 'collecting'
                if len(intervals) == 3:
                    status = 'accepted' if 5 * (max(intervals) - min(intervals)) <= np.median(intervals) else 'inconsistent'
            if status != 'early':
                anchor = position
            decisions[position] = (status, interval)
    return filtered, decisions


def metrics(decisions, eligible, fs):
    counts = Counter(status for index, (status, _) in decisions.items() if eligible[index])
    intervals = [span / fs for index, (status, span) in decisions.items()
                 if eligible[index] and status == 'accepted']
    bpm = np.full(len(eligible), np.nan)
    accepted_intervals = deque(maxlen=3)
    active = np.nan
    last_trigger = -10 * fs
    for i, valid in enumerate(eligible):
        if not valid or i - last_trigger > 3 * fs:
            active = np.nan
            accepted_intervals.clear()
        if valid and i in decisions:
            status, interval = decisions[i]
            last_trigger = i
            if status in {'accepted', 'collecting'}:
                accepted_intervals.append(interval)
            elif status == 'inconsistent':
                accepted_intervals.append(interval)
            elif status != 'early':
                accepted_intervals.clear()
            if status == 'accepted' and len(accepted_intervals) == 3:
                active = 60 * fs / np.median(accepted_intervals)
            elif status != 'early':
                active = np.nan
        if valid:
            bpm[i] = active
    return {
        'eligible_seconds': np.count_nonzero(eligible) / fs,
        'coverage_percent': round(100 * np.count_nonzero(np.isfinite(bpm)) / max(1, np.count_nonzero(eligible)), 2),
        'accepted': counts['accepted'],
        'rejected': sum(counts[key] for key in REJECTED),
        'decisions': dict(counts),
        'accepted_interval_median_ms': round(float(np.median(intervals)) * 1000, 2) if intervals else None,
        'accepted_interval_iqr_ms': round(float(np.subtract(*np.percentile(intervals, [75, 25]))) * 1000, 2)
        if intervals else None,
        'median_available_bpm': round(float(np.nanmedian(bpm)), 2) if np.isfinite(bpm).any() else None,
    }, bpm


def load_local(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    sequence = np.array([int(row['sample']) for row in rows])
    if len(sequence) < 300 or sequence[0] != 1 or np.any(np.diff(sequence) <= 0):
        raise ValueError('Expected at least 300 ordered samples starting at sequence 1')
    signal = np.full(sequence[-1], np.nan)
    signal[sequence - 1] = [int(row['ir']) for row in rows]
    eligible = np.zeros(len(signal), dtype=bool)
    eligible[sequence - 1] = [row['motion'] == '0' and row['contact'] == '1' for row in rows]
    decisions = {int(row['sample']) - 1: (row['decision'], int(row['interval_samples']))
                 for row in rows if row['trigger'] == '1'}
    return signal, eligible, decisions, int(rows[-1]['fifo_drop_events'])


def plot_pdf(path, raw, band, original, proposed, eligible, fs):
    with path.open('xb') as stream:
        pdf = canvas.Canvas(stream, pagesize=(1000, 600))
        pdf.setTitle('SENTRY offline PPG detector comparison')
        pdf.setFont('Helvetica-Bold', 18)
        pdf.drawString(70, 566, 'Offline PPG detector comparison')
        pdf.setFont('Helvetica', 11)
        pdf.drawString(70, 545, 'Green: gate-accepted   Red: rejected   Amber: ungraded   Gray: excluded by motion/contact/gap')
        left, width, height = 72, 866, 164
        for array, decisions, bottom, label in (
            (raw, original, 326, 'Raw IR counts / recorded SparkFun triggers'),
            (band, proposed, 98, 'Filtered IR counts / proposed triggers'),
        ):
            valid = np.isfinite(array)
            if not valid.any():
                continue
            lo, hi = np.min(array[valid]), np.max(array[valid])
            scale = max(float(hi - lo), 1e-12)
            def x(i):
                return left + width * i / (len(array) - 1)
            def y(i):
                return bottom + height * (array[i] - lo) / scale
            pdf.setFillColorRGB(.91, .92, .93)
            for start, end in runs(~eligible):
                pdf.rect(x(start), bottom, x(end)-x(start), height, fill=1, stroke=0)
            pdf.setFillColorRGB(.1, .2, .25)
            pdf.drawString(left, bottom + height + 14, label)
            pdf.drawRightString(left - 4, bottom, f'{lo:.0f}')
            pdf.drawRightString(left - 4, bottom + height, f'{hi:.0f}')
            pdf.setStrokeColorRGB(.23, .35, .46)
            pdf.setLineWidth(.65)
            for start, end in runs(valid):
                trace = pdf.beginPath()
                trace.moveTo(x(start), y(start))
                for i in range(start + 1, end):
                    trace.lineTo(x(i), y(i))
                pdf.drawPath(trace)
            for i, (status, _) in decisions.items():
                if valid[i]:
                    color = (.02, .46, .3) if status == 'accepted' else (.68, .15, .25) if status in REJECTED else (.56, .39, .02)
                    pdf.setFillColorRGB(*color)
                    pdf.circle(x(i), y(i), 3, fill=1, stroke=0)
            pdf.setFillColorRGB(.1, .2, .25)
            for seconds in range(0, int(len(array) / fs) + 1, 5):
                pdf.drawCentredString(x(seconds * fs), bottom - 18, str(seconds))
        pdf.drawCentredString(500, 54, 'Nominal time (seconds). Markers are detector outputs, not annotated heartbeats.')
        pdf.save()


def external(directory):
    results = {}
    for file in sorted(directory.glob('*_Signals.csv')):
        data = np.genfromtxt(file, delimiter=',', names=True, encoding='utf-8')
        numeric = np.genfromtxt(file.with_name(file.name.replace('_Signals', '_Numerics')),
                                delimiter=',', names=True, encoding='utf-8')
        signal = data['PLETH']
        # External recordings do not have the ESP32 contact/motion channels.
        _, markers = propose(signal, np.isfinite(signal), 125)
        summary, bpm = metrics(markers, np.isfinite(signal), 125)
        reference = numeric['HR']
        indexes = np.rint(numeric['Time_s'] * 125).astype(int)
        inside = (indexes >= 0) & (indexes < len(bpm)) & np.isfinite(reference) & (reference > 0)
        aligned = bpm[indexes[inside]]
        observed = np.isfinite(aligned)
        summary['reference_pairs'] = int(np.count_nonzero(observed))
        summary['median_absolute_difference_bpm'] = round(float(np.median(abs(aligned[observed] - reference[inside][observed]))), 2) if observed.any() else None
        results[file.stem] = summary
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('waveform', type=Path)
    parser.add_argument('--output', required=True, type=Path, help='New report directory')
    parser.add_argument('--bidmc-dir', type=Path)
    options = parser.parse_args()
    raw, eligible, original, drops = load_local(options.waveform)
    band, proposed = propose(raw, eligible, 100)
    report = {
        'capture': options.waveform.name,
        'recorded_samples': int(np.isfinite(raw).sum()),
        'fifo_drop_events': drops,
        'excluded_samples': int((~eligible).sum()),
        'raw_ir_span_counts': float(np.ptp(raw[np.isfinite(raw)])),
        'filtered_ir_rms_counts': round(float(np.sqrt(np.nanmean(band ** 2))), 2) if np.isfinite(band).any() else None,
        'recorded_sparkfun': metrics(original, eligible, 100)[0],
        'proposed': metrics(proposed, eligible, 100)[0],
        'bidmc': external(options.bidmc_dir) if options.bidmc_dir else {},
    }
    options.output.mkdir(parents=True, exist_ok=False)
    plot_pdf(options.output / 'comparison.pdf', raw, band, original, proposed, eligible, 100)
    (options.output / 'metrics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    with (options.output / 'proposed-triggers.csv').open('x', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(['sample', 'nominal_seconds', 'decision', 'interval_samples'])
        writer.writerows((i + 1, i / 100, status, interval) for i, (status, interval) in proposed.items())
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
