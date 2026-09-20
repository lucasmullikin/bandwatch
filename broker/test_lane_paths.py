"""Lane output files live under VAR, never under ROOT.

A lane's output is DATA. Resolving it against the checkout is correct only
while var/ sits inside the checkout -- true in every developer install and
false in every shared one -- and the failure is completely silent:
os.path.exists() returns false, the lane is skipped by `continue`, and the
collector ingests nothing while every producer keeps writing happily.

Measured: 35 minutes of ADS-B written to a 115 MB file, not one row read, and
no error anywhere. The only outward sign was that the newest event stopped
getting newer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector as C  # noqa: E402


def test_a_bare_relative_path_resolves_under_var(monkeypatch):
    monkeypatch.setattr(C, "VAR", "/data/var")
    assert C.lane_path("events/adsb.sbs") == "/data/var/events/adsb.sbs"


def test_a_legacy_var_prefixed_path_does_not_double_up(monkeypatch):
    """Configs in the wild say "var/events/x" from when var was always $ROOT/var."""
    monkeypatch.setattr(C, "VAR", "/data/var")
    assert C.lane_path("var/events/adsb.sbs") == "/data/var/events/adsb.sbs"


def test_an_absolute_path_is_left_alone(monkeypatch):
    monkeypatch.setattr(C, "VAR", "/data/var")
    assert C.lane_path("/mnt/big/adsb.sbs") == "/mnt/big/adsb.sbs"


def test_a_lone_var_segment_is_not_swallowed(monkeypatch):
    """"var" alone is a filename, not the prefix -- stripping it yields "".

    Worth pinning: the partition() would otherwise leave an empty tail and the
    lane would silently read the VAR directory itself.
    """
    monkeypatch.setattr(C, "VAR", "/data/var")
    assert C.lane_path("var") == "/data/var/var"


def test_a_nested_var_directory_is_only_stripped_once(monkeypatch):
    monkeypatch.setattr(C, "VAR", "/data/var")
    assert C.lane_path("var/var/x") == "/data/var/var/x"


def test_it_never_resolves_against_root(monkeypatch):
    """The actual bug: ROOT and VAR disagreeing produced a path nothing wrote to."""
    monkeypatch.setattr(C, "ROOT", "/opt/bandwatch")
    monkeypatch.setattr(C, "VAR", "/srv/station/var")
    got = C.lane_path("var/events/adsb.sbs")
    assert got.startswith("/srv/station/var")
    assert "/opt/bandwatch" not in got


def test_generated_confs_are_read_from_the_config_dir_not_the_checkout():
    """conf/ is GENERATED into the config directory, which may be elsewhere.

    Reading it from $ROOT/conf left freq_mhz NULL on every recording once the
    config moved -- the same silent-empty failure the docstring above describes.
    """
    assert C.CONF_DIR.startswith(C.CONFIG_DIR)
    assert C.CONF_DIR.endswith("conf")
