#!/usr/bin/env python3
"""Automatically capture good satellite passes, without starving the lanes.

A pass cannot be rescheduled, so this waits for one, takes the radio, records,
decodes, sends both channels, and gives the radio straight back.

THE BUDGET IS THE WHOLE DESIGN. Every capture costs the low-band radio about
seventeen minutes -- guard 121.5, tower, ACARS, VHF fire and APRS all go off
the air for that long. There are roughly nine passes a day above 20 degrees,
and taking all of them would spend two and a half hours a day of one radio on
weather pictures.

So it is selective by default:

  * only passes at or above `sat_min_elevation` (default 40 degrees). Below
    about 30 an APT image is mostly noise, so a low pass costs the same radio
    time for a worse picture -- the least defensible trade in the system.
  * at most `sat_max_per_day` captures (default 4), so a busy pass day cannot
    quietly eat the airband lanes.
  * a hard floor of `sat_min_gap_min` between captures, so two passes minutes
    apart cannot chain into a 35-minute blackout.

Everything is in config/bandwatch.json and the daily count is reported in the log, so
the cost is visible rather than implicit.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
sys.path.insert(0, ROOT)
import bandwatch_config as C  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "lanes"))
LOG = os.path.join(VAR, "logs", "satwatch.log")
STATE = os.path.join(VAR, "satwatch.json")
TLE = os.path.join(VAR, "noaa.tle")
# --- site -------------------------------------------------------------
# From config, never hardcoded. Satellite geometry is computed FROM this point:
# a wrong coordinate does not fail, it predicts passes over somewhere else and
# looks exactly like a correct prediction.
#
# Keep it approximate. Three decimal places is a city block and buys no useful
# accuracy for a pass prediction; your exact address should not appear in
# source, in a config file you might share, or in any request built from these
# constants.
# Resolved on first use -- see the note in bin/flight-track.py. A module that
# demands configuration at import time cannot even print its own help.
_SITE = []


def SITE_():
    if not _SITE:
        _SITE.extend(C.station())
    return _SITE
# NOAA APT went off air in 2025; the live LRPT birds are the Meteors, both
# on 137.9 MHz. Scheduling a NOAA pass books a radio to record silence.
FREQS = {"METEOR-M2 3": 137.9000, "METEOR-M2 4": 137.9000}

DEFAULTS = {
    "sat_auto_capture": True,
    "sat_min_elevation": 40.0,
    "sat_max_per_day": 4,
    "sat_min_gap_min": 45,
    "sat_tle_max_age_days": 3.0,
}


def log(msg):
    line = "%s  %s" % (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       msg)
    # stdout is redirected into this same file by the supervisor, so writing
    # both would double every line and make the log look like the loop is
    # running twice as often as it is
    if sys.stdout.isatty():
        print(line, flush=True)
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    else:
        print(line, flush=True)


def cfg():
    c = dict(DEFAULTS)
    try:
        c.update(json.load(open(os.path.join(C.CONFIG, "bandwatch.json"))))
    except Exception:
        pass
    return c


def state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"captures": []}


def save(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def recent_captures(s, hours=24):
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return [c for c in s.get("captures", []) if c.get("ts", "") >= cut]


def tle_age_days():
    try:
        return (time.time() - os.path.getmtime(TLE)) / 86400.0
    except OSError:
        return 1e9


def refresh_tles():
    """Stale elements mean a pass predicted minutes off -- and a capture that
    starts after the satellite has already risen loses the top of the image."""
    try:
        subprocess.run([os.path.join(ROOT, "bin", "bw-satpass"), "--refresh",
                        "--hours", "1"], capture_output=True, timeout=180)
        log("TLEs refreshed (age now %.2f d)" % tle_age_days())
    except Exception as e:
        log("TLE refresh failed: %s -- predictions will drift" % e)


def upcoming(min_el, hours=14):
    import passes as P
    if not os.path.exists(TLE):
        return []
    lines = [l.rstrip("\n") for l in open(TLE) if l.strip()]
    now = datetime.now(timezone.utc)
    out = []
    for i in range(0, len(lines) - 2, 3):
        name = lines[i].strip()
        for p in P.next_passes(lines[i + 1], lines[i + 2], SITE_()[0], SITE_()[1],
                               SITE_()[2], start=now, hours=hours,
                               min_elevation_deg=min_el, name=name):
            p["freq_mhz"] = FREQS.get(name)
            out.append(p)
    out.sort(key=lambda p: p["start"])
    return out


def capture_running():
    try:
        return subprocess.run(["pgrep", "-f", "[s]dr-capture-sat"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def main():
    log("satwatch up")
    while True:
        c = cfg()
        if not c.get("sat_auto_capture", True):
            log("auto-capture disabled in config/bandwatch.json; idling")
            time.sleep(900)
            continue

        if tle_age_days() > c["sat_tle_max_age_days"]:
            refresh_tles()

        s = state()
        done = recent_captures(s)
        if len(done) >= int(c["sat_max_per_day"]):
            log("daily budget spent (%d/%d captures in 24h); idling"
                % (len(done), c["sat_max_per_day"]))
            time.sleep(1800)
            continue

        ps = upcoming(float(c["sat_min_elevation"]))
        if not ps:
            log("no pass above %.0f deg in the next 14h; checking again later"
                % c["sat_min_elevation"])
            time.sleep(3600)
            continue

        nxt = ps[0]
        start = datetime.fromisoformat(nxt["start"])
        now = datetime.now(timezone.utc)

        # do not chain two captures into one long blackout
        if done:
            last = max(d["ts"] for d in done)
            gap = (start - datetime.fromisoformat(last)).total_seconds() / 60.0
            if gap < float(c["sat_min_gap_min"]):
                log("skipping %s at %s -- only %.0f min after the last capture "
                    "(floor is %s)" % (nxt["satellite"], nxt["start"][11:16],
                                       gap, c["sat_min_gap_min"]))
                time.sleep(max(120, (start - now).total_seconds() + 120))
                continue

        wait = (start - now).total_seconds() - 150      # wake 2.5 min early
        if wait > 1800:
            log("next: %s at %s (%.1f deg) in %.0f min -- sleeping"
                % (nxt["satellite"], nxt["start"][11:16],
                   nxt["max_elevation_deg"], (start - now).total_seconds() / 60))
            time.sleep(1800)
            continue
        if wait > 0:
            log("next: %s at %s (%.1f deg) -- waking in %.0f s"
                % (nxt["satellite"], nxt["start"][11:16],
                   nxt["max_elevation_deg"], wait))
            time.sleep(wait)

        if capture_running():
            log("a capture is already running; leaving it alone")
            time.sleep(600)
            continue

        log("CAPTURING %s at %s (%.1f deg, %.4f MHz) -- radio busy ~17 min"
            % (nxt["satellite"], nxt["start"][11:16],
               nxt["max_elevation_deg"], nxt["freq_mhz"]))
        rc = 1
        try:
            r = subprocess.run(
                [os.path.join(ROOT, "bin", "bw-capture-sat"),
                 "--at", nxt["start"][:16], "--signal"],
                capture_output=True, text=True, timeout=2400)
            rc = r.returncode
            for line in (r.stdout or "").splitlines()[-6:]:
                log("  " + line)
        except Exception as e:
            log("capture failed: %s" % e)

        s = state()
        s.setdefault("captures", []).append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "satellite": nxt["satellite"],
            "max_elevation_deg": nxt["max_elevation_deg"],
            "ok": rc == 0,
        })
        s["captures"] = recent_captures(s, hours=72)
        save(s)
        log("captures in the last 24h: %d of %d allowed"
            % (len(recent_captures(s)), c["sat_max_per_day"]))
        time.sleep(120)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("satwatch stopped")
