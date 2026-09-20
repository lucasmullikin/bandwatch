#!/usr/bin/env python3
"""Continuous military-aircraft watch.

WHY THIS EXISTS, AND WHY IT IS NOT THE 1090 LANE.
The station already classifies military aircraft: collector.py's
aircraft_alerts() raises `notable_aircraft` off the plane_alert.csv list, and it
works -- it caught a KC-135R, a C-17A, a UH-72A and a C-12U on 2026-09-17/18.
But it can only classify what the radio HEARD, and the 1090 lane is one slot in
a rotating schedule: ~27% duty cycle by day, ~13% by night, and in practice far
less, because the lane wedges after ~24s on 70% of its runs on dongle 1. Over
2026-09-19 01:08->18:45 it produced 2500 events in ONE hour and nothing in the
other sixteen. "Always listening" cannot be built on that.

So the spine is the feed that is already continuous: adsb.fi's dedicated
military endpoint. The local receiver stays what it is -- an independent
corroborating witness when its lane runs.

TWO SOURCES, NEVER MIXED. Rows carry `source`, and network rows are NEVER
written to aircraft_positions. A row there means "my radio heard this", and
quietly filling it from the internet would destroy the one property that makes
the local record worth having. Agreement between the two is interesting; it is
not evidence, and it is not reception.

A GAP MUST READ AS A GAP. Every poll writes a mil_watch_health row, success or
failure. An hour with no sightings and no health rows means the watcher was
down; an hour with health rows and no sightings means the sky was empty. Those
are different facts and the schema is what keeps them apart. If the feed stays
unreachable past STALE_ALERT_MIN, the watcher pages about its own blindness --
silence is never reported as "no military aircraft".
"""
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")
CONFIG = os.path.join(
    os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
    "bandwatch.json")
STATE = os.path.join(VAR, "milwatch.json")       # OWN state file: one job, one sender
LOG = os.path.join(VAR, "logs", "milwatch.log")
ENRICH_DATA = os.path.join(ROOT, "enrich", "data")

sys.path.insert(0, ROOT)
from enrich import aircraft_db                                    # noqa: E402
from analysis import alert_policy                                 # noqa: E402

FEED_URL = "https://opendata.adsb.fi/api/v2/mil"
POLL_SEC = 60
# Station position, from config. Every radius below is measured FROM it, so a
# wrong value silently reports aircraft as near or far from somewhere you are
# not. Resolved lazily -- importing this module must not require a configured
# station, or the tests cannot run.
_SITE = None


def home():
    """(lat, lon) of the receiving station, from config."""
    global _SITE
    if _SITE is None:
        sys.path.insert(0, ROOT)
        import bandwatch_config as C
        lat, lon, _alt = C.station()
        _SITE = (lat, lon)
    return _SITE
LOG_RADIUS_NM = 250.0                        # recorded
ALERT_RADIUS_NM = 100.0                      # paged
ALERT_REPEAT_MIN = 90                        # same airframe, no repeat inside this
STALE_ALERT_MIN = 20                         # blind this long == page about the blindness
LOG_EVERY_MIN = 5.0                          # per-airframe write throttle, see below
LOG_MOVE_NM = 15.0                           # ...unless it moved this far since last write
RULE = "mil_aircraft"                        # NOT notable_aircraft: that rule is in
                                             # bandwatch.json's notify.no_notify_rules and is
                                             # deliberately log-only.
# A contactable UA is the courteous minimum when polling someone else's free
# feed. Set BANDWATCH_CONTACT to your own email or project URL.
USER_AGENT = "bandwatch-milwatch/1.0 (+%s)" % os.environ.get(
    "BANDWATCH_CONTACT", "https://github.com/lucasmullikin/bandwatch")


def now():
    return datetime.now(timezone.utc)


def local_now(c):
    """Quiet hours are wall-clock LOCAL; every ts written to the DB is UTC.

    collector.py learned this the hard way and says so at _local_now(): handing
    UTC to a "quiet after 22:00" rule silences the wrong six hours. At the reference site
    that would have muted every page between 16:00 and 01:00 local -- the busiest
    part of the day -- while the log showed the alert being 'held', which reads
    like working rate limiting rather than a timezone bug. Caught by the test,
    not by reading the code.

    should_notify() compares `now` against each recent alert's ts, so BOTH must
    be on this same clock.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(c.get("timezone", "UTC")))
    except Exception:
        return datetime.now(timezone.utc)


def log(msg):
    line = "%s  %s" % (now().isoformat(timespec="seconds"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def cfg():
    try:
        return json.load(open(CONFIG))
    except Exception:
        return {}


def load_state():
    try:
        s = json.load(open(STATE))
    except Exception:
        s = {}
    s.setdefault("alerted", {})       # hex -> iso ts of last page
    s.setdefault("recent_alerts", []) # for alert_policy's rate limiting
    s.setdefault("logged", {})        # hex -> [iso ts, dist_nm] of last row written
    s.setdefault("last_ok", None)
    s.setdefault("stale_paged", False)
    return s


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def nm_between(lat1, lon1, lat2, lon2):
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def ensure_schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS mil_sightings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      hex TEXT NOT NULL,
      callsign TEXT, reg TEXT, type TEXT, operator TEXT, category TEXT,
      alt_ft INTEGER, lat REAL, lon REAL, speed_kt INTEGER, track REAL,
      dist_nm REAL, squawk TEXT,
      source TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_mil_ts   ON mil_sightings(ts);
    CREATE INDEX IF NOT EXISTS ix_mil_hex  ON mil_sightings(hex);

    -- One row per poll, success or failure. This table is what makes a gap
    -- legible: no rows == the watcher was not running.
    CREATE TABLE IF NOT EXISTS mil_watch_health (
      ts TEXT PRIMARY KEY,
      ok INTEGER NOT NULL,
      n_global INTEGER,
      n_in_range INTEGER,
      error TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_milh_ts ON mil_watch_health(ts);
    """)
    con.commit()


def fetch():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _notify_send(text):
    """Hand the alert to the configured notifier.

    Every one of these tools used to carry its own copy of a Signal JSON-RPC
    call, with an account number baked in. Three copies meant three places to
    change a credential and three chances to leak one.
    """
    sys.path.insert(0, os.path.join(ROOT, "notify"))
    import notifiers
    return notifiers.send(cfg(), text)


def notify(text, s):
    c = cfg()
    if not c.get("notify_enabled"):
        log("NOTIFY suppressed (notify_enabled=false): %s" % text[:90])
        return
    ok, detail = _notify_send(text)
    if ok:
        log("NOTIFY sent: %s" % text.replace("\n", " ")[:110])
    else:
        log("NOTIFY FAILED: %s" % (detail or "no detail")[:160])


def describe(a, nb, dist):
    cs = (a.get("flight") or "").strip() or "-"
    typ = a.get("t") or (nb or {}).get("type") or "?"
    reg = a.get("r") or (nb or {}).get("registration") or ""
    op = (nb or {}).get("operator") or a.get("ownOp") or ""
    alt = a.get("alt_baro")
    alt_s = "%s ft" % alt if isinstance(alt, int) else str(alt or "?")
    bits = ["%s %s" % (cs, typ)]
    if reg:
        bits.append("(%s)" % reg)
    if op:
        bits.append("-- %s" % op)
    return "%s  %.0f nm, %s" % (" ".join(bits), dist, alt_s)


def _maybe_fresh(a, nb, d, hexid, s, fresh):
    """Alerting is deduped on its OWN clock, independently of the write
    throttle: a throttled write must never silently swallow a page."""
    last = s["alerted"].get(hexid)
    if last:
        try:
            age = (now() - datetime.fromisoformat(last)).total_seconds() / 60.0
        except Exception:
            age = 1e9
        if age < ALERT_REPEAT_MIN:
            return
    fresh.append((a, nb, d))


def poll_once(con, s):
    try:
        data = fetch()
    except Exception as e:
        con.execute("INSERT OR REPLACE INTO mil_watch_health (ts,ok,n_global,n_in_range,error)"
                    " VALUES (?,0,NULL,NULL,?)",
                    (now().isoformat(timespec="seconds"), "%s: %s" % (type(e).__name__, e)[:200]))
        con.commit()
        log("FETCH FAILED: %s: %s" % (type(e).__name__, e))
        return

    ac = data.get("ac") or data.get("aircraft") or []
    in_range, fresh = [], []
    for a in ac:
        lat, lon = a.get("lat"), a.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        _hlat, _hlon = home()
        d = nm_between(_hlat, _hlon, lat, lon)
        if d > LOG_RADIUS_NM:
            continue
        hexid = (a.get("hex") or "").strip().lower()
        nb = aircraft_db.notable(hexid) or {}
        in_range.append((a, nb, d))

        prev = s["logged"].get(hexid)
        if prev:
            try:
                age_min = (now() - datetime.fromisoformat(prev[0])).total_seconds() / 60.0
            except Exception:
                age_min = 1e9
            moved = abs(d - (prev[1] if len(prev) > 1 else 1e9))
            if age_min < LOG_EVERY_MIN and moved < LOG_MOVE_NM:
                if d <= ALERT_RADIUS_NM:
                    _maybe_fresh(a, nb, d, hexid, s, fresh)
                continue
        s["logged"][hexid] = [now().isoformat(timespec="seconds"), d]

        con.execute(
            "INSERT INTO mil_sightings (ts,hex,callsign,reg,type,operator,category,"
            "alt_ft,lat,lon,speed_kt,track,dist_nm,squawk,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now().isoformat(timespec="seconds"), hexid,
             (a.get("flight") or "").strip() or None,
             a.get("r") or nb.get("registration"),
             a.get("t") or nb.get("type"),
             nb.get("operator") or a.get("ownOp"),
             nb.get("category"),
             a.get("alt_baro") if isinstance(a.get("alt_baro"), int) else None,
             lat, lon,
             int(a["gs"]) if isinstance(a.get("gs"), (int, float)) else None,
             a.get("track"), round(d, 1), a.get("squawk"), "adsb.fi"))

        if d <= ALERT_RADIUS_NM:
            _maybe_fresh(a, nb, d, hexid, s, fresh)

    con.execute("INSERT OR REPLACE INTO mil_watch_health (ts,ok,n_global,n_in_range,error)"
                " VALUES (?,1,?,?,NULL)",
                (now().isoformat(timespec="seconds"), len(ac), len(in_range)))
    con.commit()

    for _m in ("alerted", "logged"):
        if len(s[_m]) > 4000:
            s[_m] = dict(sorted(s[_m].items(),
                                key=lambda kv: kv[1][0] if isinstance(kv[1], list) else kv[1],
                                reverse=True)[:2000])

    s["last_ok"] = now().isoformat(timespec="seconds")
    s["stale_paged"] = False

    if not fresh:
        return

    c = cfg()
    lnow = local_now(c)
    alert = {"rule": RULE, "ts": lnow.isoformat(timespec="seconds")}
    ok, why = alert_policy.should_notify(alert, c, lnow, s["recent_alerts"])
    lines = [describe(a, nb, d) for a, nb, d in fresh]
    for a, _nb, _d in fresh:
        s["alerted"][(a.get("hex") or "").strip().lower()] = now().isoformat(timespec="seconds")
    if ok:
        notify("MILITARY AIR -- %d within %.0f nm\n%s"
               % (len(fresh), ALERT_RADIUS_NM, "\n".join(lines)), s)
        s["recent_alerts"].append(alert)
        s["recent_alerts"] = s["recent_alerts"][-200:]
    else:
        # Logged either way. Rate limiting decides what PAGES, never what is recorded.
        log("held (%s): %s" % (why, " | ".join(lines)))


def check_stale(s):
    """Page about our own blindness. Silence is not a negative result."""
    if not s.get("last_ok") or s.get("stale_paged"):
        return
    try:
        age = (now() - datetime.fromisoformat(s["last_ok"])).total_seconds() / 60.0
    except Exception:
        return
    if age >= STALE_ALERT_MIN:
        notify("MIL WATCH BLIND: no successful fetch from the ADS-B military feed "
               "for %.0f min. This is NOT 'no military aircraft' -- it is no data. "
               "Local 1090 reception is a rotated lane and does not cover the gap."
               % age, s)
        s["stale_paged"] = True


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    counts = aircraft_db.load(ENRICH_DATA)
    log("milwatch up -- enrichment: %s" % counts)
    con = sqlite3.connect(DB, timeout=30)
    ensure_schema(con)
    s = load_state()
    once = "--once" in sys.argv[1:]
    while True:
        try:
            poll_once(con, s)
            check_stale(s)
            save_state(s)
        except Exception as e:
            log("POLL LOOP ERROR: %s: %s" % (type(e).__name__, e))
        if once:
            return 0
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
