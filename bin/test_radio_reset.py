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


class TestCodexFindings(unittest.TestCase):
    """Regressions for what an outside review found in the first version.

    Every one of these was a real defect that the original tests missed,
    because they exercised the reporting logic and never the libusb or
    filesystem plumbing underneath it.
    """

    SRC = open(os.path.join(HERE, "bw-radio-reset.py")).read()

    def test_device_pointers_are_referenced_before_the_list_is_freed(self):
        """libusb_free_device_list(lst, 1) unrefs every device in the list.

        A list comprehension over the generator exhausts it, runs the finally,
        and leaves every pointer dangling -- a use-after-free that does not
        crash reliably, so it survives testing and fails later.
        """
        self.assertIn("libusb_ref_device", self.SRC)
        self.assertIn("libusb_unref_device", self.SRC)
        self.assertIn("def dongles(", self.SRC,
                      "the refcounted accessor is gone; callers would hold "
                      "freed device pointers again")

    def test_main_does_not_iterate_each_dongle_directly(self):
        """each_dongle yields REFERENCED devices; only dongles() unrefs them."""
        body = self.SRC.split("def main(", 1)[1]
        self.assertNotIn("each_dongle(lib, ctx)", body,
                         "main iterates the raw generator again, so the refs "
                         "it takes are never released")

    def test_the_after_probe_refinds_by_serial(self):
        """A reset can re-enumerate the device, invalidating the pointer.

        Probing the old one gives a meaningless verdict at best.
        """
        after = self.SRC.split("rc = reset(lib, dev)", 1)[1][:900]
        self.assertNotIn("probe(lib, dev)", after,
                         "the post-reset probe still uses the pre-reset pointer")
        self.assertIn("dongles(lib, ctx)", after)

    def test_pause_path_is_shared_with_the_broker(self):
        """Two spellings of the flag is a hold that holds nothing.

        The first version wrote bandwatch_pause_dev* while the broker watched
        sdr_pause_dev*, so the tool would reset a radio out from under a
        running lane and nothing would report an error.
        """
        self.assertIn("from bandwatch_config import PAUSE_FMT", self.SRC)
        broker = open(os.path.join(os.path.dirname(HERE), "broker", "broker.py")).read()
        self.assertIn("from bandwatch_config import PAUSE_FMT", broker,
                      "the broker defines its own PAUSE_FMT again")

    def test_flag_is_created_without_following_symlinks(self):
        """The path is predictable and /tmp is world-writable."""
        self.assertIn("O_NOFOLLOW", self.SRC)
        self.assertIn("O_EXCL", self.SRC)

    def test_release_only_removes_our_own_flag(self):
        """Two concurrent resets must not delete each other's hold."""
        rel = self.SRC.split("\ndef release(slot, pid):", 1)[1][:600]
        self.assertIn("== str(pid)", rel,
                      "release() unlinks the flag without checking it is ours")

    def test_hold_happens_inside_the_cleanup_try(self):
        """An exception during the yield wait must not leak a flag."""
        body = self.SRC.split("def main(", 1)[1]
        try_at = body.index("        try:\n            if not a.no_hold:")
        self.assertGreater(try_at, 0,
                           "hold() moved back outside the try/finally")
