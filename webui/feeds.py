"""Are we actually contributing to the aggregators right now?

"Configured to feed" and "feeding" are different claims, and a panel that
shows the first while meaning the second is the same class of lie this project
has spent the day removing. So this asks the operating system what TCP
connections exist, rather than reading the config back.

One honest complication: the ADS-B lane runs about 23% of the rotation, so
"not connected" is the NORMAL state most of the time. A feed panel that showed
red whenever readsb happened to be between turns would be worse than useless.
The state is therefore three-way -- feeding / idle between lane turns /
configured but never seen -- and the last successful connection is remembered
so silence can be dated.
"""
import json
import os
import subprocess
import time

FEEDS = [
    {"id": "airplanes.live", "host": "feed.airplanes.live", "port": 30004,
     "why": "community-owned, unfiltered"},
    {"id": "adsbexchange", "host": "feed.adsbexchange.com", "port": 30005,
     "why": "unfiltered, JETNET-owned since 2023"},
]
STATE_NAME = "feedstate.json"


def _state_path(root):
    return os.path.join(root, "var", STATE_NAME)


def _load(root):
    try:
        return json.load(open(_state_path(root)))
    except Exception:
        return {}


def _save(root, d):
    p = _state_path(root)
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(d, open(tmp, "w"), indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def _readsb_pid():
    try:
        r = subprocess.run(["pgrep", "-f", "[r]eadsb"],
                           capture_output=True, text=True, timeout=8)
        pids = [p for p in (r.stdout or "").split() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _established(pid):
    """Remote endpoints readsb currently has open. Asks the OS, not the config."""
    if not pid:
        return set()
    try:
        r = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return set()
    out = set()
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[8]
        if "->" in name:
            out.add(name.split("->", 1)[1])
    return out


def status(root):
    pid = _readsb_pid()
    live = _established(pid)
    st = _load(root)
    now = time.time()
    rows = []
    for f in FEEDS:
        key = "%s:%d" % (f["host"], f["port"])
        connected = any(str(f["port"]) in e for e in live)
        prev = st.get(f["id"]) or {}
        if connected:
            prev["last_connected"] = now
        last = prev.get("last_connected")
        if connected:
            state, detail = "feeding", "connected now"
        elif pid is None:
            state = "idle"
            detail = ("the ADS-B lane is not its turn -- it runs ~23% of the "
                      "rotation, so this is normal")
        elif last:
            state, detail = "idle", "readsb is up but this feed is not connected"
        else:
            state, detail = "never seen", "no successful connection recorded yet"
        rows.append({
            "id": f["id"], "host": f["host"], "port": f["port"],
            "why": f["why"], "state": state, "detail": detail,
            "last_connected": last,
            "last_connected_min_ago": None if not last else round((now - last) / 60, 1),
        })
        st[f["id"]] = prev
    _save(root, st)
    return {
        "feeds": rows,
        "readsb_running": pid is not None,
        "mlat": False,
        "mlat_note": ("MLAT is deliberately NOT connected. It needs this "
                      "receiver's true coordinates, and feeding a deliberately "
                      "offset position would corrupt other stations' solutions "
                      "rather than protect this one. ADS-B accuracy does not "
                      "depend on receiver position, so nothing is lost."),
        "note": ("Both targets are UNFILTERED aggregators -- they do not "
                 "honour aircraft blocking requests. FlightAware and FR24 do, "
                 "which would remove the aircraft this system exists to see."),
    }
