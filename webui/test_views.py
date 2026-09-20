"""Tests for the map / pattern / entity view layer.

Every check here exists because the corresponding mistake is cheap to make and
invisible once made: a downsampled track that quietly moves an aircraft's last
known position, an hour histogram built in UTC and labelled as local, a single
stray fix drawn as a "track".
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import views  # noqa: E402

SCHEMA = """
CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT, lane TEXT, kind TEXT,
                    device_key TEXT, summary TEXT, raw TEXT, preserved INT DEFAULT 0);
CREATE TABLE devices(device_key TEXT PRIMARY KEY, kind TEXT, first_seen TEXT,
                     last_seen TEXT, seen_count INT, label TEXT);
CREATE TABLE voice(id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, freq_mhz REAL,
                   duration_s REAL, transcript TEXT, watchlist_hit TEXT,
                   preserved INT, rejected_because TEXT, model TEXT);
CREATE TABLE alerts(id INTEGER PRIMARY KEY, ts TEXT, rule TEXT, device_key TEXT,
                    message TEXT, notified INT);
CREATE TABLE coverage(id INTEGER PRIMARY KEY, device TEXT, lane TEXT,
                      started_at TEXT, ended_at TEXT, events INT);
CREATE TABLE aircraft_positions(id INTEGER PRIMARY KEY, ts TEXT, hex TEXT,
                                callsign TEXT, alt_ft INT, lat REAL, lon REAL,
                                speed_kt INT, track INT, vert_rate INT, squawk TEXT);
"""


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        os.unlink(self.db)

    def exec(self, sql, rows):
        con = sqlite3.connect(self.db)
        con.executemany(sql, rows)
        con.commit()
        con.close()


class TestDownsample(Base):
    def test_keeps_endpoints(self):
        pts = list(range(1000))
        out = views._downsample(pts, cap=50)
        self.assertEqual(len(out), 50)
        self.assertEqual(out[0], 0)
        # NEGATIVE CONTROL: the last fix must survive. A track whose tail is
        # trimmed silently reports a stale last-known position.
        self.assertEqual(out[-1], 999)

    def test_short_track_untouched(self):
        pts = [1, 2, 3]
        self.assertIs(views._downsample(pts, cap=50), pts)

    def test_monotonic(self):
        out = views._downsample(list(range(500)), cap=40)
        self.assertEqual(out, sorted(out))


class TestTracks(Base):
    def seed(self):
        rows = []
        t = self.now - timedelta(minutes=30)
        for i in range(20):
            rows.append((iso(t + timedelta(seconds=i * 20)), "A4E3FC", "SWA123",
                         30000 + i * 10, 40.5 + i * 0.01, -76.2 + i * 0.01,
                         420, 90, 0, "1200"))
        # a second aircraft, close in time, DIFFERENT hex
        for i in range(8):
            rows.append((iso(t + timedelta(seconds=i * 20)), "AC0598", "N874EB",
                         5000, 40.7, -76.4, 130, 180, -900, "1234"))
        # a lone fix: one point is not a track
        rows.append((iso(t), "BBBBBB", None, 9000, 41.0, -117.0, 200, 0, 0, None))
        self.exec("INSERT INTO aircraft_positions(ts,hex,callsign,alt_ft,lat,lon,"
                  "speed_kt,track,vert_rate,squawk) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        self.exec("INSERT INTO devices(device_key,kind,first_seen,last_seen,"
                  "seen_count,label) VALUES(?,?,?,?,?,?)",
                  [("A4E3FC", "adsb", iso(t), iso(t), 20, "N412LF BELL 407")])

    def test_groups_by_hex_and_never_merges(self):
        self.seed()
        r = views.tracks(self.db, since_h=6)
        hexes = {t["hex"] for t in r["tracks"]}
        # NEGATIVE CONTROL: two aircraft overlapping in time do NOT become one
        self.assertIn("A4E3FC", hexes)
        self.assertIn("AC0598", hexes)
        self.assertEqual(len(r["tracks"]), 2)

    def test_single_fix_is_not_a_track(self):
        self.seed()
        r = views.tracks(self.db, since_h=6)
        self.assertNotIn("BBBBBB", {t["hex"] for t in r["tracks"]})

    def test_label_attached(self):
        self.seed()
        r = views.tracks(self.db, since_h=6)
        a = next(t for t in r["tracks"] if t["hex"] == "A4E3FC")
        self.assertEqual(a["label"], "N412LF BELL 407")
        self.assertEqual(a["callsign"], "SWA123")
        self.assertEqual(a["n"], 20)

    def test_hex_filter(self):
        self.seed()
        r = views.tracks(self.db, since_h=6, hex_filter="ac0598")
        self.assertEqual([t["hex"] for t in r["tracks"]], ["AC0598"])

    def test_window_excludes_old(self):
        self.exec("INSERT INTO aircraft_positions(ts,hex,lat,lon,alt_ft) "
                  "VALUES(?,?,?,?,?)",
                  [(iso(self.now - timedelta(hours=48)), "OLDOLD", 40.0, -76.0, 100),
                   (iso(self.now - timedelta(hours=48)), "OLDOLD", 40.1, -76.1, 100)])
        self.assertEqual(views.tracks(self.db, since_h=6)["tracks"], [])
        self.assertEqual(len(views.tracks(self.db, since_h=72)["tracks"]), 1)

    def test_empty_db_is_not_an_error(self):
        r = views.tracks(self.db, since_h=6)
        self.assertEqual(r["tracks"], [])
        self.assertIsNone(r["center"])

    def test_center_is_derived_not_hardcoded(self):
        self.seed()
        r = views.tracks(self.db, since_h=6)
        self.assertEqual(r["center"]["source"], "median of received positions")
        self.assertTrue(40.0 < r["center"]["lat"] < 41.5)


class TestPatterns(Base):
    def setUp(self):
        """Pin a NON-UTC zone for the duration of these tests.

        views.LOCAL now defaults to UTC, which is the right default but makes
        the negative control below inert: at offset 0 the local hour and the
        UTC hour are the same bucket, so a histogram built in UTC would pass a
        test meant to catch exactly that. A guard that cannot observe a
        negative is not a guard, so the zone is pinned explicitly here.
        """
        super().setUp()
        self._tz = views.LOCAL
        try:
            from zoneinfo import ZoneInfo
            views.LOCAL = ZoneInfo("America/Denver")
        except Exception:
            views.LOCAL = timezone(timedelta(hours=-6))

    def tearDown(self):
        views.LOCAL = self._tz
        down = getattr(super(), "tearDown", None)
        if down:
            down()

    def test_buckets_are_local_time_not_utc(self):
        # 06:00 UTC is 00:00 local at UTC-6
        d = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary) "
                  "VALUES(?,?,?,?,?)", [(iso(d), "l", "ism", "k", "s")])
        # widen the window so the fixed date is inside it
        days = (datetime.now(timezone.utc) - d).days + 2
        r = views.patterns(self.db, days=days)
        h = r["hourly"]["ism"]
        local_hour = d.astimezone(views.LOCAL).hour
        self.assertEqual(h[local_hour], 1)
        # NEGATIVE CONTROL. setUp pins a non-UTC zone precisely so this can
        # fire; if it is ever skipped, the test above proves nothing.
        self.assertNotEqual(local_hour, 6,
                            "the pinned zone is UTC -- the control below is inert")
        self.assertEqual(h[6], 0, "histogram was built in UTC")

    def test_kind_separation_and_dow(self):
        d = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)   # a Thursday
        days = (datetime.now(timezone.utc) - d).days + 2
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary) "
                  "VALUES(?,?,?,?,?)",
                  [(iso(d), "l", "ism", "a", "s"), (iso(d), "l", "adsb", "b", "s")])
        r = views.patterns(self.db, days=days)
        self.assertIn("ism", r["hourly"])
        self.assertIn("adsb", r["hourly"])
        loc = d.astimezone(views.LOCAL)
        self.assertEqual(r["dow_hour"]["ism"][loc.weekday()][loc.hour], 1)

    def test_filter_by_kind(self):
        d = datetime.now(timezone.utc) - timedelta(hours=2)
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary) "
                  "VALUES(?,?,?,?,?)",
                  [(iso(d), "l", "ism", "a", "s"), (iso(d), "l", "adsb", "b", "s")])
        r = views.patterns(self.db, days=2, kinds=["ism"])
        self.assertEqual(list(r["hourly"]), ["ism"])

    def test_empty_db(self):
        r = views.patterns(self.db, days=7)
        self.assertEqual(r["hourly"], {})
        self.assertIsNone(r["busiest_hour"])

    def test_coverage_denominator_present(self):
        a = self.now - timedelta(hours=3)
        self.exec("INSERT INTO coverage(device,lane,started_at,ended_at,events) "
                  "VALUES(?,?,?,?,?)",
                  [("0", "ism", iso(a), iso(a + timedelta(minutes=90)), 5)])
        r = views.patterns(self.db, days=2)
        secs = r["coverage_seconds_by_hour"]["ism"]
        self.assertAlmostEqual(sum(secs), 5400, delta=2)

    def test_malformed_timestamp_is_skipped_not_fatal(self):
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary) "
                  "VALUES(?,?,?,?,?)", [("not-a-date", "l", "ism", "a", "s")])
        r = views.patterns(self.db, days=2)
        self.assertEqual(r["hourly"], {})


class TestEntity(Base):
    def seed(self):
        t = self.now - timedelta(hours=1)
        self.exec("INSERT INTO devices(device_key,kind,first_seen,last_seen,"
                  "seen_count,label) VALUES(?,?,?,?,?,?)",
                  [("LaCrosse-TX141Bv3/89", "ism", iso(t), iso(self.now), 42, None),
                   ("A4E3FC", "adsb", iso(t), iso(self.now), 20, "N412LF BELL 407")])
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary,raw) "
                  "VALUES(?,?,?,?,?,?)",
                  [(iso(t), "433", "ism", "LaCrosse-TX141Bv3/89", "21.5C",
                    '{"temperature_C": 21.5, "battery_ok": 0}')])
        self.exec("INSERT INTO alerts(ts,rule,device_key,message,notified) "
                  "VALUES(?,?,?,?,?)",
                  [(iso(t), "new_device", "LaCrosse-TX141Bv3/89", "new", 1)])

    def test_unknown_key_returns_error_not_exception(self):
        r = views.entity(self.db, "nope")
        self.assertIn("error", r)

    def test_ism_detail_parses_raw(self):
        self.seed()
        r = views.entity(self.db, "LaCrosse-TX141Bv3/89")
        self.assertEqual(r["seen_count"], 42)
        self.assertEqual(r["readings"][0]["temperature_C"], 21.5)
        self.assertEqual(r["alerts"][0]["rule"], "new_device")
        self.assertEqual(sum(r["hours_local"]), 1)

    def test_bad_raw_json_does_not_break_detail(self):
        self.seed()
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary,raw) "
                  "VALUES(?,?,?,?,?,?)",
                  [(iso(self.now), "433", "ism", "LaCrosse-TX141Bv3/89", "x", "{oops")])
        r = views.entity(self.db, "LaCrosse-TX141Bv3/89")
        self.assertEqual(len(r["readings"]), 2)

    def test_aircraft_detail(self):
        self.seed()
        t = self.now - timedelta(minutes=10)
        self.exec("INSERT INTO aircraft_positions(ts,hex,callsign,alt_ft,lat,lon,"
                  "speed_kt,squawk) VALUES(?,?,?,?,?,?,?,?)",
                  [(iso(t), "A4E3FC", "SWA123", 30000, 40.5, -76.2, 420, "1200"),
                   (iso(self.now), "A4E3FC", "SWA123", 31000, 40.6, -76.3, 430, "1200")])
        r = views.entity(self.db, "A4E3FC")
        self.assertEqual(r["aircraft"]["positions"], 2)
        self.assertEqual(r["aircraft"]["alt_max"], 31000)
        self.assertEqual(r["aircraft"]["callsigns"], ["SWA123"])

    def test_search(self):
        self.seed()
        hits = views.search_entities(self.db, "LaCrosse")
        self.assertEqual(len(hits), 1)
        self.assertEqual(views.search_entities(self.db, "zzzz"), [])


class TestGeometry(unittest.TestCase):
    def test_due_north(self):
        b, nm = views.bearing_range(40.0, -76.0, 41.0, -76.0)
        self.assertAlmostEqual(b, 0.0, places=1)
        self.assertAlmostEqual(nm, 60.0, delta=0.5)      # 1 deg lat == 60 nm

    def test_due_east(self):
        b, _ = views.bearing_range(40.0, -76.0, 40.0, -75.0)
        self.assertAlmostEqual(b, 90.0, delta=0.5)

    def test_zero_range(self):
        b, nm = views.bearing_range(40.0, -76.0, 40.0, -76.0)
        self.assertEqual(nm, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
