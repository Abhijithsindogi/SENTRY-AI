import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from capture_waveform import FIELDS, parse_frame, save_outputs


FRAME = [
    'READY SENTRY_WAVEFORM_V1',
    'BEGIN SENTRY_WAVEFORM_V1,100,30',
    ','.join(FIELDS),
    '1,0,9999,105000,1,first_beat,0,0,1,0',
    '2,10000,20000,105100,1,early,1,0,1,0',
    '7,60000,70100,105500,1,accepted,65,-1,1,1',
    'END 3,1',
]


class WaveformTests(unittest.TestCase):
    def test_csv_svg_and_loss_marker(self):
        rows, drops = parse_frame(FRAME)
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / 'fixture.csv'
            graph = save_outputs(rows, drops, path)
            self.assertIn('7,60000,70100', path.read_text())
            svg = graph.read_text(encoding='utf-8')
            ElementTree.fromstring(svg)
            self.assertIn('early | interval 1', svg)
            self.assertIn('accepted | interval 65', svg)
            self.assertIn('M83.00,424.00 L', svg)
            with self.assertRaises(FileExistsError):
                save_outputs(rows, drops, path)

    def test_truncated_frame_and_invalid_sequence(self):
        with self.assertRaisesRegex(ValueError, 'footer missing'):
            parse_frame(FRAME[:-1])
        with self.assertRaisesRegex(ValueError, 'out of order'):
            parse_frame([*FRAME[:4], FRAME[3], *FRAME[5:]])
