"""Tests for vehicle / tyre-pressure tracking.

The controls that matter: a parked car must never read as a broken sensor, and
a tyre must never be labelled with a position nobody established.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vehicles  # noqa: E402


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE readings(id INTEGER PRIMARY KEY, ts TEXT,"
                    " device_key TEXT, metric TEXT, value REAL, unit TEXT,"
                    " quality TEXT, source TEXT)")
        con.commit()
        con.close()
        self.now = datetime.now(timezone.utc)
        self.sensors = ["Toyota/a", "Toyota/b", "Toyota/c", "Toyota/d"]
        json.dump({
            "mine": self.sensors,
            "labels": {s: "tyre %s" % s[-1].upper() for s in self.sensors},
            "vehicles": {"my-car": {"label": "My car", "sensors": self.sensors}},
        }, open(os.path.join(self.root, "sensors.json"), "w"))

    def tearDown(self):
        os.unlink(self.db)

    def seed(self, minutes_ago, pressures, temp=22.0):
        con = sqlite3.connect(self.db)
        for s, p in zip(self.sensors, pressures):
            ts = iso(self.now - timedelta(minutes=minutes_ago))
            con.executemany(
                "INSERT INTO readings(ts,device_key,metric,value,unit,quality,"
                "source) VALUES(?,?,?,?,?,?,?)",
                [(ts, s, "pressure", p, "PSI", "measured", "t"),
                 (ts, s, "temperature", temp, "C", "measured", "t")])
        con.commit()
        con.close()


class TestPresence(Base):
    def test_recent_reading_is_here_and_active(self):
        self.seed(2, [33, 34, 35, 33])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertEqual(v["presence"], "here, active")

    def test_quiet_car_is_PARKED_not_broken(self):
        # NEGATIVE CONTROL: TPMS only transmits when moving. A car on the drive
        # is silent by design; calling that a dead sensor is a false alarm.
        self.seed(45, [33, 34, 35, 33])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertEqual(v["presence"], "parked")
        self.assertIn("quiet when stationary", v["presence_reason"])
        self.assertEqual(v["concerns"], [])

    def test_long_silence_is_away_not_a_fault(self):
        self.seed(600, [33, 34, 35, 33])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertEqual(v["presence"], "away")
        self.assertIn("h ago", v["presence_reason"])

    def test_never_seen(self):
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertEqual(v["presence"], "never seen")
        self.assertEqual(v["concerns"], [])


class TestPressure(Base):
    def test_healthy_set_raises_nothing(self):
        self.seed(5, [33.0, 34.0, 35.0, 33.5])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertEqual(v["concerns"], [])

    def test_low_tyre_is_flagged(self):
        self.seed(5, [33.0, 34.0, 26.0, 33.5])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertTrue(any("below 28" in c for c in v["concerns"]), v["concerns"])

    def test_imbalance_flagged_even_when_all_are_above_the_floor(self):
        # every tyre is legal, but a 6 PSI spread is worth a look
        self.seed(5, [30.0, 36.0, 34.0, 33.0])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertTrue(any("spread" in c for c in v["concerns"]), v["concerns"])

    def test_no_position_is_ever_invented(self):
        # NEGATIVE CONTROL: nothing observed establishes which tyre is
        # front-left. A label must not imply one.
        self.seed(5, [33, 34, 35, 33])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        for t in v["tyres"]:
            for banned in ("front", "rear", "left", "right", "fl", "fr"):
                self.assertNotIn(banned, t["label"].lower())

    def test_all_four_tyres_reported(self):
        self.seed(5, [33, 34, 35, 33])
        v = vehicles.vehicle_status(self.db, self.root)[0]
        self.assertEqual(len(v["tyres"]), 4)
        self.assertTrue(all(t["pressure"] is not None for t in v["tyres"]))


class TestHistory(Base):
    def test_history_keeps_the_newest_point(self):
        con = sqlite3.connect(self.db)
        for i in range(500):
            con.execute("INSERT INTO readings(ts,device_key,metric,value,unit,"
                        "quality,source) VALUES(?,?,?,?,?,?,?)",
                        (iso(self.now - timedelta(minutes=500 - i)),
                         "Toyota/a", "pressure", 30.0 + i * 0.01, "PSI",
                         "measured", "t"))
        con.commit()
        con.close()
        h = vehicles.pressure_history(self.db, "Toyota/a", hours=24, max_points=50)
        self.assertEqual(len(h), 50)
        self.assertAlmostEqual(h[-1][1], 30.0 + 499 * 0.01, places=4)

    def test_no_vehicles_configured(self):
        os.unlink(os.path.join(self.root, "sensors.json"))
        self.assertEqual(vehicles.vehicle_status(self.db, self.root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
