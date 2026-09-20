#!/usr/bin/env python3
"""Tests for baselines.py and aircraft_behaviour.py (T7 / T4).

Project rule: a detector nobody has watched DECLINE to fire is not a
detector. Every rule below gets a positive case AND a negative control.
All fixtures are synthetic, built in an in-memory sqlite db that mirrors the
real events/devices/aircraft_positions schema -- no external files, no
network, no hardware.

Run: python3 analysis/test_analysis.py -v
"""
import math
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from baselines import build_baseline, is_anomalous, rank_devices
from aircraft_behaviour import (
    track_for, detect_loiter, detect_orbit, detect_low, detect_rapid_descent,
    analyse, haversine_km,
)


# --- schema + fixtures -------------------------------------------------

SCHEMA = """
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  lane TEXT NOT NULL,
  kind TEXT NOT NULL,
  device_key TEXT,
  summary TEXT,
  raw TEXT,
  preserved INTEGER DEFAULT 0
);
CREATE TABLE devices (
  device_key TEXT PRIMARY KEY,
  kind TEXT,
  first_seen TEXT,
  last_seen TEXT,
  seen_count INTEGER DEFAULT 0,
  label TEXT
);
CREATE TABLE aircraft_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  hex TEXT NOT NULL,
  callsign TEXT,
  alt_ft INTEGER,
  lat REAL,
  lon REAL,
  speed_kt INTEGER,
  track REAL,
  vert_rate INTEGER,
  squawk TEXT
);
"""


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def insert_event(conn, ts, device_key, kind="ism", lane="ism433"):
    conn.execute(
        "INSERT INTO events(ts, lane, kind, device_key, summary, raw) "
        "VALUES (?, ?, ?, ?, '', '')",
        (ts.isoformat(timespec="seconds"), lane, kind, device_key),
    )


def insert_position(conn, hex_, ts, alt_ft=None, lat=None, lon=None,
                     speed_kt=None, track=None, vert_rate=None, squawk=None,
                     callsign=None):
    conn.execute(
        "INSERT INTO aircraft_positions"
        "(ts, hex, callsign, alt_ft, lat, lon, speed_kt, track, vert_rate, squawk) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts.isoformat(timespec="seconds"), hex_, callsign, alt_ft, lat, lon,
         speed_kt, track, vert_rate, squawk),
    )


def monday_at(hour=0, minute=0, weeks=0, days=0):
    """A guaranteed Monday-anchored UTC datetime, computed at runtime so the
    test never depends on a hardcoded date happening to be a Monday."""
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monday = anchor - timedelta(days=anchor.weekday())
    return monday + timedelta(weeks=weeks, days=days, hours=hour, minutes=minute)


def circle_points(n_points, radius_km, center_lat=40.7728, center_lon=-76.2783,
                   minutes_span=15.0, start=None, alt_ft=5000, vert_rate=0,
                   with_track_field=True):
    """n_points spaced evenly over exactly one full lap (theta 0..2*pi),
    over minutes_span minutes. `track` (heading) is set to theta itself in
    degrees, which is monotonic 0->360 across the lap, so the cumulative
    heading-change math in detect_orbit sums to ~360 for a full circle --
    exactly the geometry being tested, independent of real flight dynamics.
    """
    if start is None:
        start = monday_at(hour=10)
    lat_per_km = 1.0 / 111.32
    lon_per_km = 1.0 / (111.32 * math.cos(math.radians(center_lat)))
    pts = []
    for i in range(n_points):
        frac = i / (n_points - 1)
        theta = 2 * math.pi * frac
        lat = center_lat + radius_km * lat_per_km * math.sin(theta)
        lon = center_lon + radius_km * lon_per_km * math.cos(theta)
        ts = start + timedelta(minutes=minutes_span * frac)
        pts.append({
            "ts": ts.isoformat(timespec="seconds"),
            "hex": "TESTHX", "callsign": "TST123", "alt_ft": alt_ft,
            "lat": lat, "lon": lon, "speed_kt": 120,
            "track": (math.degrees(theta) % 360) if with_track_field else None,
            "vert_rate": vert_rate, "squawk": None,
        })
    return pts


def straight_points(n_points, minutes_span=20.0, start=None,
                     center_lat=40.7728, center_lon=-76.2783,
                     alt_ft=25000, vert_rate=0):
    """Constant heading, constant speed -- the flying-through negative
    control for both loiter and orbit."""
    if start is None:
        start = monday_at(hour=10)
    lon_step = 0.02  # ~1.6 km/point at this latitude; ~32 km over the track
    pts = []
    for i in range(n_points):
        ts = start + timedelta(minutes=minutes_span * i / (n_points - 1))
        pts.append({
            "ts": ts.isoformat(timespec="seconds"),
            "hex": "TESTHX", "callsign": "TST123", "alt_ft": alt_ft,
            "lat": center_lat, "lon": center_lon + lon_step * i,
            "speed_kt": 300, "track": 90.0, "vert_rate": vert_rate, "squawk": None,
        })
    return pts


# --- baselines.py --------------------------------------------------------

class TestBaselines(unittest.TestCase):

    def test_flat_histogram_nothing_anomalous(self):
        """A device seen every hour of every day of the week has no gaps --
        nothing should ever read as anomalous against it."""
        conn = make_db()
        for day in range(14):  # two full weeks: every dow, every hour, twice
            for hour in range(24):
                insert_event(conn, monday_at(hour=hour, days=day), "flat/1")
        baseline = build_baseline(conn, "flat/1")
        self.assertEqual(baseline["total"], 14 * 24)
        self.assertNotIn(0, baseline["hour_hist"])
        self.assertNotIn(0, baseline["dow_hist"])

        # An arbitrary hour/day inside the covered range -> not anomalous.
        anomalous, reason = is_anomalous(baseline, monday_at(hour=15, days=9).isoformat())
        self.assertFalse(anomalous, reason)

    def test_weekday_morning_device_flags_night_owl_sighting(self):
        """The flagship case from the brief: a device only ever seen
        08:00/08:45 on weekdays. A 3am Sunday sighting IS anomalous. Another
        ordinary 08:30 weekday sighting is NOT."""
        conn = make_db()
        for week in range(8):
            for day in range(5):  # Mon..Fri
                insert_event(conn, monday_at(hour=8, minute=0, weeks=week, days=day),
                             "commuter/1")
                insert_event(conn, monday_at(hour=8, minute=45, weeks=week, days=day),
                             "commuter/1")
        baseline = build_baseline(conn, "commuter/1")
        self.assertEqual(baseline["total"], 80)

        sunday_3am = monday_at(hour=3, weeks=4, days=6).isoformat()  # Sunday
        anomalous, reason = is_anomalous(baseline, sunday_3am)
        self.assertTrue(anomalous, reason)
        self.assertIn("never once been seen at 03:00", reason)

        normal_weekday_830 = monday_at(hour=8, minute=30, weeks=2, days=2).isoformat()  # Wed
        anomalous, reason = is_anomalous(baseline, normal_weekday_830)
        self.assertFalse(anomalous, reason)

    def test_insufficient_data_is_not_anomalous(self):
        """A device we've barely seen shouldn't have confident claims made
        about what it 'never' does -- that's just novelty alerting wearing
        a disguise."""
        conn = make_db()
        for i in range(5):
            insert_event(conn, monday_at(hour=3, days=i), "newbie/1")
        baseline = build_baseline(conn, "newbie/1")
        anomalous, reason = is_anomalous(baseline, monday_at(hour=14, days=20).isoformat())
        self.assertFalse(anomalous)
        self.assertIn("not enough history", reason)

    def test_rank_devices_puts_the_real_surprise_first(self):
        conn = make_db()
        # A well-established, evenly-active device -- boring by construction.
        for day in range(14):
            for hour in range(24):
                insert_event(conn, monday_at(hour=hour, days=day), "normal/1")

        # A commuter device whose most recent sighting breaks its own pattern.
        for week in range(8):
            for day in range(5):
                insert_event(conn, monday_at(hour=8, weeks=week, days=day), "surprise/1")
        insert_event(conn, monday_at(hour=3, weeks=9, days=0), "surprise/1")  # the outlier

        # A device we've barely met -- must not outrank a real surprise.
        for i in range(3):
            insert_event(conn, monday_at(hour=3, days=i), "brandnew/1")

        ranked = rank_devices(conn)
        by_key = {r["device_key"]: r for r in ranked}

        self.assertTrue(by_key["surprise/1"]["anomalous"])
        self.assertFalse(by_key["normal/1"]["anomalous"])
        self.assertFalse(by_key["brandnew/1"]["anomalous"])
        self.assertEqual(ranked[0]["device_key"], "surprise/1")
        self.assertEqual(by_key["surprise/1"]["score"], 1.0)
        self.assertLess(by_key["normal/1"]["score"], 1.0)
        self.assertEqual(by_key["brandnew/1"]["score"], 0.0)


# --- aircraft_behaviour.py -----------------------------------------------

class TestAircraftBehaviour(unittest.TestCase):

    # -- loiter / orbit --

    def test_straight_through_no_loiter_no_orbit(self):
        track = straight_points(21)
        self.assertFalse(detect_loiter(track))
        self.assertFalse(detect_orbit(track))

    def test_tight_circle_15min_is_loiter_and_orbit(self):
        track = circle_points(n_points=16, radius_km=1.5, minutes_span=15.0)
        self.assertTrue(detect_loiter(track))
        self.assertTrue(detect_orbit(track))

    def test_wide_orbit_is_not_loiter(self):
        """Proves the two detectors are independent, not the same check
        wearing two names: a wide circle (8 km) completes a full turn with
        small net displacement (orbit) but is well outside loiter's 3 km
        confinement radius."""
        track = circle_points(n_points=16, radius_km=8.0, minutes_span=15.0)
        self.assertTrue(detect_orbit(track))
        self.assertFalse(detect_loiter(track))

    def test_single_course_reversal_is_not_orbit(self):
        """A 180-degree reversal (go-around, procedure turn) must not read
        as an orbit -- min_turn_deg=270 sits above what one reversal produces."""
        start = monday_at(hour=10)
        track = [
            {"ts": start.isoformat(timespec="seconds"),
             "hex": "TESTHX", "lat": 40.7728, "lon": -76.2783,
             "alt_ft": 5000, "track": 90.0, "vert_rate": 0},
            {"ts": (start + timedelta(minutes=5)).isoformat(timespec="seconds"),
             "hex": "TESTHX", "lat": 40.7758, "lon": -76.2600,
             "alt_ft": 5000, "track": 90.0, "vert_rate": 0},
            {"ts": (start + timedelta(minutes=10)).isoformat(timespec="seconds"),
             "hex": "TESTHX", "lat": 40.7758, "lon": -76.2680,
             "alt_ft": 5000, "track": 270.0, "vert_rate": 0},
        ]
        self.assertFalse(detect_orbit(track))

    def test_loiter_and_orbit_handle_missing_lat_lon(self):
        """Rows with altitude but no fix (called out explicitly in the
        brief) must be skipped, not crash the detector."""
        track = circle_points(n_points=10, radius_km=1.5, minutes_span=10.0)
        # Knock out the lat/lon on every other point, as real ADS-B rows do.
        for i in range(0, len(track), 2):
            track[i]["lat"] = None
            track[i]["lon"] = None
        try:
            loiter = detect_loiter(track)
            orbit = detect_orbit(track)
        except Exception as exc:  # pragma: no cover - the whole point of the test
            self.fail("detector raised on missing lat/lon: %r" % exc)
        self.assertIsInstance(loiter, bool)
        self.assertIsInstance(orbit, bool)

    def test_empty_and_no_fix_tracks_raise_nothing(self):
        self.assertFalse(detect_loiter([]))
        self.assertFalse(detect_orbit([]))
        self.assertFalse(detect_low([]))
        self.assertFalse(detect_rapid_descent([]))

        no_fix_track = [
            {"ts": monday_at(hour=10).isoformat(timespec="seconds"), "hex": "TESTHX",
             "lat": None, "lon": None, "alt_ft": 30000, "track": None, "vert_rate": 0},
            {"ts": monday_at(hour=10, minute=5).isoformat(timespec="seconds"), "hex": "TESTHX",
             "lat": None, "lon": None, "alt_ft": 30000, "track": None, "vert_rate": 0},
        ]
        self.assertFalse(detect_loiter(no_fix_track))
        self.assertFalse(detect_orbit(no_fix_track))
        self.assertFalse(detect_low(no_fix_track))
        self.assertFalse(detect_rapid_descent(no_fix_track))

    # -- low altitude --

    def test_detect_low_true_and_false(self):
        low = [{"alt_ft": 1800}, {"alt_ft": 9000}]
        high = [{"alt_ft": 12000}, {"alt_ft": 9000}, {"alt_ft": None}]
        self.assertTrue(detect_low(low))
        self.assertFalse(detect_low(high))

    # -- rapid descent --

    def test_detect_rapid_descent_from_vert_rate(self):
        steep = [{"vert_rate": -2500, "ts": "x", "alt_ft": None},
                 {"vert_rate": -100, "ts": "y", "alt_ft": None}]
        gentle = [{"vert_rate": -800, "ts": "x", "alt_ft": None},
                  {"vert_rate": -200, "ts": "y", "alt_ft": None}]
        self.assertTrue(detect_rapid_descent(steep))
        self.assertFalse(detect_rapid_descent(gentle))

    def test_detect_rapid_descent_falls_back_to_altitude_delta(self):
        start = monday_at(hour=10)
        fast = [
            {"ts": start.isoformat(timespec="seconds"), "alt_ft": 10000, "vert_rate": None},
            {"ts": (start + timedelta(minutes=1)).isoformat(timespec="seconds"),
             "alt_ft": 7000, "vert_rate": None},  # 3000 ft/min
        ]
        slow = [
            {"ts": start.isoformat(timespec="seconds"), "alt_ft": 10000, "vert_rate": None},
            {"ts": (start + timedelta(minutes=1)).isoformat(timespec="seconds"),
             "alt_ft": 9500, "vert_rate": None},  # 500 ft/min
        ]
        self.assertTrue(detect_rapid_descent(fast))
        self.assertFalse(detect_rapid_descent(slow))

    # -- haversine sanity --

    def test_haversine_known_distance(self):
        # two points ~30 km apart: ~30 km.
        d = haversine_km(40.6150, -76.2023, 40.5407, -76.5635)
        self.assertTrue(28 <= d <= 32, d)

    # -- DB-backed track_for / analyse integration --

    def test_track_for_window_and_ordering(self):
        conn = make_db()
        base = monday_at(hour=12)
        for i in range(50):
            insert_position(conn, "ABCDEF", base + timedelta(minutes=i),
                             alt_ft=10000, lat=40.77, lon=-76.27)
        track = track_for(conn, "ABCDEF", minutes=10)
        # last row is at +49 min; a 10-minute window ending there covers
        # minutes 39..49 inclusive, i.e. 11 points.
        self.assertEqual(len(track), 11)
        self.assertEqual(track[0]["ts"], (base + timedelta(minutes=39)).isoformat(timespec="seconds"))
        self.assertEqual(track[-1]["ts"], (base + timedelta(minutes=49)).isoformat(timespec="seconds"))

    def test_track_for_unknown_hex_is_empty(self):
        conn = make_db()
        self.assertEqual(track_for(conn, "NOPE00", minutes=30), [])

    def test_analyse_fires_on_loiter_and_stays_silent_on_straight_flight(self):
        conn = make_db()
        for p in circle_points(n_points=16, radius_km=1.5, minutes_span=15.0):
            insert_position(conn, p["hex"], datetime.fromisoformat(p["ts"]),
                             alt_ft=p["alt_ft"], lat=p["lat"], lon=p["lon"],
                             speed_kt=p["speed_kt"], track=p["track"],
                             vert_rate=p["vert_rate"], callsign=p["callsign"])
        findings = analyse(conn, "TESTHX", minutes=30)
        rules = {f["rule"] for f in findings}
        self.assertIn("loiter", rules)

        conn2 = make_db()
        for p in straight_points(21):
            insert_position(conn2, p["hex"], datetime.fromisoformat(p["ts"]),
                             alt_ft=p["alt_ft"], lat=p["lat"], lon=p["lon"],
                             speed_kt=p["speed_kt"], track=p["track"],
                             vert_rate=p["vert_rate"])
        findings2 = analyse(conn2, "TESTHX", minutes=30)
        self.assertEqual(findings2, [])  # negative control: nothing fires


if __name__ == "__main__":
    unittest.main(verbosity=2)
