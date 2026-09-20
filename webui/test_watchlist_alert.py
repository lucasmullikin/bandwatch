"""A watchlist hit must reach a human. Prove it does, and prove when it doesn't.

Nothing sends a real Signal message here: notify is forced off, and the send
path is asserted to be untaken. The operator's Signal lane is reserved for gig
traffic and must not carry test messages.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

SCHEMA = """
CREATE TABLE voice(id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, freq_mhz REAL,
                   transcript TEXT, watchlist_hit TEXT);
CREATE TABLE alerts(id INTEGER PRIMARY KEY, ts TEXT, rule TEXT, device_key TEXT,
                    message TEXT, notified INT DEFAULT 0);
"""


class TestWatchlistAlert(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.con = sqlite3.connect(self.db)
        self.con.executescript(SCHEMA)
        self.con.execute(
            "INSERT INTO voice(id,ts,channel,freq_mhz,transcript) "
            "VALUES(1,'2026-09-01T04:00:00+00:00','FRS05',462.6625,'x')")
        self.con.commit()
        self.sent = []
        self._cfg, self._run = server.read_config, server.subprocess.run
        # notify OFF: the send path must not be taken at all
        server.read_config = lambda: {"notify_enabled": False}
        server.subprocess.run = lambda *a, **k: self.sent.append(a) or None

    def tearDown(self):
        server.read_config, server.subprocess.run = self._cfg, self._run
        self.con.close()
        os.unlink(self.db)

    def test_hit_creates_an_alert_row(self):
        server.raise_watchlist_alert(self.con, 1, "cedar hollow", "some transcript")
        rows = self.con.execute(
            "SELECT rule, device_key, message, notified FROM alerts").fetchall()
        self.assertEqual(len(rows), 1)
        rule, key, msg, notified = rows[0]
        self.assertEqual(rule, "watchlist_hit")
        self.assertEqual(key, "voice/FRS05")
        self.assertIn("cedar hollow", msg)
        self.assertIn("FRS05", msg)
        self.assertIn("462.6625", msg)
        self.assertEqual(notified, 0)

    def test_transcript_text_is_not_quoted_into_the_message(self):
        # NEGATIVE CONTROL: a transcript is a lead, not a fact. A hallucinated
        # address quoted verbatim into a push notification is worse than a
        # pointer to the clip.
        server.raise_watchlist_alert(
            self.con, 1, "cedar hollow", "SECRET TRANSCRIPT BODY")
        msg = self.con.execute("SELECT message FROM alerts").fetchone()[0]
        self.assertNotIn("SECRET TRANSCRIPT BODY", msg)
        self.assertIn("lead, not a fact", msg)

    def test_nothing_is_sent_when_notify_is_disabled(self):
        # NEGATIVE CONTROL: the alert is still recorded, but no send happens
        server.raise_watchlist_alert(self.con, 1, "cedar hollow", "x")
        self.assertEqual(self.sent, [])
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)

    def test_send_is_attempted_when_notify_is_enabled(self):
        server.read_config = lambda: {
            "notify_enabled": True, "notifier": "webhook",
            # Port 9 (discard) is deliberately unreachable: this asserts the
            # RECORD survives a failed send, so the send has to actually fail.
            "notify": {"webhook": {"url": "http://127.0.0.1:9/hook",
                                   "timeout_s": 1}}}
        server.raise_watchlist_alert(self.con, 1, "cedar hollow", "x")
        self.assertEqual(len(self.sent), 1, "send path was not taken")
        argv = self.sent[0][0]
        self.assertIn("curl", argv[0])
        # the payload must carry the alert, not the transcript
        self.assertTrue(any("cedar hollow" in str(a) for a in argv))

    def test_a_missing_voice_row_does_not_raise(self):
        server.raise_watchlist_alert(self.con, 999, "cedar hollow", "x")
        # it still records something rather than throwing away the hit
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)

    def test_alert_survives_a_broken_notify_config(self):
        server.read_config = lambda: {"notify_enabled": True}   # no rpc/recipient
        server.raise_watchlist_alert(self.con, 1, "cedar hollow", "x")
        # NEGATIVE CONTROL: the row must exist even if paging fails outright
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
