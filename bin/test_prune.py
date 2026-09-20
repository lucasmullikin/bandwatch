"""The disk pruner is allowed to delete data. That is why it needs tests.

It had none, and it did this on a real station: over six hours it deleted
1,068,613 event rows to free 0.016 GB, then kept deleting thousands more every
hour while reporting "freed 0.000 GB" each time. It never reached its target
because the target was never its to reach -- the volume was shared, another
project had filled it, and bandwatch's own footprint was 1.29 GB against a
50 GB budget. A 14-day retention was configured throughout. Two days survived.

Three properties hold that line, and each one failed in that incident:

  * Volume pressure is not OUR pressure. If the disk is full but we are inside
    our own budget, deleting our data does not fix it. Evict what is cheap,
    then say so loudly.
  * retention_days is a FLOOR, not a suggestion. Nothing inside the window goes,
    whatever the pressure.
  * Measure what was actually freed. A step that cannot tell must not report
    zero and let the next run start over.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bw_prune as P  # noqa: E402

GB = 1024 ** 3


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def station(tmp_path, monkeypatch):
    """A tiny station: a db with events of known ages, and some junk files."""
    var = tmp_path / "var"
    (var / "voice" / "rejected").mkdir(parents=True)
    (var / "logs").mkdir(parents=True)
    db = var / "events.db"

    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, raw TEXT NOT NULL, preserved INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE voice (id INTEGER PRIMARY KEY, ts TEXT, "
                "audio_path TEXT, preserved INTEGER DEFAULT 0)")
    pad = "x" * 400
    for age in (40, 30, 20, 16, 15):          # OUTSIDE a 14-day retention
        con.execute("INSERT INTO events (ts, raw) VALUES (?,?)", (iso(age), pad))
    for age in (10, 5, 1):                    # INSIDE it
        con.execute("INSERT INTO events (ts, raw) VALUES (?,?)", (iso(age), pad))
    rec = var / "voice" / "old.wav"
    rec.write_bytes(b"a" * 5000)
    con.execute("INSERT INTO voice (id, ts, audio_path) VALUES (1,?,?)",
                (iso(30), str(rec)))
    con.commit()
    con.close()

    for i in range(3):
        (var / "voice" / "rejected" / ("r%d.wav" % i)).write_bytes(b"j" * 1000)

    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    monkeypatch.setattr(P, "VAR", str(var))
    monkeypatch.setattr(P, "DB", str(db))
    return tmp_path, db


def events_left(db):
    con = sqlite3.connect(str(db))
    try:
        return [r[0] for r in con.execute("SELECT ts FROM events ORDER BY ts")]
    finally:
        con.close()


def inside_retention(rows, days=14):
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r for r in rows if r >= cut]


# --------------------------------------------------------------- the floor

def test_events_inside_retention_survive_any_budget_pressure(station):
    _, db = station
    before = inside_retention(events_left(db))
    P.plan(need_budget=10 * GB, need_volume=0, retention_days=14, apply=True)
    assert inside_retention(events_left(db)) == before


def test_budget_pressure_does_evict_events_outside_retention(station):
    _, db = station
    P.plan(need_budget=10 * GB, need_volume=0, retention_days=14, apply=True)
    assert len(events_left(db)) < 8
    assert inside_retention(events_left(db))          # but not to zero


def test_retention_floor_is_the_stopping_point_not_the_target(station):
    """With unbounded pressure, exactly the outside-retention rows go."""
    _, db = station
    P.plan(need_budget=999 * GB, need_volume=0, retention_days=14, apply=True)
    rows = events_left(db)
    assert len(rows) == 3
    assert rows == inside_retention(rows)


def test_a_zero_day_retention_still_keeps_preserved_rows(station):
    _, db = station
    con = sqlite3.connect(str(db))
    con.execute("UPDATE events SET preserved=1 WHERE ts=(SELECT MIN(ts) FROM events)")
    con.commit()
    con.close()
    P.plan(need_budget=999 * GB, need_volume=0, retention_days=0, apply=True)
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM events "
                           "WHERE COALESCE(preserved,0)=1").fetchone()[0] == 1
    finally:
        con.close()


# ------------------------------------------------- whose pressure is it

def test_volume_pressure_never_deletes_events(station):
    """The incident. The disk was full; it was not full of us."""
    _, db = station
    before = events_left(db)
    P.plan(need_budget=0, need_volume=10 * GB, retention_days=14, apply=True)
    assert events_left(db) == before


def test_volume_pressure_never_deletes_recordings(station):
    tmp, db = station
    rec = tmp / "var" / "voice" / "old.wav"
    P.plan(need_budget=0, need_volume=10 * GB, retention_days=14, apply=True)
    assert rec.exists()


def test_volume_pressure_still_evicts_rejected_clips(station):
    """Cheap bytes are still worth taking -- it must not refuse to do anything."""
    tmp, _ = station
    rej = tmp / "var" / "voice" / "rejected"
    steps, _ = P.plan(need_budget=0, need_volume=10 * GB, retention_days=14, apply=True)
    assert not list(rej.iterdir())
    assert any("rejected" in s[0] for s in steps)


def test_volume_pressure_reports_that_the_disk_is_not_ours(station):
    steps, _ = P.plan(need_budget=0, need_volume=10 * GB, retention_days=14, apply=True)
    assert any("not" in s[0].lower() and "ours" in s[0].lower() for s in steps), steps


def test_both_pressures_still_respects_the_retention_floor(station):
    _, db = station
    before = inside_retention(events_left(db))
    P.plan(need_budget=5 * GB, need_volume=10 * GB, retention_days=14, apply=True)
    assert inside_retention(events_left(db)) == before


# ------------------------------------------------------------- honesty

def test_dry_run_changes_nothing(station):
    tmp, db = station
    before = events_left(db)
    rej = sorted(p.name for p in (tmp / "var" / "voice" / "rejected").iterdir())
    P.plan(need_budget=999 * GB, need_volume=999 * GB, retention_days=14, apply=False)
    assert events_left(db) == before
    assert sorted(p.name for p in (tmp / "var" / "voice" / "rejected").iterdir()) == rej


def test_event_eviction_reports_bytes_actually_freed(tmp_path, monkeypatch):
    """It reported 0.000 GB after deleting a million rows, so it ran again.

    Built at a scale where VACUUM genuinely moves the file, and with NO other
    evictable bytes anywhere -- no rejected clips, no recordings, no logs. The
    smaller fixture cannot prove this: `freed` accumulates across steps, so a
    few junk files in step 1 would satisfy "freed > 0" while the event step
    contributed nothing at all, which is the exact bug under test.
    """
    var = tmp_path / "var"
    var.mkdir()
    db = var / "events.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, raw TEXT NOT NULL, preserved INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE voice (id INTEGER PRIMARY KEY, ts TEXT, "
                "audio_path TEXT, preserved INTEGER DEFAULT 0)")
    con.executemany("INSERT INTO events (ts, raw) VALUES (?,?)",
                    [(iso(40), "x" * 400) for _ in range(20000)])
    con.executemany("INSERT INTO events (ts, raw) VALUES (?,?)",
                    [(iso(1), "x" * 400) for _ in range(100)])
    con.commit()
    con.close()

    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    monkeypatch.setattr(P, "VAR", str(var))
    monkeypatch.setattr(P, "DB", str(db))

    before = P.db_bytes()
    steps, freed = P.plan(need_budget=999 * GB, need_volume=0,
                          retention_days=14, apply=True)
    shrank = before - P.db_bytes()

    ev = [s for s in steps if "event" in s[0]]
    assert ev, steps
    assert ev[0][1] == 20000, "should evict exactly the outside-retention rows"
    assert shrank > 0, "the file did not shrink, so there is nothing to report"
    assert ev[0][2] == shrank, (
        "reported %d bytes freed, file actually shrank %d" % (ev[0][2], shrank))
    assert len(events_left(db)) == 100


def test_unreachable_target_is_reported_not_chased(tmp_path, monkeypatch):
    """The other half of the incident: it kept going, hourly, saying nothing.

    Once everything outside retention is gone, a remaining shortfall is a fact
    about the configuration, not more work to do.
    """
    var = tmp_path / "var"
    var.mkdir()
    db = var / "events.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT NOT NULL, raw TEXT NOT NULL, preserved INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE voice (id INTEGER PRIMARY KEY, ts TEXT, "
                "audio_path TEXT, preserved INTEGER DEFAULT 0)")
    con.executemany("INSERT INTO events (ts, raw) VALUES (?,?)",
                    [(iso(1), "x" * 400) for _ in range(500)])
    con.commit()
    con.close()
    monkeypatch.setattr(P, "ROOT", str(tmp_path))
    monkeypatch.setattr(P, "VAR", str(var))
    monkeypatch.setattr(P, "DB", str(db))

    steps, _ = P.plan(need_budget=999 * GB, need_volume=0,
                      retention_days=14, apply=True)
    assert len(events_left(db)) == 500, "nothing outside retention existed to take"
    assert any("STILL" in s[0] and "SHORT" in s[0] for s in steps), steps


def test_nothing_to_do_is_no_steps(station):
    steps, freed = P.plan(need_budget=0, need_volume=0, retention_days=14, apply=True)
    assert steps == []
    assert freed == 0
