#!/usr/bin/env python3
"""Tests for export.py (T14) and alert_policy.py (T21).

Project rule: a guard nobody has watched DECLINE to fire is not a guard.
Every rule below gets a positive case AND a negative control -- in
particular the six controls the task called out explicitly:

  - a CRITICAL alert during quiet hours IS still delivered
  - a normal alert during quiet hours is deferred
  - a wrapping quiet window (22:00-07:00) correctly defers 02:00, allows 12:00
  - the 11th identical alert in an hour is rate-limited but the 1st is not
  - export with a filter that matches nothing writes a valid EMPTY file
    with correct headers, not a crash
  - the manifest sha256 matches the actual bytes written (recomputed)

All fixtures are synthetic, built in an in-memory sqlite db mirroring the
real events/voice/coverage schema, or in a temp directory for file-based
export checks -- no external files, no network, no hardware.

Run: python3 analysis/test_export_policy.py -v
"""
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, time as time_cls, timedelta, timezone

from export import (
    export_events, export_voice, export_coverage, export_bundle,
    EVENTS_COLUMNS, VOICE_COLUMNS, COVERAGE_COLUMNS, LIMITATIONS,
)
from alert_policy import (
    should_notify, classify, build_digest, in_quiet_hours,
    CRITICAL_RULES, NORMAL_RULES, LOW_RULES,
)


# --- schema + fixtures for export.py ------------------------------------

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
CREATE TABLE voice (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  channel TEXT,
  freq_mhz REAL,
  audio_path TEXT UNIQUE,
  duration_s REAL,
  transcript TEXT,
  transcribed_at TEXT,
  watchlist_hit TEXT,
  preserved INTEGER DEFAULT 0,
  preserved_at TEXT,
  sha256 TEXT,
  transcribed_by TEXT,
  model TEXT,
  no_speech_prob REAL,
  avg_logprob REAL,
  compression_ratio REAL,
  rejected_because TEXT
);
CREATE TABLE coverage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL,
  lane TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  events INTEGER DEFAULT 0
);
"""


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def insert_event(conn, ts, kind="ism", lane="ism433", device_key="dev1", summary="s", raw="r"):
    conn.execute(
        "INSERT INTO events(ts, lane, kind, device_key, summary, raw) VALUES (?,?,?,?,?,?)",
        (ts, lane, kind, device_key, summary, raw),
    )
    conn.commit()


def insert_voice(conn, ts, transcript="hello", model="whisper-base",
                  transcribed_by="asr", rejected_because=None, sha256=None):
    conn.execute(
        "INSERT INTO voice(ts, channel, freq_mhz, audio_path, duration_s, "
        "transcript, transcribed_at, watchlist_hit, preserved, preserved_at, "
        "sha256, transcribed_by, model, no_speech_prob, avg_logprob, "
        "compression_ratio, rejected_because) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, "ch1", 462.5625, "audio/%s.wav" % ts.replace(":", ""), 4.2,
         transcript, ts, None, 0, None, sha256, transcribed_by, model,
         0.1, -0.3, 1.2, rejected_because),
    )
    conn.commit()


def insert_coverage(conn, started_at, ended_at=None, device="rtl0", lane="voice", events=3):
    conn.execute(
        "INSERT INTO coverage(device, lane, started_at, ended_at, events) VALUES (?,?,?,?,?)",
        (device, lane, started_at, ended_at, events),
    )
    conn.commit()


class ExportEventsTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        insert_event(self.conn, "2026-08-30T10:00:00+00:00", kind="ism", device_key="a")
        insert_event(self.conn, "2026-08-30T11:00:00+00:00", kind="adsb", device_key="b")
        insert_event(self.conn, "2026-08-31T09:00:00+00:00", kind="ism", device_key="c")

    def test_csv_basic(self):
        buf = io.StringIO()
        n = export_events(self.conn, buf, fmt="csv")
        self.assertEqual(n, 3)
        rows = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(rows[0], EVENTS_COLUMNS)
        self.assertEqual(len(rows), 4)  # header + 3 rows

    def test_json_basic(self):
        buf = io.StringIO()
        n = export_events(self.conn, buf, fmt="json")
        self.assertEqual(n, 3)
        records = json.loads(buf.getvalue())
        self.assertEqual(len(records), 3)
        self.assertEqual(set(records[0].keys()), set(EVENTS_COLUMNS))

    def test_filter_by_kind(self):
        buf = io.StringIO()
        n = export_events(self.conn, buf, fmt="json", kind="adsb")
        self.assertEqual(n, 1)
        records = json.loads(buf.getvalue())
        self.assertEqual(records[0]["device_key"], "b")

    def test_filter_by_since_until(self):
        buf = io.StringIO()
        n = export_events(self.conn, buf, fmt="json",
                           since="2026-08-30T10:30:00+00:00",
                           until="2026-08-30T23:59:59+00:00")
        self.assertEqual(n, 1)
        records = json.loads(buf.getvalue())
        self.assertEqual(records[0]["device_key"], "b")

    def test_filter_matches_nothing_writes_valid_empty_csv(self):
        """Required negative control: a filter matching nothing must write
        a valid, correctly-headered, EMPTY file -- not raise."""
        buf = io.StringIO()
        n = export_events(self.conn, buf, fmt="csv", kind="nonexistent-kind")
        self.assertEqual(n, 0)
        rows = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(rows, [EVENTS_COLUMNS])  # header only, no crash

    def test_filter_matches_nothing_writes_valid_empty_json(self):
        buf = io.StringIO()
        n = export_events(self.conn, buf, fmt="json", kind="nonexistent-kind")
        self.assertEqual(n, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_bad_fmt_raises_valueerror_not_silent(self):
        buf = io.StringIO()
        with self.assertRaises(ValueError):
            export_events(self.conn, buf, fmt="xml")


class ExportVoiceTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        insert_voice(self.conn, "2026-08-30T10:00:00+00:00", transcript="thank you",
                     model="whisper-base", transcribed_by="asr",
                     rejected_because="no_speech_prob 0.91")
        insert_voice(self.conn, "2026-08-30T12:00:00+00:00", transcript="clear traffic",
                     model="whisper-large", transcribed_by="human",
                     sha256="abc123")

    def test_provenance_columns_present(self):
        buf = io.StringIO()
        export_voice(self.conn, buf, fmt="json")
        records = json.loads(buf.getvalue())
        for col in ("sha256", "model", "transcribed_by", "rejected_because"):
            self.assertIn(col, records[0])
        self.assertEqual(records[0]["rejected_because"], "no_speech_prob 0.91")
        self.assertEqual(records[1]["sha256"], "abc123")
        self.assertEqual(records[1]["model"], "whisper-large")

    def test_empty_result_is_valid_not_a_crash(self):
        buf = io.StringIO()
        n = export_voice(self.conn, buf, fmt="csv", since="2099-01-01T00:00:00+00:00")
        self.assertEqual(n, 0)
        rows = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(rows, [VOICE_COLUMNS])


class ExportCoverageTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        insert_coverage(self.conn, "2026-08-30T00:00:00+00:00", "2026-08-30T06:00:00+00:00")
        insert_coverage(self.conn, "2026-08-30T06:00:00+00:00", None)

    def test_basic(self):
        buf = io.StringIO()
        n = export_coverage(self.conn, buf, fmt="csv")
        self.assertEqual(n, 2)
        rows = list(csv.reader(io.StringIO(buf.getvalue())))
        self.assertEqual(rows[0], COVERAGE_COLUMNS)

    def test_empty_result_is_valid_not_a_crash(self):
        buf = io.StringIO()
        n = export_coverage(self.conn, buf, fmt="json", since="2099-01-01T00:00:00+00:00")
        self.assertEqual(n, 0)
        self.assertEqual(json.loads(buf.getvalue()), [])


class ExportBundleTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        insert_event(self.conn, "2026-08-30T10:00:00+00:00", device_key="a")
        insert_event(self.conn, "2026-08-30T11:00:00+00:00", device_key="b")
        insert_voice(self.conn, "2026-08-30T10:30:00+00:00", model="whisper-base")
        insert_coverage(self.conn, "2026-08-30T00:00:00+00:00", "2026-08-30T23:59:59+00:00")
        self.tmpdir = tempfile.mkdtemp(prefix="sdr_export_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bundle_writes_all_files_and_manifest(self):
        manifest = export_bundle(self.conn, self.tmpdir)
        for name in ("events.csv", "voice.csv", "coverage.csv", "manifest.json"):
            self.assertTrue(os.path.exists(os.path.join(self.tmpdir, name)), name)
        self.assertEqual(manifest["files"]["events.csv"]["rows"], 2)
        self.assertEqual(manifest["files"]["voice.csv"]["rows"], 1)
        self.assertEqual(manifest["files"]["coverage.csv"]["rows"], 1)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertIn("transcript_is_a_lead_not_a_fact", manifest["limitations"])
        self.assertIn("coverage_is_not_continuous", manifest["limitations"])
        self.assertIn("freq_mhz_is_reconstructed_not_measured", manifest["limitations"])
        self.assertIn("chanmap.py", manifest["limitations"]["freq_mhz_is_reconstructed_not_measured"])
        # manifest.json itself must also be on disk with the same content
        with open(os.path.join(self.tmpdir, "manifest.json")) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, manifest)

    def test_manifest_sha256_matches_actual_bytes_written(self):
        """Required negative control: recompute sha256 of the file on disk
        and check it against what the manifest claims -- for every file."""
        manifest = export_bundle(self.conn, self.tmpdir)
        for fname, meta in manifest["files"].items():
            path = os.path.join(self.tmpdir, fname)
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read())
            self.assertEqual(h.hexdigest(), meta["sha256"], fname)

    def test_bundle_with_filter_matching_nothing(self):
        manifest = export_bundle(self.conn, self.tmpdir,
                                  since="2099-01-01T00:00:00+00:00")
        self.assertEqual(manifest["files"]["events.csv"]["rows"], 0)
        self.assertEqual(manifest["files"]["voice.csv"]["rows"], 0)
        self.assertEqual(manifest["files"]["coverage.csv"]["rows"], 0)
        self.assertIsNone(manifest["observed_range"]["min_ts"])
        # files must still exist and be valid (header-only) CSVs
        with open(os.path.join(self.tmpdir, "events.csv")) as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [EVENTS_COLUMNS])


# --- alert_policy.py fixtures --------------------------------------------

def alert(rule, ts="2026-08-31T12:00:00+00:00", message="msg", device_key="d1"):
    return {"rule": rule, "ts": ts, "device_key": device_key, "message": message}


class ClassifyTest(unittest.TestCase):
    def test_critical_rules(self):
        for rule in ("emergency_squawk", "watchlist_hit"):
            self.assertEqual(classify(alert(rule)), "critical")

    def test_normal_rules(self):
        for rule in ("notable_aircraft", "new_device"):
            self.assertEqual(classify(alert(rule)), "normal")

    def test_low_rules(self):
        for rule in ("new_carrier", "low_aircraft"):
            self.assertEqual(classify(alert(rule)), "low")

    def test_unknown_rule_defaults_to_normal_not_critical(self):
        self.assertEqual(classify(alert("some_future_rule")), "normal")

    def test_malformed_input_never_raises(self):
        self.assertEqual(classify(None), "normal")
        self.assertEqual(classify({}), "normal")
        self.assertEqual(classify("not a dict"), "normal")
        self.assertEqual(classify({"rule": ["unhashable"]}), "normal")


class QuietHoursTest(unittest.TestCase):
    def test_non_wrapping_window(self):
        hit, _ = in_quiet_hours(time_cls(23, 0), "22:00", "23:30")
        self.assertTrue(hit)
        hit, _ = in_quiet_hours(time_cls(12, 0), "22:00", "23:30")
        self.assertFalse(hit)

    def test_wrapping_window_defers_2am_allows_noon(self):
        """Required negative control."""
        hit, reason = in_quiet_hours(time_cls(2, 0), "22:00", "07:00")
        self.assertTrue(hit, reason)
        hit, reason = in_quiet_hours(time_cls(12, 0), "22:00", "07:00")
        self.assertFalse(hit, reason)

    def test_wrapping_window_boundaries(self):
        self.assertTrue(in_quiet_hours(time_cls(22, 0), "22:00", "07:00")[0])   # start inclusive
        self.assertFalse(in_quiet_hours(time_cls(7, 0), "22:00", "07:00")[0])   # end exclusive
        self.assertTrue(in_quiet_hours(time_cls(6, 59), "22:00", "07:00")[0])

    def test_malformed_config_fails_open_not_quiet(self):
        self.assertEqual(in_quiet_hours(time_cls(2, 0), None, None)[0], False)
        self.assertEqual(in_quiet_hours(time_cls(2, 0), "garbage", "07:00")[0], False)
        self.assertEqual(in_quiet_hours(time_cls(2, 0), "25:99", "07:00")[0], False)

    def test_equal_start_end_is_no_window(self):
        self.assertFalse(in_quiet_hours(time_cls(3, 0), "22:00", "22:00")[0])


class ShouldNotifyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"quiet_start": "22:00", "quiet_end": "07:00",
                    "max_per_rule_per_hour": 10, "max_alerts_per_hour": 30}

    def test_critical_delivered_during_quiet_hours(self):
        """Required negative control."""
        now = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
        ok, reason = should_notify(alert("emergency_squawk"), self.cfg, now, [])
        self.assertTrue(ok, reason)

    def test_normal_deferred_during_quiet_hours(self):
        """Required negative control."""
        now = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
        ok, reason = should_notify(alert("new_device"), self.cfg, now, [])
        self.assertFalse(ok, reason)

    def test_normal_delivered_outside_quiet_hours(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        ok, reason = should_notify(alert("new_device"), self.cfg, now, [])
        self.assertTrue(ok, reason)

    def test_critical_delivered_even_past_rate_limit(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        recent = [alert("emergency_squawk", ts=(now - timedelta(minutes=1)).isoformat())
                  for _ in range(50)]
        ok, reason = should_notify(alert("emergency_squawk"), self.cfg, now, recent)
        self.assertTrue(ok, reason)

    def test_per_rule_rate_limit_11th_blocked_1st_allowed(self):
        """Required negative control."""
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        # 1st: no history yet.
        ok, reason = should_notify(alert("new_device"), self.cfg, now, [])
        self.assertTrue(ok, reason)
        # 11th: 10 identical alerts already landed in the past hour.
        recent = [alert("new_device", ts=(now - timedelta(minutes=i)).isoformat())
                  for i in range(1, 11)]
        ok, reason = should_notify(alert("new_device"), self.cfg, now, recent)
        self.assertFalse(ok, reason)

    def test_per_rule_limit_does_not_block_a_different_rule(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        recent = [alert("new_device", ts=(now - timedelta(minutes=i)).isoformat())
                  for i in range(1, 11)]
        ok, reason = should_notify(alert("notable_aircraft"), self.cfg, now, recent)
        self.assertTrue(ok, reason)

    def test_global_rate_limit(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        cfg = dict(self.cfg, max_alerts_per_hour=5, max_per_rule_per_hour=100)
        recent = [alert("new_device" if i % 2 else "notable_aircraft",
                        ts=(now - timedelta(minutes=i)).isoformat())
                  for i in range(1, 6)]
        ok, reason = should_notify(alert("new_device"), cfg, now, recent)
        self.assertFalse(ok, reason)

    def test_old_alerts_outside_window_dont_count(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        recent = [alert("new_device", ts=(now - timedelta(hours=2)).isoformat())
                  for _ in range(20)]
        ok, reason = should_notify(alert("new_device"), self.cfg, now, recent)
        self.assertTrue(ok, reason)

    def test_malformed_alert_never_raises_and_fails_open(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        ok, reason = should_notify(None, self.cfg, now, None)
        self.assertTrue(ok, reason)
        ok, reason = should_notify({}, {}, now, [{"garbage": True}, None, 42])
        self.assertTrue(ok, reason)

    def test_malformed_now_never_raises(self):
        ok, reason = should_notify(alert("new_device"), self.cfg, "not-a-datetime", [])
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)


class BuildDigestTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(build_digest([]), "No deferred alerts.")
        self.assertEqual(build_digest(None), "No deferred alerts.")

    def test_groups_by_rule_and_collapses_identical_messages(self):
        alerts = [alert("new_device", ts="2026-08-31T02:00:00+00:00", message="new ism device X"),
                  alert("new_device", ts="2026-08-31T02:15:00+00:00", message="new ism device X"),
                  alert("notable_aircraft", ts="2026-08-31T02:30:00+00:00", message="N123AB overhead")]
        digest = build_digest(alerts)
        self.assertIn("Deferred alerts (3):", digest)
        self.assertIn("new_device x2", digest)
        self.assertIn("notable_aircraft x1", digest)
        self.assertEqual(digest.count("new ism device X"), 1)  # collapsed, not repeated

    def test_never_raises_on_malformed_entries(self):
        digest = build_digest([None, {}, {"rule": "x", "message": None, "ts": "bad"}, 42])
        self.assertIsInstance(digest, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
