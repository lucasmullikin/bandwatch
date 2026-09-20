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
