"""Controls for the overnight vehicle watch.

Scratch DB and scratch source, so the real baseline is untouched and nothing is
sent.

Written as a script originally, ending in sys.exit(). pytest hit that during
collection, aborted, and reported the file as contributing zero tests -- so the
whole vehicle watch was unprotected by the harness while the file sat in the
tree looking like coverage. Same scenario, same assertions, as tests.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    spec = importlib.util.spec_from_file_location(
        "v", os.path.join(HERE, "bw-vehicles.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(proto, sid, dt, psi=34.0):
    return json.dumps({"time": dt.isoformat(timespec="seconds"), "model": proto,
                       "id": sid, "pressure_PSI": psi, "rssi": -9.0, "snr": 12.0})


@pytest.fixture(scope="module")
def watch(tmp_path_factory):
    """One overnight run: a known resident, an arriving Ford set, one passer-by."""
    v = load_module()
    tmp = str(tmp_path_factory.mktemp("vehicles"))
    v.DB = os.path.join(tmp, "t.db")
    v.STATE = os.path.join(tmp, "s.json")
    v.LOG = os.path.join(tmp, "t.log")
    v.SRC = os.path.join(tmp, "tpms.jsonl")
    sent = []
    v.send = lambda t: sent.append(t)
    v.cfg = lambda: {"notify_enabled": True,
                     "signal_recipient": "+1", "signal_rpc": "http://x"}

    base = datetime.now(timezone.utc) - timedelta(hours=3)

    # A known resident car, seen on many earlier passes -- this is the baseline.
    known = [row("Toyota", "aa0001", base - timedelta(days=d)) for d in range(1, 6)]
    open(v.SRC, "w").write("\n".join(known) + "\n")

    con = sqlite3.connect(v.DB)
    v.ensure_schema(con)
    state = v.load_state()
    v.ingest(con, state)
    state["baselined"] = True
    v.save_state(state)

    # Overnight: the resident again, PLUS an unknown 4-wheel Ford set arriving
    # together, PLUS a single unknown Schrader wheel passing once.
    new = [row("Toyota", "aa0001", base)]
    for i, sid in enumerate(["bb1000", "bb1002", "bb1004", "bb1006"]):
        new.append(row("Ford", sid, base + timedelta(seconds=10 + i * 20)))
    new.append(row("Schrader-EG53MA4", "CC9999", base + timedelta(minutes=40)))
    open(v.SRC, "a").write("\n".join(new) + "\n")

    _n, fresh = v.ingest(con, state)
    v.save_state(state)
    text, nclusters = v.digest(con, state)
    return {"fresh": fresh, "text": text, "nclusters": nclusters, "sent": sent}


def test_five_new_wheel_sensors_detected(watch):
    assert len(watch["fresh"]) == 5           # 4 Ford + 1 Schrader


def test_the_known_resident_is_not_reported_as_new(watch):
    """The baseline's whole purpose: a car that lives here is not an event."""
    assert "aa0001" not in watch["text"]


def test_five_sensors_resolve_to_two_vehicles(watch):
    assert watch["nclusters"] == 2


def test_the_ford_set_is_recognised_as_one_full_set(watch):
    assert "4 wheels -- a full set" in watch["text"]


def test_make_family_is_reported_from_the_protocol(watch):
    assert "Ford / Lincoln" in watch["text"]


def test_consecutive_ids_are_flagged_as_corroboration(watch):
    assert "ids run consecutively" in watch["text"]


def test_schrader_is_reported_as_multi_make_not_guessed(watch):
    """The protocol does not identify one manufacturer, so it must not claim to."""
    assert "GM / Stellantis / Nissan" in watch["text"]


def test_a_once_heard_wheel_is_called_out_as_passing(watch):
    assert "passing through, not parked" in watch["text"]


def test_the_report_states_the_limit_of_the_inference(watch):
    """A tyre sensor identifies a wheel, never a person and never a model."""
    assert "not a model" in watch["text"]
