import unittest

import numpy as np

from ppg_experiment import metrics, propose
from causal_ppg import detect


class PpgExperimentTests(unittest.TestCase):
    def test_periodic_fixture_and_excluded_gap(self):
        fs = 100
        time = np.arange(2000) / fs
        raw = 100_000 + 30 * time + 250 * np.sin(2 * np.pi * 1.4 * time)
        eligible = np.ones(len(raw), dtype=bool)
        eligible[800:1000] = False
        band, decisions = propose(raw, eligible, fs)
        scoring, bpm = metrics(decisions, eligible, fs)
        self.assertTrue(np.isnan(band[800:1000]).all())
        self.assertTrue(np.isnan(bpm[800:1000]).all())
        self.assertAlmostEqual(scoring['median_available_bpm'], 84.0, delta=1.5)
        self.assertEqual(scoring['rejected'], 0)

    def test_flat_input_does_not_produce_beats(self):
        signal = np.full(1000, 105000.0)
        eligible = np.ones(len(signal), dtype=bool)
        _, decisions = propose(signal, eligible, 100)
        self.assertEqual(decisions, {})
        self.assertEqual(metrics(decisions, eligible, 100)[0]['coverage_percent'], 0)


    def test_causal_prefix_and_quality_mask(self):
        fs = 100
        time = np.arange(2000) / fs
        signal = 100_000 + 30 * time + 250 * np.sin(2 * np.pi * 1.4 * time)
        eligible = np.ones(len(signal), dtype=bool)
        eligible[800:900] = False
        full_band, full = detect(signal, eligible, fs)
        short_band, short = detect(signal[:1401], eligible[:1401], fs)
        np.testing.assert_allclose(full_band[:1401], short_band, equal_nan=True)
        self.assertEqual(short, {index: marker for index, marker in full.items() if index < 1401})
        self.assertTrue(any(status == 'accepted' for status, _ in full.values()))
        self.assertTrue(all(eligible[index] for index in full))
