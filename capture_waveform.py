"""Capture a 30-second ESP32 IR waveform and generate a standalone SVG plot."""
import argparse
import csv
from pathlib import Path
import time
from xml.sax.saxutils import escape

import serial

FIELDS = (
    'sample', 'nominal_us', 'poll_us', 'ir', 'trigger', 'decision',
    'interval_samples', 'motion', 'contact', 'fifo_drop_events',
)
REJECTED = {'early', 'late', 'inconsistent', 'no_contact'}
OTHER = {'first_beat', 'collecting', 'settling'}
COLORS = {'Accepted': '#007a61', 'Rejected': '#b5273f', 'Ungraded': '#835800'}


def parse_frame(lines):
    iterator = iter(lines)
    for line in iterator:
        if line.startswith('ERROR '):
            raise ValueError(line)
        if line == 'BEGIN SENTRY_WAVEFORM_V1,100,30':
            break
    else:
        raise ValueError('Waveform header missing. Verify the installed firmware and COM port.')
    if next(iterator, '') != ','.join(FIELDS):
        raise ValueError('Unexpected waveform CSV header')
    rows = []
    prior = None
    for line in iterator:
        if line.startswith('END '):
            count, drops = map(int, line[4:].split(','))
            if not rows or count != len(rows) or drops < rows[-1]['fifo_drop_events']:
                raise ValueError('Incomplete or invalid capture')
            return rows, drops
        values = line.split(',')
        if len(values) != len(FIELDS):
            raise ValueError('Malformed waveform row')
        row = {key: value if key == 'decision' else int(value)
               for key, value in zip(FIELDS, values)}
        decision = row['decision']
        if (not 1 <= row['sample'] <= 3200
                or row['nominal_us'] != (row['sample'] - 1) * 10000
                or not 0 <= row['poll_us'] <= 31_000_000
                or not 0 <= row['ir'] <= 262143
                or row['contact'] not in (0, 1) or row['trigger'] not in (0, 1)
                or row['motion'] not in (-1, 0, 1) or row['interval_samples'] < 0
                or row['fifo_drop_events'] < 0
                or decision not in {'accepted', 'none'} | REJECTED | OTHER
                or bool(row['trigger']) != (decision != 'none')):
            raise ValueError('Invalid waveform sample values')
        if prior and (row['sample'] <= prior['sample'] or row['poll_us'] < prior['poll_us']
                      or row['fifo_drop_events'] < prior['fifo_drop_events']):
            raise ValueError('Waveform samples arrived out of order')
        rows.append(row)
        prior = row
    raise ValueError('Waveform footer missing. No output files were written.')


def serial_lines(device, seconds=55):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        data = device.readline()
        if data:
            yield data.decode('ascii', errors='replace').strip()


def waveform_svg(rows, drops):
    low = min(row['ir'] for row in rows)
    high = max(row['ir'] for row in rows)
    span = max(high - low, 1)
    max_us = max(30_000_000, rows[-1]['nominal_us'])

    def point(row):
        return 83 + 1050 * row['nominal_us'] / max_us, 424 - 330 * (row['ir'] - low) / span

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 510" role="img" aria-labelledby="title">',
        '<title id="title">MAX30102 raw IR waveform and beat-detector markers</title>',
        '<rect width="1200" height="510" fill="white"/>',
        '<g font-family="system-ui" fill="#172832">',
        '<text x="83" y="30" font-size="22">MAX30102 raw IR waveform</text>',
        f'<text x="83" y="54" font-size="13">{len(rows)} samples · 100 Hz nominal · {drops} FIFO drop events</text>',
        '<text x="605" y="491" text-anchor="middle" font-size="14">Nominal sample time (s)</text>',
    ]
    for step in range(7):
        x = 83 + 1050 * step / 6
        parts.append(f'<path d="M{x:.1f} 92V424" stroke="#d8e1e4"/>')
        parts.append(f'<text x="{x:.1f}" y="446" text-anchor="middle">{max_us * step / 6e6:g}</text>')
    for step in range(5):
        y = 424 - 330 * step / 4
        parts.append(f'<path d="M83 {y:.1f}H1133" stroke="#d8e1e4"/>')
        parts.append(f'<text x="75" y="{y + 4:.1f}" text-anchor="end">{low + span * step / 4:.0f}</text>')

    commands = []
    previous = None
    for row in rows:
        x, y = point(row)
        command = 'L' if previous is not None and row['sample'] == previous + 1 else 'M'
        commands.append(f'{command}{x:.2f},{y:.2f}')
        previous = row['sample']
    parts.append(f'<path d="{" ".join(commands)}" stroke="#375f77" fill="none" stroke-width="1.1"/>')

    for row in rows:
        if not row['trigger']:
            continue
        x, y = point(row)
        decision = row['decision']
        group = 'Accepted' if decision == 'accepted' else 'Rejected' if decision in REJECTED else 'Ungraded'
        tooltip = escape(f"{row['nominal_us']/1e6:.2f} s | {decision} | interval {row['interval_samples']} samples")
        if group == 'Rejected':
            shape = f'<path d="M{x-4:.2f},{y-4:.2f}l8,8m0,-8l-8,8" stroke="{COLORS[group]}" stroke-width="2"/>'
        else:
            shape = f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{COLORS[group]}"/>'
        parts.append(f'<g><title>{tooltip}</title>{shape}</g>')
    for offset, (group, color) in enumerate(COLORS.items()):
        x = 725 + offset * 133
        parts.append(f'<text x="{x}" y="54" fill="{color}" font-size="14">{group}</text>')
    return '\n'.join(parts + ['</g></svg>'])


def save_outputs(rows, drops, output):
    graph = output.with_suffix('.svg')
    if output.exists() or graph.exists():
        raise FileExistsError('Choose a new output name. The CSV and SVG are kept as a pair.')
    svg = waveform_svg(rows, drops)
    with output.open('x', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with graph.open('x', encoding='utf-8') as file:
        file.write(svg)
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM5')
    parser.add_argument('--output', type=Path, default=Path('sentry-waveform.csv'))
    args = parser.parse_args()
    if args.output.suffix.lower() != '.csv':
        parser.error('--output must end in .csv')
    try:
        if args.output.exists() or args.output.with_suffix('.svg').exists():
            raise FileExistsError('Choose a new output name.')
        with serial.Serial(args.port, 115200, timeout=0.5, write_timeout=2) as device:
            time.sleep(2)
            device.reset_input_buffer()
            input('Rest a finger steadily on the MAX30102, then press Enter to start the 30-second capture. ')
            device.write(b'C')
            print('Capturing and transferring. Keep your finger in place until the capture completes.', flush=True)
            rows, drops = parse_frame(serial_lines(device))
        graph = save_outputs(rows, drops, args.output)
    except (OSError, ValueError, EOFError) as exc:
        parser.exit(1, f'Capture failed: {exc}\n')
    print(f'Saved {len(rows)} samples, {drops} FIFO drop events.')
    print(f'CSV  {args.output}\nPlot {graph}')


if __name__ == '__main__':
    main()
