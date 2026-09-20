"""Tests for LoRa-width detection at 915 MHz.

The discriminator is bandwidth, so the controls that matter are the signals
that are NOT LoRa: the narrowband ISM devices this band is full of. This
receiver already decodes SCMplus gas meters here at 23-32 dB SNR, and a
detector that called those LoRa would be worse than no detector at all.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lora  # noqa: E402

STEP = 12_500          # matches the survey lane's bin size
BASE = 902_000_000


def sweep(floor_db=-30.0, signals=(), span_hz=26_000_000, step=STEP):
    """Build a synthetic power sweep. signals = [(centre_hz, width_hz, db)]."""
    pts = []
    f = BASE
    while f < BASE + span_hz:
        d = floor_db
        for c, w, s in signals:
            if abs(f - c) <= w / 2.0:
                d = max(d, s)
        pts.append((f, d))
        f += step
    return pts


class TestPlateaus(unittest.TestCase):
    def test_finds_a_250k_plateau(self):
        pts = sweep(signals=[(906_875_000, 250_000, -10.0)])
        ps = lora.find_plateaus(pts)
        self.assertEqual(len(ps), 1)
        self.assertAlmostEqual(ps[0]["centre_hz"], 906_875_000, delta=STEP)
        self.assertAlmostEqual(ps[0]["width_hz"], 250_000, delta=2 * STEP)

    def test_two_separate_signals_do_not_merge(self):
        # NEGATIVE CONTROL: a gap between them must end the run, or two narrow
        # devices would be reported as one implausibly wide plateau
        pts = sweep(signals=[(910_000_000, 25_000, -10.0),
                             (911_000_000, 25_000, -10.0)])
        ps = lora.find_plateaus(pts)
        self.assertEqual(len(ps), 2)

    def test_flat_noise_yields_nothing(self):
        self.assertEqual(lora.find_plateaus(sweep()), [])

    def test_empty_and_tiny_input(self):
        self.assertEqual(lora.find_plateaus([]), [])
        self.assertEqual(lora.find_plateaus([(1, 2)]), [])


class TestClassify(unittest.TestCase):
    def test_accepts_each_lora_bandwidth(self):
        for bw in (125_000, 250_000, 500_000):
            got, why = lora.classify_plateau({"width_hz": bw})
            self.assertEqual(got, bw, why)

    def test_rejects_a_narrowband_meter(self):
        # NEGATIVE CONTROL: SCMplus gas meters are decoded on this very band.
        # A 25 kHz signal is never LoRa.
        got, why = lora.classify_plateau({"width_hz": 25_000})
        self.assertIsNone(got)
        self.assertIn("narrowband", why)

    def test_rejects_something_far_too_wide(self):
        # NEGATIVE CONTROL: front-end overload smears across the sweep
        got, why = lora.classify_plateau({"width_hz": 3_000_000})
        self.assertIsNone(got)
        self.assertIn("too wide", why)

    def test_rejects_a_width_between_lora_channels(self):
        # NEGATIVE CONTROL: 180 kHz is wide, but is not a LoRa bandwidth and
        # must not be rounded into the nearest one
        got, why = lora.classify_plateau({"width_hz": 180_000})
        self.assertIsNone(got)
        self.assertIn("matches no standard", why)


class TestDetect(unittest.TestCase):
    def test_meshcore_default_channel_is_named(self):
        pts = sweep(signals=[(906_875_000, 250_000, -12.0)])
        r = lora.detect(pts)
        self.assertEqual(len(r["candidates"]), 1)
        self.assertTrue(r["candidates"][0]["meshcore_default_channel"])
        self.assertIn("DETECTION only", r["note"])

    def test_a_lora_elsewhere_in_the_band_is_not_called_meshcore(self):
        # NEGATIVE CONTROL: matching the width does not mean matching the network
        pts = sweep(signals=[(920_000_000, 250_000, -12.0)])
        r = lora.detect(pts)
        self.assertEqual(len(r["candidates"]), 1)
        self.assertFalse(r["candidates"][0]["meshcore_default_channel"])

    def test_meters_are_rejected_not_reported(self):
        pts = sweep(signals=[(914_656_000, 25_000, -5.0),
                             (914_847_000, 25_000, -2.0)])
        r = lora.detect(pts)
        self.assertEqual(r["candidates"], [])
        self.assertEqual(len(r["rejected"]), 2)
        self.assertIn("no LoRa-width activity", lora.summarise(r))

    def test_mixed_scene_separates_them(self):
        pts = sweep(signals=[(914_656_000, 25_000, -5.0),
                             (906_875_000, 250_000, -12.0)])
        r = lora.detect(pts)
        self.assertEqual(len(r["candidates"]), 1)
        self.assertEqual(len(r["rejected"]), 1)
        self.assertIn("MeshCore default channel", lora.summarise(r))

    def test_weak_signal_below_snr_is_not_detected(self):
        # NEGATIVE CONTROL: 2 dB above the floor is not a transmission
        pts = sweep(floor_db=-30.0, signals=[(906_875_000, 250_000, -28.0)])
        self.assertEqual(lora.detect(pts)["candidates"], [])

    def test_empty_sweep_summarises_without_crashing(self):
        r = lora.detect([])
        self.assertEqual(r["candidates"], [])
        self.assertIn("no LoRa-width activity", lora.summarise(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
