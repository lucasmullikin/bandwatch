"""A radio that will not open must be parked, not fed lane after lane.

Measured on a live station: one dongle's USB interface hung, and the rotation
kept handing it to the next lane every few seconds. 926 `usb_claim_interface
error -3` lines accumulated across the logs, the radio was hammered for hours,
and the only outward sign was ordinary per-lane faults -- so it read as twelve
lane problems rather than one radio problem.

A wedged RTL-SDR ENUMERATES PERFECTLY. It appears in rtl_test's device list, it
has the right serial, and every attempt to open it fails. Presence proves
nothing; only an open attempt does.

The detector is deliberately two-stage, and both stages matter:

  * a STREAK of early exits, which a quiet band cannot produce -- a silent band
    still holds its dwell, it just records nothing
  * then a DIRECT open probe, so the verdict is never statistical

Condemning a radio on silence alone is the failure mode to avoid: it would park
a working radio pointed at a quiet band, which is worse than the bug.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker as B  # noqa: E402


# ------------------------------------------------------- the open probe

def test_a_clean_rtl_test_reads_as_openable(monkeypatch):
    monkeypatch.setattr(B, "_rtl_test_output",
                        lambda dev, timeout: "Found 2 device(s)\nTuner gain...")
    assert B.device_opens(0) is True


def test_a_claim_error_reads_as_wedged(monkeypatch):
    monkeypatch.setattr(B, "_rtl_test_output", lambda dev, timeout: (
        "Found 2 device(s):\n  0: Realtek, SN: 00000002\n"
        "usb_claim_interface error -3\nFailed to open rtlsdr device #0."))
    assert B.device_opens(0) is False


def test_failed_to_open_reads_as_wedged(monkeypatch):
    monkeypatch.setattr(B, "_rtl_test_output",
                        lambda dev, timeout: "Failed to open rtlsdr device #1.")
    assert B.device_opens(1) is False


def test_no_devices_at_all_reads_as_wedged(monkeypatch):
    monkeypatch.setattr(B, "_rtl_test_output",
                        lambda dev, timeout: "No supported devices found.")
    assert B.device_opens(0) is False


def test_a_probe_that_times_out_means_it_OPENED(monkeypatch):
    """rtl_test runs until we stop it. Being cut off is success, not failure."""
    def boom(dev, timeout):
        raise B.subprocess.TimeoutExpired("rtl_test", timeout)
    monkeypatch.setattr(B, "_rtl_test_output", boom)
    assert B.device_opens(0) is True


def test_a_probe_that_cannot_run_never_condemns_the_radio(monkeypatch):
    """rtl_test missing, or PATH wrong, is ignorance -- not a dead radio.

    The house rule: absence of evidence is not evidence. Returning False here
    would park a perfectly good radio because a binary moved.
    """
    def boom(dev, timeout):
        raise OSError("rtl_test: command not found")
    monkeypatch.setattr(B, "_rtl_test_output", boom)
    assert B.device_opens(0) is True


# --------------------------------------------------------- the streak

def test_one_early_exit_does_not_probe():
    w = B.WedgeWatch(dev=1)
    assert w.record(early=True) is False


def test_a_streak_short_of_the_threshold_does_not_probe():
    w = B.WedgeWatch(dev=1)
    for _ in range(B.WEDGE_STREAK - 1):
        assert w.record(early=True) is False


def test_the_streak_triggers_a_probe_at_the_threshold():
    w = B.WedgeWatch(dev=1)
    fired = [w.record(early=True) for _ in range(B.WEDGE_STREAK)]
    assert fired[-1] is True


def test_a_normal_lane_resets_the_streak():
    """A radio that worked once is not wedged, whatever came before."""
    w = B.WedgeWatch(dev=1)
    for _ in range(B.WEDGE_STREAK - 1):
        w.record(early=True)
    w.record(early=False)
    assert w.record(early=True) is False


def test_a_quiet_band_never_trips_it():
    """Lanes that run their full dwell and record nothing are not early exits.

    This is the false positive that would matter: parking a working radio
    pointed at a silent band is worse than the bug being fixed.
    """
    w = B.WedgeWatch(dev=1)
    for _ in range(50):
        assert w.record(early=False) is False


# --------------------------------------------------------- parking

def test_parking_records_why_and_when():
    w = B.WedgeWatch(dev=1)
    w.park()
    assert w.parked is True
    assert w.parked_at is not None


def test_a_parked_radio_is_not_retried_immediately():
    w = B.WedgeWatch(dev=1)
    w.park()
    assert w.due_for_retry(now=w.parked_at + 1) is False


def test_a_parked_radio_is_retried_after_the_interval():
    w = B.WedgeWatch(dev=1)
    w.park()
    assert w.due_for_retry(now=w.parked_at + B.WEDGE_RETRY_S + 1) is True


def test_unparking_clears_the_streak_too():
    """Otherwise the first early exit after recovery re-parks it instantly."""
    w = B.WedgeWatch(dev=1)
    for _ in range(B.WEDGE_STREAK):
        w.record(early=True)
    w.park()
    w.unpark()
    assert w.parked is False
    assert w.record(early=True) is False
