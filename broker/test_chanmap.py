"""Tests for the channel-label to frequency map.

The negative control that matters most: `centerfreq` must NEVER be read as a
channel frequency. That substring bug has already shipped in this project once,
and here it would silently assign every channel the tuner's DC frequency --
producing a map that looks complete and is entirely wrong.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chanmap  # noqa: E402

REAL = """
# rtl_airband -- air_tower
devices:
(
  {
    type = "rtlsdr";
    index = 0;
    gain = 40;
    centerfreq = 118.8500;
    mode = "multichannel";
    sample_rate = 2.4;
    channels:
    (
    {
      freq = 118.1000;
      modulation = "am";
      label = "BOI_TWR_118";
    },    {
      freq = 119.0000;
      modulation = "am";
      label = "BOI_TWR_119";
    },    {
      freq = 119.6000;
      modulation = "am";
      label = "BIGSKY_APP";
    }
    );
  }
);
"""


class TestParse(unittest.TestCase):
    def test_pairs_freq_with_following_label(self):
        m = chanmap.parse_conf(REAL)
        self.assertEqual(m, {"BOI_TWR_118": 118.1, "BOI_TWR_119": 119.0,
                             "BIGSKY_APP": 119.6})

    def test_centerfreq_is_never_a_channel(self):
        m = chanmap.parse_conf(REAL)
        # NEGATIVE CONTROL: 118.85 is the tuner centre, not a channel. If the
        # regex lacked its line anchor, `freq` would match inside `centerfreq`
        # and this value would appear.
        self.assertNotIn(118.85, m.values())
        self.assertEqual(chanmap.centers(REAL), [118.85])

    def test_label_without_freq_is_dropped(self):
        m = chanmap.parse_conf('label = "ORPHAN";\n')
        self.assertEqual(m, {})

    def test_freq_without_label_is_dropped(self):
        m = chanmap.parse_conf("  freq = 100.0;\n")
        self.assertEqual(m, {})

    def test_second_freq_before_label_discards_the_first(self):
        # a malformed block must not smear the wrong frequency onto a label
        m = chanmap.parse_conf('  freq = 100.0;\n  freq = 200.0;\n  label = "X";\n')
        self.assertEqual(m, {"X": 200.0})

    def test_empty(self):
        self.assertEqual(chanmap.parse_conf(""), {})


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def write(self, name, text):
        with open(os.path.join(self.d, name), "w") as fh:
            fh.write(text)

    def test_merges_directory(self):
        self.write("a.conf", REAL)
        self.write("b.conf", '  freq = 462.5625;\n  label = "FRS01";\n')
        self.write("ignored.yaml", '  freq = 1.0;\n  label = "NOPE";\n')
        m = chanmap.load(self.d)
        self.assertEqual(m["FRS01"], 462.5625)
        self.assertEqual(m["BOI_TWR_118"], 118.1)
        self.assertNotIn("NOPE", m)          # only .conf files are authority

    def test_missing_dir(self):
        self.assertEqual(chanmap.load(os.path.join(self.d, "nope")), {})


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "a.conf"), "w") as fh:
            fh.write(REAL)
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE voice(id INTEGER PRIMARY KEY, ts TEXT, "
                         "channel TEXT, freq_mhz REAL)")
        self.con.executemany(
            "INSERT INTO voice(ts,channel,freq_mhz) VALUES(?,?,?)",
            [("t", "BOI_TWR_118", None), ("t", "BIGSKY_APP", None),
             ("t", "UNKNOWN_CH", None), ("t", "BOI_TWR_119", 42.0)])
        self.con.commit()

    def test_fills_known_leaves_unknown_null(self):
        n, un = chanmap.backfill(self.con, self.d)
        self.assertEqual(n, 2)
        self.assertEqual(un, ["UNKNOWN_CH"])
        rows = dict(self.con.execute(
            "SELECT channel, freq_mhz FROM voice").fetchall())
        self.assertEqual(rows["BOI_TWR_118"], 118.1)
        # NEGATIVE CONTROL: an unknown channel must stay NULL, never inherit a
        # neighbour's frequency
        self.assertIsNone(rows["UNKNOWN_CH"])
        # an already-set value is not overwritten
        self.assertEqual(rows["BOI_TWR_119"], 42.0)

    def test_idempotent(self):
        chanmap.backfill(self.con, self.d)
        n, _ = chanmap.backfill(self.con, self.d)
        self.assertEqual(n, 0)

    def test_no_conf_dir_is_a_noop(self):
        n, un = chanmap.backfill(self.con, "/nonexistent")
        self.assertEqual((n, un), (0, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
