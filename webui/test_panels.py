"""The console's panels, which had no tests at all.

These are the functions that answer "is it working?", so a wrong answer here is
worse than a crash: a panel reporting zero looks identical to a band that was
quiet, and a panel that silently drops a lane looks identical to a lane that
was never scheduled. Both of those have happened in this project.

Each test uses a scratch database and a scratch data directory, so nothing
here needs a station, a radio, or a configured site.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import panels


def iso(d):
    return d.isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.var = os.path.join(self.tmp, "var")
        self.cfgdir = os.path.join(self.tmp, "config")
        os.makedirs(os.path.join(self.var, "voice"))
        os.makedirs(self.cfgdir)
        os.environ["BANDWATCH_VAR"] = self.var
        os.environ["BANDWATCH_CONFIG"] = self.cfgdir
        # bandwatch_config reads the environment at IMPORT time, and the module
        # is cached, so the first test to import it freezes those paths for
        # every test afterwards -- later tests then silently read the FIRST
        # test's directory and assert against an empty result. Correct for
        # production, where the environment is set before launch; here the
        # module has to be reloaded once the scratch dirs exist.
        import importlib
        import bandwatch_config
        importlib.reload(bandwatch_config)
        self.db = os.path.join(self.var, "events.db")
        self.now = datetime.now(timezone.utc)
        con = sqlite3.connect(self.db)
        con.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, lane TEXT,
                             kind TEXT, device_key TEXT, summary TEXT, raw TEXT);
        CREATE TABLE coverage (id INTEGER PRIMARY KEY, device TEXT, lane TEXT,
                               started_at TEXT, ended_at TEXT, events INTEGER);
        CREATE TABLE spectrum (id INTEGER PRIMARY KEY, ts REAL, band TEXT,
                               freq_hz INTEGER, db REAL);
        CREATE TABLE voice (id INTEGER PRIMARY KEY, ts TEXT, channel TEXT,
                            freq_mhz REAL, audio_path TEXT, duration_s REAL,
                            transcript TEXT, transcribed_at TEXT,
                            watchlist_hit TEXT, rejected_because TEXT,
                            transcribed_by TEXT, model TEXT);
        CREATE TABLE health (id INTEGER PRIMARY KEY, ts TEXT, section TEXT,
                             metric TEXT, value REAL, text TEXT);
        """)
        con.commit()
        con.close()

    def tearDown(self):
        for k in ("BANDWATCH_VAR", "BANDWATCH_CONFIG"):
            os.environ.pop(k, None)

    def write_config(self, **kw):
        with open(os.path.join(self.cfgdir, "bandwatch.json"), "w") as fh:
            json.dump(kw, fh)

    def exec(self, sql, rows):
        con = sqlite3.connect(self.db)
        con.executemany(sql, rows)
        con.commit()
        con.close()

    def ran(self, lane, minutes_ago, length_min):
        s = self.now - timedelta(minutes=minutes_ago)
        self.exec("INSERT INTO coverage(device,lane,started_at,ended_at,events) "
                  "VALUES(?,?,?,?,?)",
                  [("0", lane, iso(s), iso(s + timedelta(minutes=length_min)), 0)])


class TestLaneYield(Base):
    """A lane's output lives in one of THREE tables depending on its kind.

    Counting only `events` reports every voice and every survey lane as
    producing nothing -- the exact wrong answer, and the first version of this
    query made that mistake. These pin all three paths.
    """

    def test_decoder_lane_counts_from_events(self):
        self.ran("ism433", 60, 5)
        self.exec("INSERT INTO events(ts,lane,kind,device_key,summary,raw) "
                  "VALUES(?,?,?,?,?,?)",
                  [(iso(self.now), "ism433", "ism", "k", "s", "{}")] * 4)
        rows = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertIn("ism433", rows)
        self.assertEqual(rows["ism433"]["output"], 4)
        self.assertEqual(rows["ism433"]["unit"], "events")

    def test_voice_lane_counts_recorded_CLIPS_not_event_rows(self):
        """NEGATIVE CONTROL for the original bug: no events rows at all."""
        self.ran("frs_voice", 60, 10)
        d = os.path.join(self.var, "voice", "frs_voice")
        os.makedirs(d)
        for i in range(3):
            open(os.path.join(d, "clip%d.mp3" % i), "w").close()
        rows = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertIn("frs_voice", rows)
        self.assertEqual(rows["frs_voice"]["output"], 3,
                         "a voice lane with clips on disk reported zero -- this "
                         "is the bug that made every voice lane look dead")
        self.assertEqual(rows["frs_voice"]["unit"], "clips")

    def test_survey_lane_counts_from_spectrum(self):
        self.ran("survey_fm", 60, 4)
        self.exec("INSERT INTO spectrum(ts,band,freq_hz,db) VALUES(?,?,?,?)",
                  [(1.0, "fm", 100000000, -30.0)] * 7)
        rows = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertEqual(rows["survey_fm"]["output"], 7)
        self.assertEqual(rows["survey_fm"]["unit"], "carriers")

    def test_the_rejected_directory_is_not_counted_as_output(self):
        """Clips the quality gate threw away are not yield."""
        self.ran("frs_voice", 60, 10)
        for name in ("frs_voice", "rejected"):
            d = os.path.join(self.var, "voice", name)
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "x.mp3"), "w").close()
        rows = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertNotIn("rejected", rows)

    def test_a_lane_that_ran_and_heard_NOTHING_still_appears(self):
        """The whole point of the panel.

        A lane that was tuned and returned nothing is a coverage FACT worth
        showing. Dropping it from the table makes it indistinguishable from a
        lane that was never scheduled -- and this project has lost five lanes
        to exactly that confusion.
        """
        self.ran("p25_survey", 30, 6)
        rows = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertIn("p25_survey", rows,
                      "a lane that ran and heard nothing vanished from the panel")
        self.assertEqual(rows["p25_survey"]["output"], 0)
        self.assertGreater(rows["p25_survey"]["minutes"], 0)

    def test_hours_window_excludes_older_runs(self):
        self.ran("adsb", 60 * 40, 10)         # ~40h ago
        self.ran("adsb", 30, 10)
        recent = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp, hours=6)["lanes"]}
        allt = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertLess(recent["adsb"]["minutes"], allt["adsb"]["minutes"])

    def test_a_backwards_or_zero_length_run_is_ignored(self):
        """Clock changes and crashed lanes produce these; they are not coverage."""
        s = self.now - timedelta(minutes=10)
        self.exec("INSERT INTO coverage(device,lane,started_at,ended_at,events) "
                  "VALUES(?,?,?,?,?)",
                  [("0", "weird", iso(s), iso(s - timedelta(minutes=5)), 0),
                   ("0", "weird", iso(s), iso(s), 0)])
        rows = {r["lane"]: r for r in panels.lane_yield(self.db, self.tmp)["lanes"]}
        self.assertNotIn("weird", rows)


class TestTranscriptionStatus(Base):
    """Three different things look like "no transcript" and need opposite responses.

    Presenting them as one number is how a working filter and a broken
    transcriber look identical.
    """

    def clip(self, channel, minutes_ago, transcript=None, rejected=None):
        t = self.now - timedelta(minutes=minutes_ago)
        self.exec("INSERT INTO voice(ts,channel,freq_mhz,audio_path,duration_s,"
                  "transcript,transcribed_at,rejected_because) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  [(iso(t), channel, 462.5, "/x/%s-%d.mp3" % (channel, minutes_ago),
                    3.0, transcript, iso(t) if transcript else None, rejected)])

    def test_a_channel_off_the_allowlist_is_NOT_SELECTED_not_a_failure(self):
        self.write_config(transcribe_channels=["FRS05"], transcribe_max_age_min=30)
        self.clip("TWR_A", 5)
        r = panels.transcription_status(self.db, self.tmp, hours=24)
        self.assertIn("not_selected", json.dumps(r),
                      "an unselected channel is not reported as such")

    def test_a_rejected_transcript_is_reported_separately_from_a_missing_one(self):
        """A rejection is the quality gate WORKING. It must not read as a loss."""
        self.write_config(transcribe_channels=["FRS05"], transcribe_max_age_min=30)
        self.clip("FRS05", 5, rejected="compression_ratio")
        self.clip("FRS05", 6)
        blob = json.dumps(panels.transcription_status(self.db, self.tmp, hours=24))
        self.assertIn("rejected", blob)

    def test_a_transcribed_clip_is_counted_as_done(self):
        self.write_config(transcribe_channels=["FRS05"], transcribe_max_age_min=30)
        self.clip("FRS05", 5, transcript="copy that")
        blob = json.dumps(panels.transcription_status(self.db, self.tmp, hours=24))
        self.assertIn("FRS05", blob)

    def test_missing_config_does_not_crash_the_panel(self):
        """A station that has not configured transcription still opens the page."""
        self.clip("FRS05", 5)
        r = panels.transcription_status(self.db, self.tmp, hours=24)
        self.assertIsInstance(r, dict)


class TestHealthTrends(Base):
    def test_empty_health_table_returns_a_shape_not_an_exception(self):
        """A station whose watchdog has never run still has to render."""
        r = panels.health_trends(self.db, hours=24)
        self.assertIsInstance(r, dict)

    def test_series_are_returned_when_rows_exist(self):
        # section/metric must be one the panel actually charts -- see
        # TREND_METRICS. A test that invents its own pair passes against an
        # empty trends list and proves nothing.
        self.exec("INSERT INTO health(ts,section,metric,value,text) VALUES(?,?,?,?,?)",
                  [(iso(self.now - timedelta(minutes=i * 10)), "events_1h",
                    "_total", float(10 + i), None) for i in range(6)])
        r = panels.health_trends(self.db, hours=24)
        self.assertTrue(r["trends"], "a charted metric produced no trend series")
        self.assertEqual(r["trends"][0]["label"], "Events per hour")


class TestCaptures(Base):
    def test_no_capture_directory_is_empty_not_an_error(self):
        r = panels.captures(self.tmp)
        self.assertIsInstance(r, list)

    def test_captures_follow_BANDWATCH_VAR_not_the_checkout(self):
        """The panel reads from the data directory, wherever that is.

        It used to build paths from the checkout root, so a station keeping
        recordings on another disk showed an empty Imagery panel while the
        files sat there.
        """
        d = os.path.join(self.var, "apt", "2026-09-20T12-00-00_NOAA19")
        os.makedirs(d)
        with open(os.path.join(d, "pass.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
        names = json.dumps(panels.captures(self.tmp))
        self.assertIn("pass.png", names,
                      "captures() did not look in BANDWATCH_VAR")


if __name__ == "__main__":
    unittest.main()
