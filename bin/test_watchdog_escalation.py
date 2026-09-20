"""Negative+positive control for the watchdog's escalation and dead-radio logic.

Stubs repair() and notify() so nothing real happens, and drives the SAME main()
the supervisor calls. Scratch STATE file, so live state is untouched.

Written as a script ending in sys.exit(), which aborted pytest during
collection: the harness reported the file as contributing zero tests, so none
of this ran under it. Its own closing comment records the earlier version of
the same bug -- an exit in the middle meant every check after it never ran
while the file still reported success.
"""
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

FAULT = [("1", "lane adsb exits early on every run")]


def load_wd(tmp):
    spec = importlib.util.spec_from_file_location(
        "wd", os.path.join(HERE, "bw-watchdog.py"))
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    wd.STATE = os.path.join(tmp, "watchdog.json")
    wd.MUTE_LOG = os.path.join(tmp, "muted.log")
    wd.LOG = os.path.join(tmp, "wd.log")
    return wd


# ------------------------------------------------ consecutive failed repairs

@pytest.fixture(scope="module")
def escalation(tmp_path_factory):
    """Seven passes: five faulting, one clean, then faulting again."""
    wd = load_wd(str(tmp_path_factory.mktemp("wd")))
    repairs, notifies = [], []
    wd.repair = lambda reason: repairs.append(reason)
    wd.notify = lambda text: notifies.append(text)
    wd.heartbeat = lambda faults: None

    def run(faults):
        wd.check = lambda: list(faults)
        before_r, before_n = len(repairs), len(notifies)
        wd.main()
        return len(repairs) - before_r, len(notifies) - before_n

    return [run(FAULT), run(FAULT), run(FAULT), run(FAULT), run(FAULT),
            run([]), run(FAULT)]


def test_first_two_passes_still_attempt_a_repair(escalation):
    assert escalation[0][0] == 1 and escalation[1][0] == 1


def test_a_transient_fault_self_heals_silently(escalation):
    """Pass 1 must not page: most faults are gone by the next check."""
    assert escalation[0][1] == 0


def test_second_pass_pages_via_the_repeat_rule(escalation):
    assert escalation[1][1] == 1


def test_third_pass_stops_restarting_the_stack(escalation):
    """The escalation: three failed repairs means repairing is not the answer."""
    assert escalation[2][0] == 0


def test_third_pass_pages_exactly_once(escalation):
    assert escalation[2][1] == 1


def test_later_passes_never_restart_again(escalation):
    assert escalation[3][0] == 0 and escalation[4][0] == 0


def test_later_passes_do_not_re_page(escalation):
    """A muted fault must not become alert spam."""
    assert escalation[3][1] == 0 and escalation[4][1] == 0


def test_a_clean_pass_resets_the_streak_and_repair_resumes(escalation):
    assert escalation[6][0] == 1


# ------------------------------------------------------------- dead radios

def _state(devs):
    return {"devices": {d: {"lanes": {k: {"runs": r, "events_total": e}
                                      for k, (r, e) in sp.items()}}
                        for d, sp in devs.items()}}


# Every positive has its negative control, because the failure that matters
# here is a check that cries "dead radio" at a quiet band -- that trains the
# operator to ignore the one message meaning the hardware is gone.
DEAD_CASES = [
    ("the real incident: one radio silent across 4 lanes, the other producing",
     {"0": {"acars": (3, 116), "air_survey": (3, 40)},
      "1": {"adsb": (3, 0), "ism433": (3, 0),
            "p25_survey": (3, 0), "survey_fm": (3, 0)}},
     ["1"]),
    ("both radios silent is a quiet night, not a dead radio",
     {"0": {"a": (3, 0), "b": (3, 0)}, "1": {"c": (3, 0), "d": (3, 0)}},
     []),
    ("both producing",
     {"0": {"a": (3, 5), "b": (3, 1)}, "1": {"c": (3, 2), "d": (3, 9)}},
     []),
    ("a lane that has not had a fair turn is not evidence",
     {"0": {"a": (3, 5), "b": (3, 1)}, "1": {"c": (1, 0), "d": (1, 0)}},
     []),
    ("a single judged lane is not enough to condemn a radio",
     {"0": {"a": (3, 5), "b": (3, 1)}, "1": {"c": (3, 0)}},
     []),
    ("one producing lane among many clears the radio",
     {"0": {"a": (3, 5), "b": (3, 1)},
      "1": {"c": (3, 0), "d": (3, 0), "e": (3, 2)}},
     []),
    ("a single-radio station must never self-condemn",
     {"0": {"a": (3, 0), "b": (3, 0)}},
     []),
]


@pytest.mark.parametrize("label,devs,expected",
                         DEAD_CASES, ids=[c[0][:48] for c in DEAD_CASES])
def test_dead_radio_detection(tmp_path, label, devs, expected):
    wd = load_wd(str(tmp_path))
    got = sorted(d for d, _ in wd.dead_radios(_state(devs)))
    assert got == sorted(expected), label


# --------------------------------------------- a wedged radio is not repairable

@pytest.fixture(scope="module")
def wedged(tmp_path_factory):
    """The broker parked a radio. A restart cannot clear that."""
    wd = load_wd(str(tmp_path_factory.mktemp("wedged")))
    repairs, notifies = [], []
    wd.repair = lambda reason: repairs.append(reason)
    wd.notify = lambda text: notifies.append(text)
    wd.heartbeat = lambda faults: None
    wd.check = lambda: [("1", "WEDGED, NEEDS A PHYSICAL REPLUG: radio does not "
                              "open (usb claim refused) (since 2026-09-20T23:00)")]
    wd.main()
    return {"repairs": repairs, "notifies": notifies}


def test_a_wedged_radio_never_triggers_a_stack_restart(wedged):
    """The reset tool already proved re-enumeration does not clear it."""
    assert wedged["repairs"] == []


def test_a_wedged_radio_notifies_once(wedged):
    assert len(wedged["notifies"]) == 1


def test_the_notification_says_what_to_actually_do(wedged):
    """Not 'a fault occurred' -- the one action that works."""
    text = wedged["notifies"][0]
    assert "REPLUG" in text.upper()
    assert "unplug" in text.lower()


def test_the_notification_says_the_other_radio_is_still_running(wedged):
    """So it reads as one radio down, not the station down."""
    assert "other radio" in wedged["notifies"][0].lower()
