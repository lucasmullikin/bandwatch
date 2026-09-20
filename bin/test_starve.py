#!/usr/bin/env python3
"""Force the lane-starvation detector through every branch.

The rule this project keeps relearning: a guard nobody has watched DECLINE to
fire is not a guard. So every case below includes the negative control -- the
situation where firing would be wrong.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "wd", os.path.join(ROOT, "bin", "bw-watchdog.py"))
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class TestStarvation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "profiles"))
        os.makedirs(os.path.join(self.tmp, "var"))
        self.now = datetime.now(timezone.utc)
        # a 3-lane device with a 30-minute cycle
        json.dump({"devices": {"1": {"lanes": [
            {"id": "a", "seconds": 600},
            {"id": "b", "seconds": 600},
            {"id": "c", "seconds": 600},
        ]}}}, open(os.path.join(self.tmp, "profiles", "p.json"), "w"))
        self._root, self._var = wd.ROOT, wd.VAR
        wd.ROOT = self.tmp
        wd.VAR = os.path.join(self.tmp, "var")

    def tearDown(self):
        wd.ROOT, wd.VAR = self._root, self._var
        shutil.rmtree(self.tmp)

    def state(self, up_minutes, lanes):
        json.dump({
            "profile": "p",
            "started_at": iso(self.now - timedelta(minutes=up_minutes)),
            "devices": {"1": {"lanes": lanes}},
        }, open(os.path.join(self.tmp, "var", "state.json"), "w"))

    def test_fires_on_a_lane_that_never_ran(self):
        self.state(200, {"a": {"last_run_at": iso(self.now)},
                         "b": {"last_run_at": iso(self.now)}})
        f = wd.starved_lanes()
        self.assertTrue(any("lane c has NEVER run" in r for _, r in f), f)

    def test_declines_before_the_first_cycle_is_even_due(self):
        # NEGATIVE CONTROL: 10 minutes after boot, lane c has not had its turn
        # yet. Reporting it would fire on every single restart.
        self.state(10, {"a": {"last_run_at": iso(self.now)}})
        self.assertEqual(wd.starved_lanes(), [])

    def test_declines_when_every_lane_ran_recently(self):
        # NEGATIVE CONTROL: healthy rotation must be silent
        self.state(500, {k: {"last_run_at": iso(self.now - timedelta(minutes=5))}
                         for k in "abc"})
        self.assertEqual(wd.starved_lanes(), [])

    def test_fires_on_a_lane_that_ran_long_ago(self):
        self.state(500, {"a": {"last_run_at": iso(self.now)},
                         "b": {"last_run_at": iso(self.now)},
                         "c": {"last_run_at": iso(self.now - timedelta(hours=5))}})
        f = wd.starved_lanes()
        self.assertTrue(any("lane c last ran 300m ago" in r for _, r in f), f)

    def test_boundary_just_inside_is_silent(self):
        # 89 minutes < 3 x 30-minute cycle: not yet starved
        self.state(500, {k: {"last_run_at": iso(self.now - timedelta(minutes=89))}
                         for k in "abc"})
        self.assertEqual(wd.starved_lanes(), [])

    def test_boundary_just_outside_fires(self):
        self.state(500, {k: {"last_run_at": iso(self.now - timedelta(minutes=91))}
                         for k in "abc"})
        self.assertEqual(len(wd.starved_lanes()), 3)

    def test_disabled_lanes_are_not_expected_to_run(self):
        # NEGATIVE CONTROL: milair_voice is enabled:false in the real profile.
        # Flagging a lane the operator switched off would be noise.
        json.dump({"devices": {"1": {"lanes": [
            {"id": "a", "seconds": 600},
            {"id": "off", "seconds": 600, "enabled": False},
        ]}}}, open(os.path.join(self.tmp, "profiles", "p.json"), "w"))
        self.state(500, {"a": {"last_run_at": iso(self.now)}})
        self.assertEqual(wd.starved_lanes(), [])

    def test_missing_profile_is_not_a_fault(self):
        json.dump({"profile": "gone", "started_at": iso(self.now),
                   "devices": {}},
                  open(os.path.join(self.tmp, "var", "state.json"), "w"))
        self.assertEqual(wd.starved_lanes(), [])

    def test_malformed_timestamp_counts_as_never_run(self):
        self.state(500, {"a": {"last_run_at": "garbage"},
                         "b": {"last_run_at": iso(self.now)},
                         "c": {"last_run_at": iso(self.now)}})
        f = wd.starved_lanes()
        self.assertTrue(any("lane a has NEVER run" in r for _, r in f), f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
