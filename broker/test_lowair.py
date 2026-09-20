"""low_aircraft must fire for the notable case and stay silent for routine traffic.

A filter that suppresses everything would pass the "no flood" check and be
useless, so both directions are asserted.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector

AIRPORT = (40.5644, -76.2228)
CFG = {"adsb_alert_airport_nm": 5.0, "adsb_alert_airport_lat": AIRPORT[0],
       "adsb_alert_airport_lon": AIRPORT[1], "adsb_alert_repeat_min": 60}


def db_with(hex_, lat, lon, prior_alert_min_ago=None):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE aircraft_positions(ts TEXT, hex TEXT, lat REAL, lon REAL)")
    con.execute("CREATE TABLE alerts(id INTEGER PRIMARY KEY, ts TEXT, rule TEXT, "
                "device_key TEXT, message TEXT, notified INT DEFAULT 0)")
    now = datetime.now(timezone.utc)
    if lat is not None:
        con.execute("INSERT INTO aircraft_positions VALUES(?,?,?,?)",
                    (now.isoformat(timespec="seconds"), hex_, lat, lon))
    if prior_alert_min_ago is not None:
        t = (now - timedelta(minutes=prior_alert_min_ago)).isoformat(timespec="seconds")
        con.execute("INSERT INTO alerts(ts,rule,device_key,message) VALUES(?,?,?,?)",
                    (t, "low_aircraft", hex_, "earlier"))
    con.commit()
    return con, path


class TestLowAircraftGate(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_paths", []):
            try:
                os.unlink(p)
            except OSError:
                pass

    def check(self, lat, lon, prior=None, hex_="ABC123"):
        con, path = db_with(hex_, lat, lon, prior)
        self._paths = getattr(self, "_paths", []) + [path]
        return collector._low_alert_allowed(con, CFG, hex_)

    def test_on_approach_to_the_field_is_suppressed(self):
        """2 nm out on final -- the routine case that buried the channel."""
        ok, why = self.check(40.59, -76.21)
        self.assertFalse(ok)
        self.assertIn("nm from the field", why)

    def test_low_far_from_the_field_is_ALLOWED(self):
        """The case the rule exists for: low, and nowhere near a runway."""
        ok, why = self.check(40.90, -76.60)      # ~22 nm out
        self.assertTrue(ok, "notable low aircraft was suppressed: %s" % why)

    def test_unknown_position_is_suppressed_beside_an_airport(self):
        ok, why = self.check(None, None)
        self.assertFalse(ok)
        self.assertIn("no position", why)

    def test_repeat_within_the_window_is_suppressed(self):
        """One descent must not produce four alerts."""
        ok, why = self.check(40.90, -76.60, prior=10)
        self.assertFalse(ok)
        self.assertIn("already alerted", why)

    def test_repeat_outside_the_window_is_allowed_again(self):
        ok, why = self.check(40.90, -76.60, prior=120)
        self.assertTrue(ok, "suppressed after the repeat window: %s" % why)

    def test_airport_filter_can_be_switched_off(self):
        con, path = db_with("ZZ", None, None)
        self._paths = getattr(self, "_paths", []) + [path]
        cfg = dict(CFG, adsb_alert_airport_nm=0, adsb_alert_repeat_min=0)
        ok, _ = collector._low_alert_allowed(con, cfg, "ZZ")
        self.assertTrue(ok, "setting nm=0 must restore altitude-only behaviour")

    def test_distance_maths_is_right(self):
        d = collector._nm_between(40.6, -76.2, *AIRPORT)
        self.assertTrue(2.0 < d < 3.0, "site->airport computed as %.2f nm" % d)


if __name__ == "__main__":
    unittest.main()
