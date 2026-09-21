"""Profiles are the only place a lane's dwell can differ by duty cycle.

A profile used to be membership and nothing else: it picked which lanes ran on
which radio, and every lane's dwell came from the catalogue. That is fine until
you want an event profile that actually pins something -- "track this aircraft"
means ADS-B for two hours, not the 300 seconds it gets overnight. Without an
override the event profiles still load, still rotate and still look correct;
they just quietly stop doing the one thing they exist to do.

So a profile's lane list takes either a bare id or {"id": ..., "seconds": ...},
and these tests hold that line in both directions: the override must win where
it is given, and the catalogue must win everywhere else.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bandwatch_config as C  # noqa: E402
import make_profiles as M  # noqa: E402


def doc(profile_lanes):
    return {
        "devices": {"0": {"label": "LOW", "serial": "00000001"}},
        "lanes": {
            "guard_121": {"kind": "voice", "conf": "guard121.conf", "seconds": 240},
            "adsb": {"kind": "script", "script": "lane-adsb.sh",
                     "event_file": "events/adsb.sbs", "seconds": 300},
        },
        "profiles": [{"name": "p", "description": "", "devices": {"0": profile_lanes}}],
    }


def lanes_of(built):
    return {l["id"]: l for l in built[0]["devices"]["0"]["lanes"]}


def test_bare_id_takes_the_catalogue_dwell():
    got = lanes_of(M.build_profiles(doc(["guard_121", "adsb"])))
    assert got["guard_121"]["seconds"] == 240
    assert got["adsb"]["seconds"] == 300


def test_override_wins_for_that_profile_only():
    """The case this exists for: event-track pinning ADS-B for two hours."""
    got = lanes_of(M.build_profiles(doc([{"id": "adsb", "seconds": 7200}])))
    assert got["adsb"]["seconds"] == 7200


def test_override_does_not_leak_into_the_catalogue():
    """A mutated catalogue would corrupt every LATER profile, not this one.

    Worth its own test because the bug it guards is invisible here: build the
    overriding profile first and the damage shows up in the next one.
    """
    d = doc([{"id": "adsb", "seconds": 7200}])
    d["profiles"].append({"name": "q", "description": "", "devices": {"0": ["adsb"]}})
    built = M.build_profiles(d)
    by_name = {p["name"]: p for p in built}
    assert by_name["p"]["devices"]["0"]["lanes"][0]["seconds"] == 7200
    assert by_name["q"]["devices"]["0"]["lanes"][0]["seconds"] == 300
    assert d["lanes"]["adsb"]["seconds"] == 300


def test_enabled_can_be_overridden_off():
    """frs_voice runs everywhere except voice-first, which is a real profile."""
    got = lanes_of(M.build_profiles(doc([{"id": "guard_121", "enabled": False}])))
    assert got["guard_121"]["enabled"] is False
    assert got["guard_121"]["seconds"] == 240      # untouched fields survive


def test_mixed_bare_and_override_entries():
    got = lanes_of(M.build_profiles(doc(["guard_121", {"id": "adsb", "seconds": 60}])))
    assert got["guard_121"]["seconds"] == 240
    assert got["adsb"]["seconds"] == 60


def test_override_of_an_undefined_lane_is_refused():
    with pytest.raises(C.ConfigError) as e:
        M.build_profiles(doc([{"id": "nosuchlane", "seconds": 60}]))
    assert "nosuchlane" in str(e.value)


def test_entry_without_an_id_is_refused():
    """Silently skipping it would drop a lane from the rotation for good."""
    with pytest.raises(C.ConfigError) as e:
        M.build_profiles(doc([{"seconds": 60}]))
    assert "id" in str(e.value)


def test_unknown_override_field_is_refused():
    """A typo must not read as "no override".

    "second": 7200 would otherwise be accepted, ignored, and leave the lane on
    its catalogue dwell -- the exact silent no-op this schema exists to avoid.
    """
    with pytest.raises(C.ConfigError) as e:
        M.build_profiles(doc([{"id": "adsb", "second": 7200}]))
    assert "second" in str(e.value)


def test_override_dwell_must_be_a_positive_int():
    for bad in (0, -5, "7200", 12.5):
        with pytest.raises(C.ConfigError):
            M.build_profiles(doc([{"id": "adsb", "seconds": bad}]))


# ---------------------------------------------------------------- sweeps

def sweep_doc(spec):
    d = doc(["s"])
    d["lanes"]["s"] = dict(spec)
    d["profiles"][0]["devices"]["0"] = ["s"]
    return d


def built_sweep(spec):
    return lanes_of(M.build_profiles(sweep_doc(spec)))["s"]


def test_sweep_goes_through_the_append_safe_wrapper():
    """Not raw rtl_power: it truncates, so a killed pass erased the record."""
    cmd = built_sweep({"kind": "sweep", "range": "225M:400M:25k", "seconds": 150})["cmd"]
    assert cmd[0].endswith("lane-survey.sh"), cmd
    assert "rtl_power" not in cmd[0]


def test_sweep_splits_the_range_into_the_wrapper_arguments():
    cmd = built_sweep({"kind": "sweep", "range": "225M:400M:25k", "seconds": 150})["cmd"]
    assert cmd[1] == "{DEV}"
    assert cmd[2:5] == ["225M", "400M", "25k"]


def test_sweep_exit_time_lands_inside_the_dwell():
    """The whole point of deriving it: a hand-set -e drifts off its slot."""
    for seconds in (30, 40, 60, 90, 150, 600):
        cmd = built_sweep({"kind": "sweep", "range": "88M:108M:100k",
                           "seconds": seconds})["cmd"]
        exitsec = int(cmd[6])
        assert exitsec < seconds or seconds <= 20, (seconds, exitsec)


def test_sweep_gain_is_passed_through():
    """survey_fm runs at 30; 40 overloads the front end on FM broadcast."""
    cmd = built_sweep({"kind": "sweep", "range": "88M:108M:100k",
                       "seconds": 60, "gain": "30"})["cmd"]
    assert cmd[-1] == "30"


def test_sweep_gain_defaults_to_40():
    cmd = built_sweep({"kind": "sweep", "range": "88M:108M:100k", "seconds": 60})["cmd"]
    assert cmd[-1] == "40"


def test_sweep_min_per_hour_can_be_set_to_zero():
    """A sweep over a band that is genuinely quiet must not fault hourly."""
    lane = built_sweep({"kind": "sweep", "range": "902M:928M:12.5k",
                        "seconds": 40, "min_per_hour": 0})
    assert lane["expect_min_events_per_hour"] == 0


def test_sweep_with_a_malformed_range_is_refused():
    with pytest.raises(C.ConfigError) as e:
        built_sweep({"kind": "sweep", "range": "225M-400M", "seconds": 60})
    assert "LOW:HIGH:BINSIZE" in str(e.value)


def test_min_per_hour_can_be_overridden_per_profile():
    """A pinned lane is reasonably expected to produce more than a sliced one."""
    d = doc([{"id": "s", "min_per_hour": 3}])
    d["lanes"]["s"] = {"kind": "sweep", "range": "225M:400M:25k",
                       "seconds": 300, "min_per_hour": 0}
    got = lanes_of(M.build_profiles(d))
    assert got["s"]["expect_min_events_per_hour"] == 3


def test_min_per_hour_override_of_zero_is_honoured():
    """0 is falsy; treating it as unset would restore the strict default."""
    d = doc([{"id": "s", "min_per_hour": 0}])
    d["lanes"]["s"] = {"kind": "sweep", "range": "225M:400M:25k", "seconds": 300}
    got = lanes_of(M.build_profiles(d))
    assert got["s"]["expect_min_events_per_hour"] == 0


# ------------------------------------- one pass must fit inside the slot

def test_a_sweep_whose_pass_fits_keeps_its_configured_integration():
    lane = built_sweep({"kind": "sweep", "range": "162.35M:162.6M:5k",
                        "seconds": 40, "interval": 10})
    assert lane["cmd"][5] == "10"
    assert lane["sweep_hops"] == 1


def test_integration_is_clamped_when_the_pass_cannot_fit():
    """air_survey: 19 MHz is 8 hops; at -i 10 one pass needs 80s in a 60s slot.

    rtl_power honours -e at a PASS BOUNDARY, not mid-pass, so it ran until the
    pass finished, sailed past the dwell and was SIGKILLed -- and a SIGKILL
    mid-USB-transfer is what leaves the interface claimed.
    """
    lane = built_sweep({"kind": "sweep", "range": "118M:137M:25k",
                        "seconds": 60, "interval": 10})
    assert lane["sweep_hops"] == 8
    assert lane["sweep_pass_s"] <= 50          # dwell 60 minus the 10s margin
    assert int(lane["cmd"][5]) < 10


def test_the_clamp_only_ever_lowers_integration():
    """A configured value that already fits must be left alone."""
    lane = built_sweep({"kind": "sweep", "range": "929M:932M:12.5k",
                        "seconds": 40, "interval": 5})
    assert lane["cmd"][5] == "5"


def test_integration_never_clamps_below_one_second():
    """175 MHz in a short slot would otherwise compute a 0s integration."""
    lane = built_sweep({"kind": "sweep", "range": "225M:400M:25k",
                        "seconds": 30, "interval": 10})
    assert int(lane["cmd"][5]) >= 1


def test_every_sweep_pass_fits_its_slot_after_clamping():
    """The property, over the shapes that actually broke."""
    for rng, secs, iv in (("118M:137M:25k", 60, 10),     # air_survey
                          ("88M:108M:100k", 45, 10),     # survey_fm
                          ("902M:928M:12.5k", 40, 12),   # lora915
                          ("225M:400M:25k", 300, 3)):    # milair_survey
        lane = built_sweep({"kind": "sweep", "range": rng,
                            "seconds": secs, "interval": iv})
        assert lane["sweep_pass_s"] <= int(lane["cmd"][6]), (rng, lane)


def test_span_parsing_handles_the_rtl_power_spellings():
    assert M._span_mhz("118M", "137M") == 19.0
    assert M._span_mhz("902M", "928M") == 26.0
    assert round(M._span_mhz("162.35M", "162.6M"), 3) == 0.25
    assert M._span_mhz("1G", "1.1G") == 100.0
