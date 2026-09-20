"""A lane's progress counter, and the empty file that used to fool it.

Measured on a live station 2026-09-20: one radio's USB interface had been
stuck claimed for two days. Every open failed, so rtl_power produced nothing --
but it still CREATED and TRUNCATED its output CSV on each run. The mtime moved,
the size stayed at zero, and all four sweep lanes on that radio reported
themselves healthy for 48 hours while the event store recorded nothing from
them.

A touched file and a written file were the same signal. These pin the
difference, in both directions.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sink = os.path.join(self.tmp, "survey.csv")

    def lane(self, mode=None):
        l = {"id": "t", "event_file": self.sink}
        if mode:
            l["count_mode"] = mode
        return l

    def write(self, text):
        with open(self.sink, "w") as fh:
            fh.write(text)

    def touch_empty(self):
        """Exactly what rtl_power does when the radio will not open: create and
        truncate the file, write nothing."""
        with open(self.sink, "w"):
            pass
        # push mtime forward so this is unambiguously a NEW rewrite
        t = time.time() + 5
        os.utime(self.sink, (t, t))


class TestMtimeMode(Base):
    def test_a_written_file_counts_as_progress(self):
        """POSITIVE CONTROL. Without this the fix could just be 'always zero'."""
        self.write("1,2,3\n")
        first = broker.count_events(self.lane("mtime"))
        self.assertGreater(first, 0)
        time.sleep(0.01)
        self.write("4,5,6\n")
        t = time.time() + 5
        os.utime(self.sink, (t, t))
        self.assertGreater(broker.count_events(self.lane("mtime")) - first, 0,
                           "a rewritten NON-EMPTY sweep file must read as progress")

    def test_an_EMPTY_rewrite_is_not_progress(self):
        """The bug. rtl_power truncating to nothing must not read as output."""
        self.touch_empty()
        self.assertEqual(broker.count_events(self.lane("mtime")), 0,
                         "an empty file was counted as progress -- this is the "
                         "failure that hid a dead radio for two days")

    def test_going_from_written_to_empty_never_reads_as_progress(self):
        """The exact sequence on the live station: it worked, then it did not.

        The delta must not come out positive when a lane stops producing and
        the file is merely being touched.
        """
        self.write("real,data\n")
        before = broker.count_events(self.lane("mtime"))
        self.assertGreater(before, 0)
        self.touch_empty()
        after = broker.count_events(self.lane("mtime"))
        gained = after - before
        # the caller floors a negative delta at zero; what matters is that it
        # is NOT positive
        self.assertLessEqual(gained, 0,
                             "a lane that stopped producing still reported a gain")

    def test_repeated_empty_rewrites_never_accumulate(self):
        """Two days of this is what actually happened."""
        prev = broker.count_events(self.lane("mtime"))
        for _ in range(5):
            self.touch_empty()
            cur = broker.count_events(self.lane("mtime"))
            self.assertLessEqual(cur - prev, 0, "empty rewrites accumulated progress")
            prev = cur

    def test_a_missing_file_is_zero(self):
        self.assertEqual(broker.count_events(self.lane("mtime")), 0)


class TestLineMode(Base):
    """The default counter, unchanged -- asserted so the fix did not touch it."""

    def test_counts_lines(self):
        self.write("a\nb\nc\n")
        self.assertEqual(broker.count_events(self.lane()), 3)

    def test_appending_is_progress(self):
        self.write("a\n")
        before = broker.count_events(self.lane())
        with open(self.sink, "a") as fh:
            fh.write("b\n")
        self.assertEqual(broker.count_events(self.lane()) - before, 1)

    def test_empty_file_is_zero_lines(self):
        self.write("")
        self.assertEqual(broker.count_events(self.lane()), 0)

    def test_missing_file_is_zero(self):
        self.assertEqual(broker.count_events(self.lane()), 0)



class TestLaneLabelling(unittest.TestCase):
    """Labels follow the LOGICAL slot; commands follow the PHYSICAL index.

    librtlsdr's ordering is not stable across a replug -- that is the whole
    reason lanes are pinned by serial. Measured 2026-09-20: after replugging,
    physical index 0 became the high-band radio and physical 1 the low-band
    one. run_lane() was given only the physical index and used it for the log
    tag AND the log filename, so every line and every file named the wrong
    radio. Anyone diagnosing a fault would have been sent to the other dongle.
    """

    def test_signature_takes_a_separate_logical_slot(self):
        import inspect
        sig = inspect.signature(broker.run_lane)
        self.assertIn("slot", sig.parameters,
                      "run_lane lost its logical-slot argument; labels would "
                      "follow the physical index again")

    def test_slot_defaults_to_the_physical_index(self):
        """The common case -- they agree -- must not need the argument."""
        import inspect
        self.assertIsNone(inspect.signature(broker.run_lane).parameters["slot"].default)

    def test_the_caller_passes_the_logical_slot(self):
        """Read the source: the wiring is the bug, and no unit test reaches it.

        run_lane is called inside the device thread, which needs real hardware.
        Asserting on the call site is the only way to pin that `slot=dev` is
        actually passed rather than quietly dropped in a refactor.
        """
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "broker.py")).read()
        self.assertIn("run_lane(phys, lane, dstate, slot=dev)", src,
                      "the device loop no longer passes the logical slot, so "
                      "log tags and filenames follow the physical index again")

    def test_command_substitution_still_uses_the_PHYSICAL_index(self):
        """NEGATIVE CONTROL for the fix.

        Swapping labels to the logical slot must NOT swap the {DEV} the decoder
        is given -- that would open the wrong radio, which is worse than a
        wrong label and shows up only as bad reception.
        """
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "broker.py")).read()
        self.assertIn('cmd = [c.replace("{DEV}", str(dev)) for c in lane["cmd"]]', src,
                      "{DEV} must still be the physical index")
        self.assertNotIn('c.replace("{DEV}", str(slot))', src,
                         "{DEV} was switched to the logical slot -- this opens "
                         "the WRONG radio after a replug")


# The entry point stays at the BOTTOM. Placed mid-file it runs before the
# classes below are defined, so `python3 test_counting.py` silently skips them
# while still reporting success -- the same trap that had been sitting in
# bin/test_watchdog_escalation.py.
if __name__ == "__main__":
    unittest.main()
