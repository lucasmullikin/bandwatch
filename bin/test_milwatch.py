"""Controls for the military watch's decide-and-record logic.

Runs against a scratch DB and a stubbed feed, so nothing pages and the live
record is untouched. Proves the three properties that matter:
  * a close aircraft PAGES, a distant one is LOGGED ONLY, a far one is neither
  * the same airframe does not page twice inside ALERT_REPEAT_MIN
  * a failed fetch writes a health row saying so -- it NEVER looks like "no
    military aircraft"

Written as a script originally, ending in sys.exit(). pytest hit that during
collection and reported the file as contributing zero tests, so none of this
ran under the harness. The three passes are sequential and share state -- the
dedupe pass only means anything after the pass that paged -- so they stay in
one fixture and the assertions become the tests.
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# The reference site is 40.65,-76.25. 1 deg lat ~= 60 nm.
CLOSE = {"hex": "ae1111", "flight": "REACH01 ", "t": "C17", "r": "00-0185",
         "lat": 40.9, "lon": -76.25, "alt_baro": 24000, "gs": 410}
MID = {"hex": "ae2222", "flight": "GRIZZLY2", "t": "KC135", "r": "58-0118",
       "lat": 43.3, "lon": -76.25, "alt_baro": 31000, "gs": 440}      # ~160 nm
FAR = {"hex": "ae3333", "flight": "FARAWAY3", "t": "C130", "r": "08-5679",
       "lat": 49.0, "lon": -76.25, "alt_baro": 25000, "gs": 300}      # ~500 nm


@pytest.fixture(scope="module")
def passes(tmp_path_factory):
    spec = importlib.util.spec_from_file_location(
        "mw", os.path.join(HERE, "bw-milwatch.py"))
    mw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mw)

    tmp = str(tmp_path_factory.mktemp("milwatch"))
    mw.DB = os.path.join(tmp, "t.db")
    mw.STATE = os.path.join(tmp, "s.json")
    mw.LOG = os.path.join(tmp, "t.log")
    # Pin the station position: home() reads config, and a unit test must not
    # need a configured station. Every distance here is measured from this.
    mw.home = lambda: (40.65, -76.25)
    pages = []
    mw.notify = lambda text, s: pages.append(text)
    mw.cfg = lambda: {"notify_enabled": True, "signal_recipient": "+1",
                      "signal_rpc": "http://x", "quiet_start": "22:00",
                      "quiet_end": "07:00", "max_per_rule_per_hour": 10,
                      "max_alerts_per_hour": 30, "timezone": "UTC"}

    # PIN THE CLOCK. These three properties are about distance, dedupe and
    # feed outages -- none of them is about the time of day. Left on the real
    # clock the fixture inherits the quiet-hours rule, so every page is held
    # between 22:00 and 07:00 and the distance assertions fail for nine hours
    # out of twenty-four. That is what was happening: the file also aborted
    # pytest during collection, so the harness reported it as "zero tests"
    # rather than "fails at night", and the flake was invisible from both ends.
    #
    # 14:00 is the middle of the waking day on the same clock local_now uses.
    from datetime import datetime as _dt, timezone as _tz
    _fixed = _dt(2026, 9, 20, 14, 0, tzinfo=_tz.utc)
    mw.local_now = lambda _c: _fixed
    mw.now = lambda: _fixed

    con = sqlite3.connect(mw.DB)
    mw.ensure_schema(con)
    state = mw.load_state()

    def run(aclist, fail=False):
        if fail:
            mw.fetch = lambda: (_ for _ in ()).throw(OSError("simulated feed outage"))
        else:
            mw.fetch = lambda: {"ac": list(aclist)}
        before = len(pages)
        mw.poll_once(con, state)
        n_s = con.execute("select count(*) from mil_sightings").fetchone()[0]
        health = con.execute(
            "select ok, n_in_range, error from mil_watch_health "
            "order by ts desc limit 1").fetchone()
        return len(pages) - before, n_s, health, list(pages)

    first = run([CLOSE, MID, FAR])
    # Defeat only the WRITE throttle, so what is tested next is the alert
    # dedupe rather than the sighting throttle standing in for it.
    state["logged"] = {}
    second = run([CLOSE, MID, FAR])
    outage = run([], fail=True)
    return {"first": first, "second": second, "outage": outage}


# ------------------------------------------------ distance decides the action

def test_only_the_close_aircraft_pages(passes):
    npages, _, _, _ = passes["first"]
    assert npages == 1


def test_the_page_names_the_close_aircraft(passes):
    _, _, _, pages = passes["first"]
    assert "REACH01" in pages[-1]


def test_the_far_aircraft_is_not_paged(passes):
    _, _, _, pages = passes["first"]
    assert "FARAWAY3" not in pages[-1]


def test_close_and_mid_are_logged_but_the_far_one_is_outside_the_radius(passes):
    _, n_sightings, _, _ = passes["first"]
    assert n_sightings == 2


def test_health_records_two_aircraft_in_range(passes):
    _, _, health, _ = passes["first"]
    assert health[0] == 1 and health[1] == 2


# ------------------------------------------------------------------- dedupe

def test_the_same_airframe_does_not_page_twice_in_the_repeat_window(passes):
    npages, _, _, _ = passes["second"]
    assert npages == 0


# ------------------------------------------------------------------ outage

def test_an_outage_does_not_page_a_sighting(passes):
    npages, _, _, _ = passes["outage"]
    assert npages == 0


def test_an_outage_writes_ok_zero_with_the_error_text(passes):
    _, _, health, _ = passes["outage"]
    assert health[0] == 0 and health[2] is not None


def test_an_outage_records_n_in_range_as_null_never_zero(passes):
    """A gap in the feed is not a quiet sky, and 0 would read as one."""
    _, _, health, _ = passes["outage"]
    assert health[1] is None


# ------------------------------------------------------------- quiet hours

@pytest.fixture(scope="module")
def at_night(tmp_path_factory):
    """The same close aircraft, at 23:00 on the configured clock.

    Pinning the clock in the fixture above keeps the distance assertions
    deterministic, but it also means nothing exercises quiet hours -- and
    local_now()'s own docstring says a timezone bug there was caught by this
    test, not by reading the code. So quiet hours gets its own pinned clock
    rather than being left to whenever the suite happens to run.
    """
    spec = importlib.util.spec_from_file_location(
        "mw_night", os.path.join(HERE, "bw-milwatch.py"))
    mw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mw)

    tmp = str(tmp_path_factory.mktemp("milwatch_night"))
    mw.DB = os.path.join(tmp, "t.db")
    mw.STATE = os.path.join(tmp, "s.json")
    mw.LOG = os.path.join(tmp, "t.log")
    mw.home = lambda: (40.65, -76.25)
    pages = []
    mw.notify = lambda text, s: pages.append(text)
    mw.cfg = lambda: {"notify_enabled": True, "signal_recipient": "+1",
                      "signal_rpc": "http://x", "quiet_start": "22:00",
                      "quiet_end": "07:00", "max_per_rule_per_hour": 10,
                      "max_alerts_per_hour": 30, "timezone": "UTC"}
    from datetime import datetime as _dt, timezone as _tz
    night = _dt(2026, 9, 20, 23, 0, tzinfo=_tz.utc)
    mw.local_now = lambda _c: night
    mw.now = lambda: night

    con = sqlite3.connect(mw.DB)
    mw.ensure_schema(con)
    state = mw.load_state()
    mw.fetch = lambda: {"ac": [CLOSE]}
    mw.poll_once(con, state)
    n_s = con.execute("select count(*) from mil_sightings").fetchone()[0]
    return {"pages": pages, "sightings": n_s}


def test_a_close_aircraft_inside_quiet_hours_is_not_paged(at_night):
    assert at_night["pages"] == []


def test_quiet_hours_withholds_the_page_but_still_records_the_sighting(at_night):
    """Quiet hours silences the phone, never the record."""
    assert at_night["sightings"] == 1
