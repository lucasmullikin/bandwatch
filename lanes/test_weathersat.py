from datetime import datetime, timedelta, timezone

import pytest

import weathersat as ws


# Independently valid TLE lines (checksums computed by hand with the mod-10
# algorithm, not by calling the code under test) used across several tests.
ISS_NAME = "ISS (ZARYA)"
ISS_L1 = "1 25544U 98067A   24001.50000000  .00002182  00000-0  49422-4 0  9990"
ISS_L2 = "2 25544  51.6431  95.3699 0004531  61.0353 106.2823 15.48957534313172"

SAT2_NAME = "NOAA-TEST"
SAT2_L1 = "1 33591U 09005A   24001.50000000  .00000123  00000-0  12345-3 0  9991"
SAT2_L2 = "2 33591  99.1234 123.4567 0013579  45.6789 314.5678 14.12345678123453"


def make_tle_text(*records):
    lines = []
    for name, l1, l2 in records:
        lines += [name, l1, l2]
    return "\n".join(lines) + "\n"


# --- parse_tle -------------------------------------------------------------

def test_parse_tle_valid_multi_satellite():
    text = make_tle_text((ISS_NAME, ISS_L1, ISS_L2), (SAT2_NAME, SAT2_L1, SAT2_L2))
    result = ws.parse_tle(text)
    assert result == {ISS_NAME: (ISS_L1, ISS_L2), SAT2_NAME: (SAT2_L1, SAT2_L2)}


def test_parse_tle_corrupted_checksum_is_rejected():
    """Negative control: flip the checksum digit and confirm the satellite is
    dropped, not silently accepted with a wrong pass time downstream."""
    bad_l1 = ISS_L1[:-1] + str((int(ISS_L1[-1]) + 1) % 10)
    assert bad_l1 != ISS_L1
    text = make_tle_text((ISS_NAME, bad_l1, ISS_L2))
    result = ws.parse_tle(text)
    assert result == {}, "corrupted checksum must be rejected, not parsed"


def test_parse_tle_corrupted_entry_does_not_block_valid_entries():
    bad_l2 = SAT2_L2[:-1] + str((int(SAT2_L2[-1]) + 1) % 10)
    text = make_tle_text((ISS_NAME, ISS_L1, ISS_L2), (SAT2_NAME, SAT2_L1, bad_l2))
    result = ws.parse_tle(text)
    assert ISS_NAME in result
    assert SAT2_NAME not in result


@pytest.mark.parametrize("text", ["", "   \n\n  ", "garbage\nlines\nthat are not a tle\n", None])
def test_parse_tle_malformed_or_empty_returns_empty_dict(text):
    """Negative control: malformed/empty input must return {} rather than raise."""
    assert ws.parse_tle(text) == {}


def test_tle_line_checksum_matches_hand_computation():
    # Sanity-check the checksum helper directly against a hand count.
    # "1 2-3" -> digits 1,2,3 plus '-' as 1 => 7
    assert ws._tle_line_checksum("1 2-3" + "0" * 63) == 7


# --- pass_quality ------------------------------------------------------

def test_pass_quality_low_elevation_is_unusable():
    """Negative control: a 5-degree max-elevation pass must classify as
    unusable, never 'good'."""
    label, reason = ws.pass_quality(5)
    assert label == "unusable"
    assert reason


@pytest.mark.parametrize(
    "elev,expected",
    [(0, "unusable"), (19.9, "unusable"), (20, "poor"), (29.9, "poor"),
     (30, "good"), (59.9, "good"), (60, "excellent"), (90, "excellent")],
)
def test_pass_quality_boundaries(elev, expected):
    label, _ = ws.pass_quality(elev)
    assert label == expected


@pytest.mark.parametrize("bad", [-1, 91, 200])
def test_pass_quality_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        ws.pass_quality(bad)


# --- load_passes -------------------------------------------------------

def _iso(dt):
    return dt.isoformat()


def test_load_passes_normalizes_valid_entries():
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=12)
    entries = [
        {
            "satellite": "NOAA-19",
            "freq_mhz": 137.1000,
            "start": _iso(start),
            "end": _iso(end),
            "max_elevation_deg": 45,
        }
    ]
    passes = ws.load_passes(entries)
    assert len(passes) == 1
    p = passes[0]
    assert p["satellite"] == "NOAA-19"
    assert p["device"] == "0"
    assert p["start"] == start
    assert p["end"] == end


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"freq_mhz": 137.1, "start": "2026-09-01T00:00:00+00:00", "end": "2026-09-01T00:10:00+00:00", "max_elevation_deg": 10},
        {"satellite": "X", "freq_mhz": 137.1, "start": "2026-09-01T00:10:00+00:00", "end": "2026-09-01T00:00:00+00:00", "max_elevation_deg": 10},
        {"satellite": "X", "freq_mhz": 137.1, "start": "2026-09-01T00:00:00+00:00", "end": "2026-09-01T00:10:00+00:00", "max_elevation_deg": 150},
    ],
)
def test_load_passes_rejects_malformed_entries(bad_entry):
    with pytest.raises(ValueError):
        ws.load_passes([bad_entry])


# --- schedule_conflicts --------------------------------------------------

def test_schedule_conflicts_no_conflict_when_device_absent():
    """Negative control: a pass targeting a device that isn't in the profile
    must report no conflicts, not crash."""
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    passes = [{
        "satellite": "NOAA-19", "start": start, "end": start + timedelta(minutes=10),
        "device": "1",
    }]
    profile = {"devices": {"0": {"lanes": [{"id": "voice", "seconds": 60, "enabled": True}]}}}
    result = ws.schedule_conflicts(passes, profile)
    assert len(result) == 1
    assert result[0]["displaced_lanes"] == []
    assert result[0]["displaced_seconds"] == 0.0


def test_schedule_conflicts_no_conflict_when_all_lanes_disabled():
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    passes = [{"satellite": "NOAA-19", "start": start, "end": start + timedelta(minutes=10), "device": "0"}]
    profile = {"devices": {"0": {"lanes": [{"id": "voice", "seconds": 60, "enabled": False}]}}}
    result = ws.schedule_conflicts(passes, profile)
    assert result[0]["displaced_lanes"] == []
    assert result[0]["displaced_seconds"] == 0.0


def test_schedule_conflicts_exact_displacement_with_known_cursor():
    """Deterministic positive case: with an explicit cursor, verify exact
    lane-by-lane displacement, not just that something non-crashing happened."""
    as_of = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Two lanes, 60s each, rotating a-b-a-b-...; cursor says lane index 0
    # ("a") just started (elapsed 0) at as_of.
    profile = {
        "devices": {
            "0": {
                "lanes": [
                    {"id": "a", "seconds": 60, "enabled": True},
                    {"id": "b", "seconds": 60, "enabled": True},
                ]
            }
        }
    }
    cursor = {"0": {"lane_index": 0, "elapsed_seconds": 0, "as_of": as_of}}
    # Pass runs from 30s to 150s after as_of: overlaps tail of "a" (30s),
    # all of "b" (60s), and head of the next "a" (30s) => a:60, b:60.
    pass_start = as_of + timedelta(seconds=30)
    pass_end = as_of + timedelta(seconds=150)
    passes = [{"satellite": "NOAA-19", "start": pass_start, "end": pass_end, "device": "0"}]

    result = ws.schedule_conflicts(passes, profile, cursor=cursor)
    displaced = {d["lane_id"]: d["seconds_displaced"] for d in result[0]["displaced_lanes"]}
    assert displaced == {"a": 60.0, "b": 60.0}
    assert result[0]["displaced_seconds"] == 120.0


def test_schedule_conflicts_multiple_passes_independent():
    start = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    profile = {"devices": {"0": {"lanes": [{"id": "voice", "seconds": 3600, "enabled": True}]}}}
    passes = [
        {"satellite": "NOAA-15", "start": start, "end": start + timedelta(minutes=10), "device": "0"},
        {"satellite": "NOAA-18", "start": start + timedelta(hours=2), "end": start + timedelta(hours=2, minutes=10), "device": "0"},
    ]
    result = ws.schedule_conflicts(passes, profile)
    assert len(result) == 2
    assert all(r["displaced_seconds"] == 600.0 for r in result)


# --- apt_lane_config -----------------------------------------------------

def test_apt_lane_config_returns_argv_lists_not_shell_strings():
    """Negative control: argv must be lists of separate tokens, never a single
    shell command string (injection risk)."""
    cfg = ws.apt_lane_config(137.1000, 600, "/tmp/apt_out")
    assert isinstance(cfg["rtl_fm"], list)
    assert isinstance(cfg["sox"], list)
    for argv in (cfg["rtl_fm"], cfg["sox"]):
        assert all(isinstance(tok, str) for tok in argv)
        # No token should itself be a shell pipeline/command string.
        for tok in argv:
            assert "|" not in tok
            assert ";" not in tok
            assert "&&" not in tok
    assert cfg["rtl_fm"][0] != cfg["sox"][0]  # two distinct processes, not one joined string


def test_apt_lane_config_uses_correct_frequency_and_output_rate():
    cfg = ws.apt_lane_config(137.6200, 900, "/tmp/apt_out")
    assert "137620000" in cfg["rtl_fm"]
    assert str(ws.APT_SAMPLE_RATE_HZ) in cfg["sox"]
    assert cfg["output_path"].startswith("/tmp/apt_out")
    assert cfg["output_path"].endswith(".wav")


def test_apt_lane_config_rejects_bad_input():
    with pytest.raises(ValueError):
        ws.apt_lane_config(0, 600, "/tmp/apt_out")
    with pytest.raises(ValueError):
        ws.apt_lane_config(137.1, 0, "/tmp/apt_out")
