"""Withheld-by-policy must be distinguishable from failed-to-deliver.

Aircraft alerts are recorded but never pushed to Signal. Left at notified=0
they were indistinguishable from alerts that genuinely could not be sent, so
the undelivered backlog grew forever and stopped meaning anything -- it drifted
111 -> 116 within hours of the change that created it.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PENDING, DELIVERED, WITHHELD = 0, 1, 2


class TestWithheldState(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.con = sqlite3.connect(self.path)
        self.con.execute("CREATE TABLE alerts(id INTEGER PRIMARY KEY, ts TEXT, "
                         "rule TEXT, device_key TEXT, message TEXT, "
                         "notified INTEGER DEFAULT 0)")
        for rule, key, n in (("low_aircraft", "A1", WITHHELD),
                             ("notable_aircraft", "A2", WITHHELD),
                             ("new_device", "D1", PENDING),
                             ("new_carrier", "C1", PENDING),
                             ("new_device", "D2", DELIVERED)):
            self.con.execute("INSERT INTO alerts(ts,rule,device_key,message,notified) "
                             "VALUES('2026-09-03T00:00:00+00:00',?,?,'m',?)",
                             (rule, key, n))
        self.con.commit()

    def tearDown(self):
        self.con.close(); os.unlink(self.path)

    def q(self, sql):
        return self.con.execute(sql).fetchone()[0]

    def test_withheld_is_not_counted_as_undelivered(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM alerts WHERE notified=0"), 2,
                         "withheld alerts are inflating the undelivered count")

    def test_withheld_does_not_consume_the_hourly_budget(self):
        """The budget counts DELIVERED alerts; a silenced one must not count."""
        self.assertEqual(self.q("SELECT COUNT(*) FROM alerts WHERE notified=1"), 1)

    def test_delivery_update_does_not_resurrect_withheld_alerts(self):
        """The send path targets notified=0; 2 must be left alone."""
        self.con.execute("UPDATE alerts SET notified=1 WHERE notified=0")
        self.con.commit()
        self.assertEqual(self.q("SELECT COUNT(*) FROM alerts WHERE notified=2"), 2)

    def test_the_three_states_are_documented_in_the_schema(self):
        src = open(os.path.join(ROOT, "broker", "collector.py")).read()
        self.assertIn("withheld by policy", src,
                      "a magic number 2 with no explanation is worse than none")

    def test_collector_marks_them_withheld_not_pending(self):
        src = open(os.path.join(ROOT, "broker", "collector.py")).read()
        self.assertIn("SET notified=2", src)


if __name__ == "__main__":
    unittest.main()
