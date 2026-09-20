"""A self-terminating lane must be allowed to finish, not SIGKILLed.

rtl_power stops by itself when its -e elapses, and it also CATCHES SIGTERM to
finish the current hop first ("Signal caught, finishing scan pass"). That
routinely outlasted the broker's SIGTERM wait, so the broker escalated to
SIGKILL -- which is uncatchable, so libusb never released the USB interface.

The numbers, across this station's entire history:

    1,923 SIGTERM escalations in 19,122 lane starts   (10.1%)
    96% of them sweeps: pager_survey, survey_fm, lora915, noaa_wx,
                        milair_survey, p25_survey, air_survey
    claim errors on the NEXT open: ~10% of lane starts

Two independent 10% figures on the same set of lanes is why the grace exists.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker as B  # noqa: E402

SWEEP = {"id": "lora915", "self_terminating": True, "seconds": 40}
VOICE = {"id": "guard_121", "seconds": 180}


def test_a_sweep_is_given_time_to_finish_itself():
    assert B.exit_grace(SWEEP, yielding=False) == B.SELF_TERM_GRACE_S


def test_a_normal_lane_gets_no_grace():
    """A voice lane is not exiting on its own; waiting for it is dead time."""
    assert B.exit_grace(VOICE, yielding=False) == 0


def test_a_displaced_lane_gets_no_grace_even_if_self_terminating():
    """Someone clicked listen and is waiting on audio. They win."""
    assert B.exit_grace(SWEEP, yielding=True) == 0


def test_a_displaced_normal_lane_also_gets_no_grace():
    assert B.exit_grace(VOICE, yielding=True) == 0


def test_the_grace_is_long_enough_to_outlast_a_scan_pass():
    """Shorter than the SIGTERM wait would make it decorative.

    The point is to avoid the escalation entirely, so it has to exceed the
    10s the broker would otherwise wait before SIGKILL.
    """
    assert B.SELF_TERM_GRACE_S > 10


def test_a_lane_with_the_flag_absent_is_treated_as_normal():
    """Absent must mean 'not self-terminating', never a crash or a grace."""
    assert B.exit_grace({"id": "x", "seconds": 60}, yielding=False) == 0
