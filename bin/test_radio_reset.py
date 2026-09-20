#!/usr/bin/env python3
"""The recovery tool's honesty, which is the only thing that makes it useful.

A radio in use by a running lane refuses a claim in exactly the same way a
wedged one does. Reporting the first as a fault would send someone resetting a
healthy dongle in the middle of a recording; reporting the second as fine would
leave a dead radio dead. The distinction is the tool.
"""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rr", os.path.join(HERE, "bw-radio-reset.py"))
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


class TestDescribe(unittest.TestCase):
    HEALTHY = {"open": True, "kernel": 0, "claim": 0}
    REFUSED = {"open": True, "kernel": 1, "claim": -3}
    SHUT = {"open": False, "kernel": None, "claim": None}

    def test_claimable_is_healthy(self):
        self.assertIn("healthy", rr.describe(self.HEALTHY))

    def test_claimable_stays_healthy_even_while_a_lane_runs(self):
        """POSITIVE CONTROL: busy must not downgrade a radio that IS claimable."""
        self.assertIn("healthy", rr.describe(self.HEALTHY, busy=True))

    def test_refused_while_a_lane_runs_is_BUSY_not_a_fault(self):
        out = rr.describe(self.REFUSED, busy=True)
        self.assertIn("NOT a fault", out)
        self.assertNotIn("wedged", out)

    def test_refused_with_NO_lane_running_is_wedged(self):
        """NEGATIVE CONTROL. Without this the tool could call everything busy
        and never report a real fault."""
        out = rr.describe(self.REFUSED, busy=False)
        self.assertIn("wedged", out)
        self.assertNotIn("NOT a fault", out)

    def test_unopenable_is_reported_distinctly(self):
        self.assertIn("cannot even be opened", rr.describe(self.SHUT))


class TestLaneDetection(unittest.TestCase):
    def test_does_not_INVOKE_pgrep(self):
        """pgrep excludes itself but not its siblings, so two concurrent pgreps
        match each OTHER and a busy device reads as free forever. That bug cost
        this project days; the tool must not reintroduce it.

        Asserted against CODE, not prose: the first version of this test
        searched the whole file for the word and failed on the comment
        explaining why pgrep is avoided. A test that cannot tell an
        explanation from an invocation is a test that will be deleted.
        """
        src = open(os.path.join(HERE, "bw-radio-reset.py")).read()
        code = "\n".join(l.split("#")[0] for l in src.splitlines()
                         if not l.strip().startswith("#"))
        for line in code.splitlines():
            self.assertNotIn('"pgrep"', line)
            self.assertNotIn("'pgrep'", line)
        self.assertIn('"ps", "-axo", "command="', src,
                      "lane detection must scan ps output explicitly")

    def test_returns_a_bool_and_never_raises(self):
        self.assertIsInstance(rr.a_lane_is_running(), bool)


class TestSafety(unittest.TestCase):
    def test_hold_is_bounded(self):
        """A hold that does not expire parks a radio forever if this crashes."""
        self.assertGreater(rr.HOLD_SECONDS, 0)
        self.assertLessEqual(rr.HOLD_SECONDS, 120)

    def test_config_is_not_imported_at_module_scope(self):
        """A recovery tool must run on a machine whose config is broken --
        that is precisely when someone reaches for it."""
        src = open(os.path.join(HERE, "bw-radio-reset.py")).read()
        head = src.split("def ", 1)[0]
        self.assertNotIn("import bandwatch_config", head,
                         "config is imported at module scope; --list would die "
                         "on a machine with no working config")


if __name__ == "__main__":
    unittest.main()
