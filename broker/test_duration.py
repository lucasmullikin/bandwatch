"""duration_s must be measured, never guessed or taken from the model.

Two independent sources were writing this column and both were wrong:
  * ingest estimated size/4000, i.e. a fixed 32 kbps, while rtl_airband writes
    VBR (measured 12.5-17 kbps on consecutive clips);
  * the transcription worker sent the end timestamp of Whisper's last segment,
    which on a hallucinated transcript runs past the end of the audio, and
    save_transcript PREFERRED it over the measured value.

Result: 912 of 977 rows (93%) were wrong, some by 30 seconds.
"""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")


class TestDuration(unittest.TestCase):
    def test_no_fixed_bitrate_divisor_remains(self):
        """The size/4000 heuristic must be gone -- it cannot work on VBR."""
        src = open(os.path.join(ROOT, "broker", "collector.py")).read()
        self.assertNotIn("getsize(full) / 4000", src)
        self.assertIn("audio_duration_s(full)", src)

    def test_measured_duration_wins_over_the_worker(self):
        """COALESCE order decides whose number survives."""
        src = open(os.path.join(ROOT, "webui", "server.py")).read()
        self.assertIn("duration_s=COALESCE(duration_s,?)", src,
                      "the worker's estimate would override the measurement")
        self.assertNotIn("duration_s=COALESCE(?,duration_s)", src)

    def test_missing_file_returns_None_not_a_guess(self):
        """An absent value is honest; a fabricated one silently mis-gates."""
        self.assertIsNone(collector.audio_duration_s("/nonexistent/nope.mp3"))

    def test_real_clip_matches_ffprobe(self):
        import glob
        files = sorted(glob.glob(os.path.join(VAR, "voice", "*", "*.mp3")))
        if not files:
            self.skipTest("no audio on disk")
        f = files[-1]
        got = collector.audio_duration_s(f)
        self.assertIsNotNone(got)
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            self.skipTest("ffprobe not installed")
        ref = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", f],
            capture_output=True, text=True, timeout=15).stdout.strip()
        self.assertAlmostEqual(got, float(ref), places=1)

    def test_stored_durations_now_agree_with_the_files(self):
        """The backfill actually landed -- checked against real audio.

        This one needs a station that has been running: it audits rows a live
        collector wrote against the audio still on disk. It is kept rather than
        deleted because it is the only check that proves the backfill LANDED
        rather than that the code would land it, but it can only mean anything
        where there is real data, so it skips cleanly everywhere else --
        including CI.

        sqlite3.connect() CREATES an empty database when the file is absent, so
        the existence check has to come first; without it this fails with "no
        such table: voice", which reads like a schema bug rather than a missing
        station.
        """
        import sqlite3
        db = os.path.join(VAR, "events.db")
        if not os.path.exists(db):
            self.skipTest("no event store at %s -- this check needs a live station" % db)
        con = sqlite3.connect(db)
        # Existence of the FILE is not existence of the DATA. An earlier,
        # buggier version of this test called sqlite3.connect() before checking
        # anything, which CREATED an empty database -- and that empty file then
        # satisfied every later existence check, so the guard passed and the
        # query failed with "no such table: voice". Ask for the table.
        if not con.execute("SELECT name FROM sqlite_master "
                           "WHERE type='table' AND name='voice'").fetchone():
            con.close()
            self.skipTest("event store has no voice table -- no station has run here")
        bad = checked = 0
        for vid, d, p in con.execute(
                "SELECT id,duration_s,audio_path FROM voice "
                "WHERE duration_s IS NOT NULL AND audio_path IS NOT NULL "
                "ORDER BY id DESC LIMIT 25"):
            if not os.path.exists(p):
                continue
            real = collector.audio_duration_s(p)
            if real is None:
                continue
            checked += 1
            if abs(real - d) > max(0.5, 0.15 * real):
                bad += 1
        con.close()
        if checked < 5:
            self.skipTest("not enough audio retained to check")
        self.assertEqual(bad, 0, "%d of %d stored durations still disagree"
                         % (bad, checked))


if __name__ == "__main__":
    unittest.main()
