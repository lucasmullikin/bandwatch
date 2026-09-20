#!/usr/bin/env python3
"""Tests for correlate.py (T5 / T6).

Project rule: a detector nobody has watched DECLINE to fire is not a
detector. Every join/cluster rule below gets a positive case AND a
negative control. All fixtures are synthetic, built in an in-memory
sqlite db that mirrors the real events/devices/voice/aircraft_positions
schema -- no external files, no network, no hardware, no 32MB dataset.

Run: python3 analysis/test_correlate.py -v
"""
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from correlate import (
    hex_to_tail, aircraft_identities, related_events,
    find_incidents, store_incidents,
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
CREATE TABLE voice (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  channel TEXT,
  freq_mhz REAL,
  duration_s REAL,
  transcript TEXT,
  watchlist_hit TEXT
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

BOI_TWR_MHZ = 118.1     # conf/air_tower.conf BOI_TWR_118 -- airband
FRS_MHZ = 462.5625      # conf/frs.conf GMRS block -- NOT airband


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def monday_at(hour=0, minute=0, seconds=0, days=0):
    """A guaranteed Monday-anchored UTC datetime, computed at runtime so
    tests never depend on a hardcoded date happening to be a Monday (see
    analysis/test_analysis.py, same convention)."""
    anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monday = anchor - timedelta(days=anchor.weekday())
    return monday + timedelta(days=days, hours=hour, minutes=minute, seconds=seconds)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def insert_event(conn, ts, kind, device_key, lane=None, summary=""):
    conn.execute(
        "INSERT INTO events(ts, lane, kind, device_key, summary, raw) "
        "VALUES (?, ?, ?, ?, ?, '')",
        (iso(ts), lane or kind, kind, device_key, summary),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def insert_device(conn, device_key, kind, label=None):
    conn.execute(
        "INSERT INTO devices(device_key, kind, first_seen, last_seen, seen_count, label) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (device_key, kind, iso(monday_at()), iso(monday_at()), label),
    )


def insert_voice(conn, ts, channel, freq_mhz, transcript=""):
    conn.execute(
        "INSERT INTO voice(ts, channel, freq_mhz, duration_s, transcript) "
        "VALUES (?, ?, ?, 1.0, ?)",
        (iso(ts), channel, freq_mhz, transcript),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def insert_position(conn, hex_, ts, callsign=None):
    conn.execute(
        "INSERT INTO aircraft_positions(ts, hex, callsign, alt_ft, lat, lon, "
        "speed_kt, track, vert_rate, squawk) VALUES (?, ?, ?, 5000, 40.6, -76.2, 120, 90, 0, NULL)",
        (iso(ts), hex_, callsign),
    )


# --- hex_to_tail / aircraft_identities -----------------------------------


class TestHexToTail(unittest.TestCase):
    def test_parses_registration_from_enriched_label(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        self.assertEqual(hex_to_tail(conn, "A4E3FC"), "N412LF")

    def test_lowercase_hex_input_still_resolves(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        self.assertEqual(hex_to_tail(conn, "a4e3fc"), "N412LF")

    def test_type_only_label_has_no_registration(self):
        """Negative control: collector.py writes label as just the type
        ("BELL 407") when registration was empty -- the first word of a
        type description ('BELL') must never be reported as a tail."""
        conn = make_db()
        insert_device(conn, "A00001", "adsb", label="BELL 407")
        self.assertIsNone(hex_to_tail(conn, "A00001"))

    def test_bare_icao_type_code_label_has_no_registration(self):
        conn = make_db()
        insert_device(conn, "A00002", "adsb", label="A320")
        self.assertIsNone(hex_to_tail(conn, "A00002"))

    def test_unenriched_hex_falls_back_to_injected_lookup(self):
        conn = make_db()
        insert_device(conn, "A00003", "adsb", label=None)

        def fake_lookup(hexid):
            self.assertEqual(hexid, "A00003")
            return {"registration": "N999ZZ", "type": "CESSNA 172", "operator": None}

        self.assertEqual(hex_to_tail(conn, "A00003", lookup=fake_lookup), "N999ZZ")

    def test_no_device_row_and_no_lookup_returns_none(self):
        conn = make_db()
        self.assertIsNone(hex_to_tail(conn, "DEADBE"))

    def test_lookup_miss_returns_none(self):
        conn = make_db()
        self.assertIsNone(hex_to_tail(conn, "DEADBE", lookup=lambda h: None))

    def test_empty_database_no_exception(self):
        conn = make_db()
        self.assertIsNone(hex_to_tail(conn, "A4E3FC"))


class TestAircraftIdentities(unittest.TestCase):
    def test_full_shape(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_position(conn, "A4E3FC", monday_at(hour=10), callsign="")
        insert_position(conn, "A4E3FC", monday_at(hour=10, minute=1), callsign="LIFE23")
        ident = aircraft_identities(conn, "A4E3FC")
        self.assertEqual(ident, {
            "hex": "A4E3FC", "tail": "N412LF", "callsign": "LIFE23",
            "label": "N412LF BELL 407", "registration": "N412LF",
        })

    def test_unknown_hex_returns_dict_of_nones(self):
        conn = make_db()
        ident = aircraft_identities(conn, "FFFFFF")
        self.assertEqual(ident, {
            "hex": "FFFFFF", "tail": None, "callsign": None,
            "label": None, "registration": None,
        })


# --- related_events (T5) -------------------------------------------------


class TestRelatedEvents(unittest.TestCase):
    def test_hex_match(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        eid = insert_event(conn, monday_at(hour=10), "adsb", "A4E3FC")
        rows = related_events(conn, "A4E3FC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"], "hex match")
        self.assertEqual(rows[0]["source"], "events")
        self.assertEqual(rows[0]["id"], eid)

    def test_tail_match(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_event(conn, monday_at(hour=10), "acars", "acars/N412LF",
                     summary="N412LF [SQ] out of ISO")
        rows = related_events(conn, "A4E3FC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"], "tail match")

    def test_acars_row_with_no_tail_joins_nothing(self):
        """Negative control (required): 'acars/label-SQ' has no tail --
        must not be invented, must not join by tail."""
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_event(conn, monday_at(hour=10), "acars", "acars/label-SQ",
                     summary="uplink, no tail")
        rows = related_events(conn, "A4E3FC")
        self.assertEqual(rows, [])

    def test_voice_on_airband_within_window_joins_by_proximity(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_position(conn, "A4E3FC", monday_at(hour=10, minute=0))
        vid = insert_voice(conn, monday_at(hour=10, minute=1), "BOI_TWR_118",
                            BOI_TWR_MHZ, transcript="traffic in sight")
        rows = related_events(conn, "A4E3FC", window_s=300)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"], "time+channel proximity")
        self.assertEqual(rows[0]["source"], "voice")
        self.assertEqual(rows[0]["id"], vid)

    def test_voice_on_non_airband_channel_not_joined(self):
        """Negative control (required): FRS 462.x is not airband."""
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_position(conn, "A4E3FC", monday_at(hour=10, minute=0))
        insert_voice(conn, monday_at(hour=10, minute=1), "GMRS15", FRS_MHZ,
                     transcript="breaker breaker")
        rows = related_events(conn, "A4E3FC", window_s=300)
        self.assertEqual(rows, [])

    def test_voice_outside_window_not_joined(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_position(conn, "A4E3FC", monday_at(hour=10, minute=0))
        insert_voice(conn, monday_at(hour=11, minute=0), "BOI_TWR_118", BOI_TWR_MHZ)
        rows = related_events(conn, "A4E3FC", window_s=300)
        self.assertEqual(rows, [])

    def test_two_aircraft_close_in_time_different_hex_do_not_merge(self):
        """Negative control (required)."""
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_device(conn, "B00001", "adsb", label="N555ZZ CESSNA 172")
        insert_event(conn, monday_at(hour=10, minute=0), "adsb", "A4E3FC")
        insert_event(conn, monday_at(hour=10, minute=0, seconds=5), "adsb", "B00001")
        rows = related_events(conn, "A4E3FC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device_key"], "A4E3FC")

    def test_empty_database_no_exception(self):
        conn = make_db()
        self.assertEqual(related_events(conn, "A4E3FC"), [])

    def test_unknown_hex_no_exception(self):
        conn = make_db()
        insert_event(conn, monday_at(hour=10), "adsb", "A4E3FC")
        self.assertEqual(related_events(conn, "FFFFFF"), [])


# --- find_incidents / store_incidents (T6) --------------------------------


class TestFindIncidents(unittest.TestCase):
    def test_multi_lane_aircraft_incident_outscores_single_lane_burst(self):
        """Required control: a 3-event multi-lane incident (adsb + acars +
        voice, joined via hex/tail/proximity) scores higher than a 6-event
        single-lane burst from one device."""
        conn = make_db()

        # Multi-lane: one aircraft, 3 events across 3 lanes.
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_event(conn, monday_at(hour=10, minute=0), "adsb", "A4E3FC")
        insert_event(conn, monday_at(hour=10, minute=1), "acars", "acars/N412LF")
        insert_position(conn, "A4E3FC", monday_at(hour=10, minute=0))
        insert_voice(conn, monday_at(hour=10, minute=2), "BOI_TWR_118", BOI_TWR_MHZ)

        # Single-lane burst: one TPMS sensor, 6 pings, tightly spaced, far
        # away in time from the aircraft incident so the two never merge.
        for i in range(6):
            insert_event(conn, monday_at(hour=20, minute=i), "ism", "tpms/AABBCC")

        incidents = find_incidents(conn, gap_s=240, min_events=3)
        self.assertEqual(len(incidents), 2)

        multi = next(inc for inc in incidents if len(inc["kinds"]) > 1)
        single = next(inc for inc in incidents if len(inc["kinds"]) == 1)
        self.assertEqual(sorted(multi["kinds"]), ["acars", "adsb", "voice"])
        self.assertEqual(len(multi["events"]), 3)
        self.assertEqual(single["kinds"], ["ism"])
        self.assertEqual(len(single["events"]), 6)
        self.assertGreater(multi["score"], single["score"])

    def test_events_separated_by_more_than_gap_s_form_separate_incidents(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        base = monday_at(hour=10)
        # First cluster of 3, tightly spaced.
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=i * 10), "adsb", "A4E3FC")
        # Second cluster of 3, well past gap_s=240s later.
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=1000 + i * 10), "adsb", "A4E3FC")

        incidents = find_incidents(conn, gap_s=240, min_events=3)
        self.assertEqual(len(incidents), 2)
        for inc in incidents:
            self.assertEqual(len(inc["events"]), 3)
        self.assertLess(incidents[0]["end"], incidents[1]["start"])

    def test_below_min_events_is_dropped(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_event(conn, monday_at(hour=10), "adsb", "A4E3FC")
        insert_event(conn, monday_at(hour=10, minute=0, seconds=5), "adsb", "A4E3FC")
        incidents = find_incidents(conn, gap_s=240, min_events=3)
        self.assertEqual(incidents, [])

    def test_two_aircraft_different_hex_do_not_merge_into_one_incident(self):
        """Required control, at the incident-clustering level."""
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        insert_device(conn, "B00001", "adsb", label="N555ZZ CESSNA 172")
        base = monday_at(hour=10)
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=i * 5), "adsb", "A4E3FC")
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=i * 5 + 1), "adsb", "B00001")

        incidents = find_incidents(conn, gap_s=240, min_events=3)
        self.assertEqual(len(incidents), 2)
        for inc in incidents:
            self.assertEqual(len(inc["entities"]), 1)
        entity_sets = {inc["entities"][0] for inc in incidents}
        self.assertEqual(entity_sets, {"A4E3FC", "B00001"})

    def test_acars_with_no_tail_does_not_join_the_aircraft_incident(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        base = monday_at(hour=10)
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=i * 5), "adsb", "A4E3FC")
        # No-tail acars messages, same time window, but must stay separate.
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=i * 5), "acars", "acars/label-SQ")

        incidents = find_incidents(conn, gap_s=240, min_events=3)
        self.assertEqual(len(incidents), 2)
        for inc in incidents:
            self.assertEqual(len(inc["kinds"]), 1)

    def test_voice_on_non_airband_channel_not_folded_into_aircraft_incident(self):
        conn = make_db()
        insert_device(conn, "A4E3FC", "adsb", label="N412LF BELL 407")
        base = monday_at(hour=10)
        for i in range(3):
            insert_event(conn, base + timedelta(seconds=i * 5), "adsb", "A4E3FC")
        insert_position(conn, "A4E3FC", base)
        for i in range(3):
            insert_voice(conn, base + timedelta(seconds=i * 5), "GMRS15", FRS_MHZ)

        incidents = find_incidents(conn, gap_s=240, min_events=3)
        # The aircraft incident and the FRS-chatter incident stay separate.
        self.assertEqual(len(incidents), 2)
        for inc in incidents:
            self.assertEqual(len(inc["kinds"]), 1)

    def test_empty_database_no_exception(self):
        conn = make_db()
        self.assertEqual(find_incidents(conn), [])


class TestStoreIncidents(unittest.TestCase):
    def test_creates_table_and_persists(self):
        conn = make_db()
        incidents = [{
            "start": "2026-01-05T10:00:00+00:00", "end": "2026-01-05T10:02:00+00:00",
            "kinds": ["adsb", "voice"], "entities": ["A4E3FC"],
            "events": ["events:1", "voice:1"], "score": 39.0,
        }]
        n = store_incidents(conn, incidents)
        self.assertEqual(n, 1)
        row = conn.execute(
            "SELECT start, end, kinds, entities, events, score FROM incidents"
        ).fetchone()
        self.assertEqual(row[0], incidents[0]["start"])
        self.assertEqual(json.loads(row[2]), ["adsb", "voice"])
        self.assertEqual(json.loads(row[3]), ["A4E3FC"])
        self.assertEqual(json.loads(row[4]), ["events:1", "voice:1"])
        self.assertEqual(row[5], 39.0)

    def test_empty_list_creates_table_with_no_rows(self):
        conn = make_db()
        n = store_incidents(conn, [])
        self.assertEqual(n, 0)
        count = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 0)

    def test_second_call_replaces_first(self):
        conn = make_db()
        one = [{"start": "t0", "end": "t1", "kinds": ["adsb"], "entities": ["A"],
                "events": ["events:1"], "score": 1.0}]
        two = [
            {"start": "t0", "end": "t1", "kinds": ["adsb"], "entities": ["A"],
             "events": ["events:1"], "score": 1.0},
            {"start": "t2", "end": "t3", "kinds": ["voice"], "entities": ["B"],
             "events": ["voice:1"], "score": 2.0},
        ]
        store_incidents(conn, one)
        store_incidents(conn, two)
        count = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        self.assertEqual(count, 2)

    def test_does_not_touch_existing_tables(self):
        conn = make_db()
        insert_event(conn, monday_at(hour=10), "adsb", "A4E3FC")
        store_incidents(conn, [])
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
