import os
import sqlite3
import tempfile
import unittest

import spectrum

FIXTURE = os.path.join(os.path.dirname(__file__), "sample_power.csv")


def flat_noise_bins(ts, hz_low=100000000, hz_step=1000, n=100, floor=-90.0):
    """Deterministic near-flat noise: wobbles +/-0.5 dB around `floor`, never
    enough to trip any min_over_floor threshold used in these tests."""
    wobble = [0.0, 0.5, -0.5]
    return [
        {"ts": ts, "freq_hz": hz_low + i * hz_step, "db": floor + wobble[i % 3]}
        for i in range(n)
    ]


def with_carrier(bins, ts, freq_hz, db):
    return bins + [{"ts": ts, "freq_hz": freq_hz, "db": db}]


class TestParse(unittest.TestCase):
    def test_parses_sample_fixture(self):
        bins = spectrum.parse(FIXTURE)
        self.assertEqual(len(bins), 160)
        # spot check known values from the fixture generator
        self.assertEqual(bins[0]["freq_hz"], 150000000)
        self.assertEqual(bins[10]["freq_hz"], 150010000)
        self.assertEqual(bins[10]["db"], -40.0)

    def test_missing_file_returns_empty(self):
        self.assertEqual(spectrum.parse("/no/such/file.csv"), [])

    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            path = f.name
        try:
            self.assertEqual(spectrum.parse(path), [])
        finally:
            os.unlink(path)

    def test_malformed_rows_skipped_not_raised(self):
        content = (
            "2026-08-30,12:00:00,150000000,150004000,1000,20,-85.0,-84.0,-40.0,-83.0,-86.0\n"
            "bad, row, too, short\n"
            "2026-08-30,12:00:05,151000000,151002000,1000,20,notanumber,-50.0\n"
            "2026-08-30,12:00:10,not_a_number_hz,151002000,1000,20,-80.0,-81.0\n"
            "\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            bins = spectrum.parse(path)  # must not raise
        finally:
            os.unlink(path)

        # row1: 5 good bins; short row: 0; row3: 1 bin kept (bad dB skipped,
        # good one kept), 1 dropped; row4: whole row dropped (bad Hz_low)
        self.assertEqual(len(bins), 6)
        freqs = {b["freq_hz"]: b["db"] for b in bins}
        self.assertEqual(freqs[150002000], -40.0)
        self.assertEqual(freqs[151001000], -50.0)


class TestSummarize(unittest.TestCase):
    def test_median_floor_not_mean(self):
        bins = flat_noise_bins(ts=0, floor=-90.0, n=9)
        bins = with_carrier(bins, ts=0, freq_hz=999000000, db=0.0)  # one huge outlier
        s = spectrum.summarize(bins)
        # mean would be dragged way up by the 0.0 dB outlier; median should not be
        self.assertLess(s["floor_db"], -50.0)
        self.assertEqual(s["peak_db"], 0.0)
        self.assertEqual(s["n_bins"], 10)

    def test_empty(self):
        s = spectrum.summarize([])
        self.assertEqual(s["n_bins"], 0)


class TestFindCarriers(unittest.TestCase):
    def test_three_known_carriers_from_fixture(self):
        bins = spectrum.parse(FIXTURE)
        carriers = spectrum.find_carriers(bins)
        freqs = sorted(c["freq_hz"] for c in carriers)
        self.assertEqual(freqs, [150010000, 150090000, 151020000])
        # sorted strongest first (highest db)
        dbs = [c["db"] for c in carriers]
        self.assertEqual(dbs, sorted(dbs, reverse=True))

    def test_flat_noise_negative_control(self):
        bins = flat_noise_bins(ts=0, n=200)
        self.assertEqual(spectrum.find_carriers(bins), [])

    def test_close_peaks_deduplicated_to_one(self):
        bins = flat_noise_bins(ts=0, hz_low=200000000, n=50)
        bins = with_carrier(bins, ts=0, freq_hz=200025000, db=-50.0)
        bins = with_carrier(bins, ts=0, freq_hz=200045000, db=-40.0)  # 20kHz away, stronger
        carriers = spectrum.find_carriers(bins, min_over_floor=10.0, min_sep_hz=50000)
        self.assertEqual(len(carriers), 1)
        self.assertEqual(carriers[0]["freq_hz"], 200045000)
        self.assertEqual(carriers[0]["db"], -40.0)

    def test_far_peaks_not_deduplicated(self):
        bins = flat_noise_bins(ts=0, hz_low=200000000, n=50)
        bins = with_carrier(bins, ts=0, freq_hz=200000000, db=-50.0)
        bins = with_carrier(bins, ts=0, freq_hz=200200000, db=-40.0)  # 200kHz away
        carriers = spectrum.find_carriers(bins, min_over_floor=10.0, min_sep_hz=50000)
        self.assertEqual(len(carriers), 2)


class TestStore(unittest.TestCase):
    def test_creates_table_and_stores_carriers_only(self):
        conn = sqlite3.connect(":memory:")
        bins = spectrum.parse(FIXTURE)
        n = spectrum.store(conn, "milair", bins)
        self.assertEqual(n, 3)  # only the 3 carriers, not all 160 bins
        rows = conn.execute("SELECT band, freq_hz, db FROM spectrum ORDER BY freq_hz").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], "milair")

    def test_store_no_carriers_inserts_nothing(self):
        conn = sqlite3.connect(":memory:")
        bins = flat_noise_bins(ts=0, n=50)
        n = spectrum.store(conn, "milair", bins)
        self.assertEqual(n, 0)
        count = conn.execute("SELECT COUNT(*) FROM spectrum").fetchone()[0]
        self.assertEqual(count, 0)


class TestNewCarriers(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.day = 86400

    def test_flags_only_the_genuinely_new_carrier(self):
        t0 = 10_000_000  # arbitrary epoch, 2 days before "now"
        hist_bins = flat_noise_bins(ts=t0, hz_low=300000000, n=200)
        hist_bins = with_carrier(hist_bins, t0, 300010000, -40.0)  # A
        hist_bins = with_carrier(hist_bins, t0, 300500000, -45.0)  # B
        spectrum.store(self.conn, "milair", hist_bins)

        now = t0 + 2 * self.day
        cur_bins = flat_noise_bins(ts=now, hz_low=300000000, n=200)
        cur_bins = with_carrier(cur_bins, now, 300010000, -41.0)   # A again
        cur_bins = with_carrier(cur_bins, now, 300500000, -44.0)   # B again
        cur_bins = with_carrier(cur_bins, now, 301000000, -30.0)   # C: new

        result = spectrum.new_carriers(self.conn, "milair", cur_bins, history_days=3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["freq_hz"], 301000000)

    def test_no_history_means_everything_is_new(self):
        now = 20_000_000
        bins = flat_noise_bins(ts=now, hz_low=400000000, n=50)
        bins = with_carrier(bins, now, 400010000, -30.0)
        result = spectrum.new_carriers(self.conn, "milair", bins, history_days=3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["freq_hz"], 400010000)

    def test_history_outside_window_does_not_count_as_seen(self):
        t_old = 0  # far in the past
        hist_bins = flat_noise_bins(ts=t_old, hz_low=500000000, n=50)
        hist_bins = with_carrier(hist_bins, t_old, 500010000, -40.0)  # D, old
        spectrum.store(self.conn, "milair", hist_bins)

        now = t_old + 10 * self.day  # D is 10 days old, outside a 3-day window
        cur_bins = flat_noise_bins(ts=now, hz_low=500000000, n=50)
        cur_bins = with_carrier(cur_bins, now, 500010000, -40.0)  # D reappears

        result = spectrum.new_carriers(self.conn, "milair", cur_bins, history_days=3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["freq_hz"], 500010000)

    def test_band_isolation(self):
        t0 = 0
        hist_bins = flat_noise_bins(ts=t0, hz_low=600000000, n=50)
        hist_bins = with_carrier(hist_bins, t0, 600010000, -40.0)
        spectrum.store(self.conn, "band_a", hist_bins)

        now = t0 + 1000
        cur_bins = flat_noise_bins(ts=now, hz_low=600000000, n=50)
        cur_bins = with_carrier(cur_bins, now, 600010000, -40.0)

        # same freq, different band -> history in band_a doesn't suppress band_b
        result = spectrum.new_carriers(self.conn, "band_b", cur_bins, history_days=3)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
