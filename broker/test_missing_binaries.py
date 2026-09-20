"""A missing decoder must be named once, at startup -- not discovered per lane.

It does not fail loudly on its own. The lane starts, the shell writes "No such
file or directory" into that lane's private log, the process is gone in a few
seconds, and the rotation goes round and does it again. Nothing aggregates it,
so one environment mistake presents as a dozen unrelated lane faults.

That is exactly how it played out: BANDWATCH_TOOLS pointed at a checkout whose
tools/ holds only a README, every voice and decoder lane on BOTH radios died in
~5s, and the sweeps -- which use a packaged rtl_power and never look in tools/
-- held their dwells and looked perfectly healthy. It read as a dead dongle for
half an hour.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker as B  # noqa: E402


def lane(lane_id, prog, *args):
    return {"id": lane_id, "cmd": [prog, *args]}


def test_an_absolute_path_that_does_not_exist_is_reported():
    got = B.missing_binaries([lane("guard_121", "/nope/tools/rtl_airband", "-c", "x")])
    assert got == {"/nope/tools/rtl_airband": ["guard_121"]}


def test_a_real_executable_is_not_reported():
    got = B.missing_binaries([lane("x", "/bin/sh", "-c", "true")])
    assert got == {}


def test_a_program_on_PATH_is_not_reported():
    assert B.missing_binaries([lane("x", "sh")]) == {}


def test_a_bare_name_not_on_PATH_is_reported():
    got = B.missing_binaries([lane("acars", "acarsdec_that_does_not_exist")])
    assert list(got) == ["acarsdec_that_does_not_exist"]


def test_every_lane_sharing_one_missing_program_is_grouped():
    """The whole point: ONE message naming the cause, not one per lane."""
    prog = "/nope/tools/RTLSDR-Airband/build/src/rtl_airband"
    got = B.missing_binaries([
        lane("guard_121", prog, "-c", "a"),
        lane("air_tower_voice", prog, "-c", "b"),
        lane("frs_voice", prog, "-c", "c"),
    ])
    assert got == {prog: ["guard_121", "air_tower_voice", "frs_voice"]}


def test_a_directory_is_not_mistaken_for_a_program(tmp_path):
    """os.access(X_OK) is true for a directory; isfile() is what rules it out."""
    d = tmp_path / "rtl_airband"
    d.mkdir()
    assert list(B.missing_binaries([lane("x", str(d))])) == [str(d)]


def test_a_present_but_non_executable_file_is_reported(tmp_path):
    f = tmp_path / "rtl_power"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)
    assert list(B.missing_binaries([lane("x", str(f))])) == [str(f)]


def test_a_lane_with_no_command_is_skipped():
    assert B.missing_binaries([{"id": "x"}, {"id": "y", "cmd": []}]) == {}


def test_a_healthy_profile_reports_nothing():
    lanes = [lane("a", "/bin/sh"), lane("b", "sh"), lane("c", "/bin/ls")]
    assert B.missing_binaries(lanes) == {}
