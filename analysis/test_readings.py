"""Tests for the numeric readings layer.

The controls that matter here are about not fabricating: not inventing a unit,
not claiming a neighbour's sensor is yours, not presenting a stale value as
current, and not turning a non-numeric field into a number.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import readings  # noqa: E402


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class TestParse(unittest.TestCase):
    def test_lifts_real_lacrosse_packet(self):
        raw = ('{"time":"2026-08-30T20:35:53","model":"LaCrosse-TX141Bv3",'
               '"id":89,"battery_ok":0,"temperature_C":21.5,"rssi":-12.1,'
               '"snr":13.5}')
        rows = readings.parse_fields(raw, ts="T", device_key="k")
        got = {r["metric"]: (r["value"], r["unit"]) for r in rows}
        self.assertEqual(got["temperature"], (21.5, "C"))
        self.assertEqual(got["battery_ok"], (0.0, "bool"))
        self.assertEqual(got["snr"], (13.5, "dB"))

    def test_fahrenheit_keeps_its_own_unit(self):
        # NEGATIVE CONTROL: the inFactory decoder emits temperature_F. Storing
        # 88.2 as Celsius would be a 30-degree error that looks plausible.
        rows = readings.parse_fields('{"temperature_F":88.2}')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 88.2)
        self.assertEqual(rows[0]["unit"], "F")

    def test_unknown_field_is_skipped_not_guessed(self):
        rows = readings.parse_fields('{"mystery_reading":42,"temperature_C":1}')
        self.assertEqual([r["metric"] for r in rows], ["temperature"])

    def test_non_numeric_value_is_not_a_reading(self):
        # NEGATIVE CONTROL: rtl_433 emits "test":"No" and mod:"ASK"
        rows = readings.parse_fields('{"temperature_C":"unknown","humidity":55}')
        self.assertEqual([r["metric"] for r in rows], ["humidity"])

    def test_nan_is_rejected(self):
        # NaN compares False against every threshold, so it must never be stored
        rows = readings.parse_fields({"temperature_C": float("nan")})
        self.assertEqual(rows, [])

    def test_booleans_become_numbers(self):
        rows = readings.parse_fields({"battery_ok": True})
        self.assertEqual(rows[0]["value"], 1.0)

    def test_garbage_input_returns_empty(self):
        for bad in ("", "{not json", None, 5, [1, 2]):
            self.assertEqual(readings.parse_fields(bad), [])


class Base(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT,"
                         " kind TEXT, device_key TEXT, raw TEXT)")
        readings.ensure_schema(self.con)
        self.now = datetime.now(timezone.utc)
        self.root = tempfile.mkdtemp()


class TestStore(Base):
    def test_store_and_dedupe(self):
        rows = readings.parse_fields('{"temperature_C":21.5}', ts="T", device_key="k")
        self.assertEqual(readings.store(self.con, rows), 1)
        # NEGATIVE CONTROL: re-storing the identical reading must not duplicate
        self.assertEqual(readings.store(self.con, rows), 0)

    def test_backfill_from_events_is_idempotent(self):
        self.con.executemany(
            "INSERT INTO events(ts,kind,device_key,raw) VALUES(?,?,?,?)",
            [(iso(self.now - timedelta(minutes=i)), "ism", "LaCrosse/89",
              json.dumps({"temperature_C": 20 + i * 0.1, "battery_ok": 0}))
             for i in range(10)])
        self.con.commit()
        n1 = readings.backfill(self.con)
        self.assertEqual(n1, 20)              # temperature + battery_ok each
        n2 = readings.backfill(self.con)
        self.assertEqual(n2, 0)

    def test_backfill_ignores_non_ism_kinds(self):
        self.con.execute(
            "INSERT INTO events(ts,kind,device_key,raw) VALUES(?,?,?,?)",
            (iso(self.now), "adsb", "A4E3FC", '{"temperature_C":99}'))
        self.con.commit()
        self.assertEqual(readings.backfill(self.con), 0)


class TestOwnership(Base):
    def test_nothing_is_mine_by_default(self):
        # NEGATIVE CONTROL: receiving is not owning. With no nomination file,
        # every sensor must read as third-party, never as the operator's.
        owned, _ = readings.load_owned(self.root)
        self.assertEqual(owned, set())
        self.assertEqual(readings.ownership("LaCrosse/89", owned), "third-party")

    def test_nominated_device_is_mine(self):
        with open(os.path.join(self.root, "sensors.json"), "w") as fh:
            json.dump({"mine": ["LaCrosse-TX141Bv3/89"],
                       "labels": {"LaCrosse-TX141Bv3/89": "Back garden"}}, fh)
        owned, labels = readings.load_owned(self.root)
        self.assertEqual(readings.ownership("LaCrosse-TX141Bv3/89", owned), "mine")
        self.assertEqual(readings.ownership("inFactory-TH/115", owned), "third-party")
        self.assertEqual(labels["LaCrosse-TX141Bv3/89"], "Back garden")

    def test_malformed_nomination_file_claims_nothing(self):
        with open(os.path.join(self.root, "sensors.json"), "w") as fh:
            fh.write("{broken")
        owned, _ = readings.load_owned(self.root)
        self.assertEqual(owned, set())

    def test_an_unknown_device_still_APPEARS_just_unclaimed(self):
        """Discovery must not depend on configuration.

        The panel's whole job is to surface devices nobody has told it about.
        Claiming a device changes how it is LABELLED, never whether it is
        SHOWN -- so a station with no sensors.json at all still sees
        everything it hears, marked third-party.

        This is the direction that would fail silently: an ownership filter
        applied one line too early turns "we heard 14 devices, 4 are yours"
        into "we heard 4 devices", and the panel looks completely normal.
        """
        self.assertFalse(os.path.exists(os.path.join(self.root, "sensors.json")),
                         "this test needs the no-config case")
        readings.store(self.con, [{
            "ts": iso(self.now), "device_key": "Nobody-Ever-Heard-Of-This/77",
            "metric": "temperature", "value": 4.0, "unit": "C",
            "quality": "measured", "source": "t"}])
        rows = readings.latest(self.con, root=self.root)
        keys = [r["device_key"] for r in rows]
        self.assertIn("Nobody-Ever-Heard-Of-This/77", keys,
                      "an unclaimed device vanished from the panel -- discovery "
                      "is being gated on ownership")
        row = [r for r in rows if r["device_key"] == "Nobody-Ever-Heard-Of-This/77"][0]
        self.assertEqual(row["owner"], "third-party")
        self.assertIsNone(row["label"])

    def test_claiming_one_device_does_not_hide_the_others(self):
        """The positive control for the test above.

        With a nomination file present, the claimed device is labelled AND the
        unclaimed one is still listed. A filter that survived the no-config
        case could still drop unclaimed devices once a file exists.
        """
        with open(os.path.join(self.root, "sensors.json"), "w") as fh:
            json.dump({"mine": ["Mine/1"], "labels": {"Mine/1": "Back garden"}}, fh)
        for key in ("Mine/1", "Theirs/2"):
            readings.store(self.con, [{
                "ts": iso(self.now), "device_key": key, "metric": "temperature",
                "value": 4.0, "unit": "C", "quality": "measured", "source": "t"}])
        rows = {r["device_key"]: r for r in readings.latest(self.con, root=self.root)}
        self.assertIn("Theirs/2", rows, "an unclaimed device was hidden once a "
                                        "nomination file existed")
        self.assertEqual(rows["Mine/1"]["owner"], "mine")
        self.assertEqual(rows["Mine/1"]["label"], "Back garden")
        self.assertEqual(rows["Theirs/2"]["owner"], "third-party")


class TestLatest(Base):
    def seed(self, minutes_ago, value=21.5, key="LaCrosse/89", metric="temperature"):
        readings.store(self.con, [{
            "ts": iso(self.now - timedelta(minutes=minutes_ago)),
            "device_key": key, "metric": metric, "value": value,
            "unit": "C", "quality": "measured", "source": "t"}])

    def test_fresh_reading_is_not_stale(self):
        self.seed(2)
        r = readings.latest(self.con, root=self.root)[0]
        self.assertFalse(r["stale"])
        self.assertLess(r["age_min"], 5)
        self.assertEqual(r["owner"], "third-party")

    def test_old_reading_is_marked_stale(self):
        # a two-hour-old number must not render as current
        self.seed(150)
        r = readings.latest(self.con, root=self.root)[0]
        self.assertTrue(r["stale"])
        self.assertGreater(r["age_min"], 120)

    def test_link_metrics_are_excluded_from_the_panel(self):
        # NEGATIVE CONTROL: rssi describes the radio link, not the world, and
        # must not sit beside temperature as though it were environment data
        self.seed(1, value=-12.0, metric="rssi")
        self.seed(1, value=21.5, metric="temperature")
        got = [r["metric"] for r in readings.latest(self.con, root=self.root)]
        self.assertIn("temperature", got)
        self.assertNotIn("rssi", got)

    def test_only_the_newest_value_per_metric(self):
        self.seed(60, value=10.0)
        self.seed(1, value=20.0)
        rows = readings.latest(self.con, root=self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 20.0)

    def test_empty_db(self):
        self.assertEqual(readings.latest(self.con, root=self.root), [])


class TestSeries(Base):
    def test_downsample_keeps_the_newest_point(self):
        rows = []
        for i in range(1000):
            rows.append({"ts": iso(self.now - timedelta(seconds=1000 - i)),
                         "device_key": "k", "metric": "temperature",
                         "value": float(i), "unit": "C",
                         "quality": "measured", "source": "t"})
        readings.store(self.con, rows)
        s = readings.series(self.con, "k", "temperature", hours=1, max_points=50)
        self.assertEqual(len(s), 50)
        # NEGATIVE CONTROL: the last point is what a panel shows as "now".
        # A trimmed tail would display a stale number as current.
        self.assertEqual(s[-1][1], 999.0)

    def test_window_excludes_old(self):
        readings.store(self.con, [{
            "ts": iso(self.now - timedelta(hours=48)), "device_key": "k",
            "metric": "temperature", "value": 1.0, "unit": "C",
            "quality": "measured", "source": "t"}])
        self.assertEqual(readings.series(self.con, "k", "temperature", hours=6), [])
        self.assertEqual(len(readings.series(self.con, "k", "temperature", hours=72)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
