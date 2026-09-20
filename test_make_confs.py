"""The generated rtl_airband configs decide what the receiver can hear.

A wrong value here does not raise: rtl_airband accepts the file, the lane runs,
the process looks healthy, and it records nothing. So the parts that are easy to
get quietly wrong -- squelch and where the tuner is centred -- are held here.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bandwatch_config as C  # noqa: E402
import make_confs as M  # noqa: E402


def snr_of(text):
    import re
    return sorted({float(x) for x in re.findall(r"squelch_snr_threshold = ([\d.]+)", text)})


def centre_of(text):
    import re
    return float(re.search(r"centerfreq = ([\d.]+)", text).group(1))


AM = [("TWR", 118.1), ("APP", 119.0)]
FM = [("A", 154.265), ("B", 154.28)]


# ------------------------------------------------------------------ squelch

def test_am_defaults_to_15db():
    """9.54 dB, the library default, gave 155 recordings an hour, 74% under 2s."""
    assert snr_of(M.build("t", "am", AM, "", "/rec")) == [15.0]


def test_nfm_defaults_to_11db():
    assert snr_of(M.build("t", "nfm", FM, "", "/rec")) == [11.0]


def test_a_set_may_override_the_squelch():
    """Some bands need a lower threshold to hear anything at all.

    Without this the only way to set it was editing the GENERATED file, which
    the next run silently reverts -- and on one station did, holding vhf_fire at
    7.0 dB in a file whose own header says not to hand-edit it.
    """
    assert snr_of(M.build("t", "nfm", FM, "", "/rec", squelch=7.0)) == [7.0]


def test_override_of_zero_is_honoured_not_treated_as_unset():
    """0.0 is falsy; `squelch or default` would silently discard it."""
    assert snr_of(M.build("t", "nfm", FM, "", "/rec", squelch=0.0)) == [0.0]


def test_non_numeric_squelch_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "load", lambda _n: {"sets": {
        "s": {"mode": "nfm", "channels": [["A", 154.0]], "squelch_snr": "loud"}}})
    with pytest.raises(C.ConfigError) as e:
        M.load_sets()
    assert "squelch_snr" in str(e.value)


def test_boolean_squelch_is_refused(tmp_path, monkeypatch):
    """True is an int in Python and would become a 1 dB threshold."""
    monkeypatch.setattr(C, "load", lambda _n: {"sets": {
        "s": {"mode": "nfm", "channels": [["A", 154.0]], "squelch_snr": True}}})
    with pytest.raises(C.ConfigError):
        M.load_sets()


# ------------------------------------------------------------------ tuning

def test_tuner_is_never_centred_on_a_channel():
    """The RTL-SDR DC spike sits at the tuner centre and would swallow it."""
    chans = [("A", 118.0), ("B", 118.0), ("C", 118.0)]
    c = centre_of(M.build("t", "am", chans, "", "/rec"))
    assert all(abs(f - c) > 0.060 for _, f in chans)


def test_centre_is_the_block_midpoint_so_the_worst_offset_is_half_the_span():
    chans = [("A", 118.1), ("B", 119.6)]
    c = centre_of(M.build("t", "am", chans, "", "/rec"))
    assert max(abs(f - c) for _, f in chans) <= (119.6 - 118.1) / 2 + 0.001


def test_a_span_wider_than_the_passband_is_refused():
    """Better to fail than to emit a config whose edge channels are inaudible."""
    with pytest.raises(SystemExit):
        M.build("t", "am", [("A", 118.0), ("B", 121.0)], "", "/rec")


def test_placeholder_sets_are_refused_by_default(monkeypatch):
    monkeypatch.setattr(C, "load", lambda _n: {"sets": {
        "air_tower": {"replace_me": True, "mode": "am", "channels": [["A", 118.1]]}}})
    monkeypatch.setattr(sys, "argv", ["make_confs.py"])
    with pytest.raises(C.ConfigError) as e:
        M.load_sets()
    assert "placeholder" in str(e.value).lower()
