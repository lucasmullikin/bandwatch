#!/usr/bin/env python3
"""Overnight vehicle watch, built on the 315 MHz TPMS lane.

WHAT A TPMS ID IS, AND WHAT IT IS NOT.
The lane note in the profile already says it: a TPMS id is a TYRE SENSOR, never
a person. This tool does not weaken that. It tracks WHEELS. Everything above a
wheel -- "this is one vehicle", "this is a Toyota", "this one is new" -- is an
INFERENCE with a stated basis, and the report says which is which. A sensor can
be swapped between cars, a car can arrive on four sensors from four sources, and
a rental or a new set of tyres looks exactly like a new vehicle.

WHY THE MAKE IS A FAMILY, NOT A MODEL. rtl_433's `model` field names the
DECODER -- the sensor protocol -- not the car. "Toyota" means a Pacific
Industrial-style protocol used across Toyota/Lexus/Scion. "Schrader-EG53MA4" is
an OE/aftermarket sensor fitted across several GM, Stellantis and Nissan lines.
Reporting "a 2019 Camry" from this would be invention. The report gives the
protocol, the family it implies, and says plainly where the confidence ends.

HOW FOUR WHEELS BECOME ONE VEHICLE. Two independent signals, and the report
says which fired:
  * CO-OCCURRENCE -- sensors heard inside CLUSTER_SEC of each other, merged
    transitively. This is the primary signal.
  * ID ADJACENCY -- factory sets are often issued in a run (00ab12c0/c4/c8/cc).
    Corroboration only; it must never merge on its own, because two unrelated
    cars can share a prefix.

WHAT "NEW" MEANS. Not in vehicle_sensors before this run. The baseline is
whatever has been ingested, so the FIRST ingest necessarily calls everything new
-- it is reported as a baseline, not as an event, because "I have never looked
before" is not the same finding as "this has never been here".
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")
CONFIG = os.path.join(
    os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
    "bandwatch.json")
STATE = os.path.join(VAR, "vehicles.json")
LOG = os.path.join(VAR, "logs", "vehicles.log")
SRC = os.path.join(VAR, "events", "tpms315.jsonl")

CLUSTER_SEC = 120          # sensors heard this close together -> probably one vehicle
DIGEST_HOURS = 14          # the overnight window the morning digest covers

# Protocol -> what it actually tells you. Deliberately conservative: these are
# the families the decoder implies, not a model identification.
FAMILY = {
    "Toyota":            ("Toyota / Lexus / Scion", "Pacific Industrial-style OE sensor"),
    "Ford":              ("Ford / Lincoln", "Ford OE sensor protocol"),
    "Schrader":          ("multi-make", "Schrader OE/aftermarket -- GM, Stellantis, Nissan and others"),
    "Schrader-EG53MA4":  ("multi-make (GM / Stellantis / Nissan common)", "Schrader EG53MA4 OE sensor"),
    "Schrader-SMD3MA4":  ("multi-make (Ford / Mazda / JLR common)", "Schrader SMD3MA4 OE sensor"),
    "Elantra2012":       ("Hyundai / Kia", "Elantra-generation protocol (~2011-2013)"),
    "Renault":           ("Renault / Dacia", "Renault OE sensor"),
    "Citroen":           ("Stellantis (Citroen / Peugeot)", "PSA OE sensor"),
    "Abarth":            ("Stellantis (Fiat / Abarth)", "Fiat OE sensor"),
}


def now():
    return datetime.now(timezone.utc)


def log(m):
    line = "%s  %s" % (now().isoformat(timespec="seconds"), m)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        open(LOG, "a").write(line + "\n")
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
    s.setdefault("offset", 0)       # bytes consumed from SRC
    s.setdefault("baselined", False)
    return s


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def ensure_schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS vehicle_sensors (
      protocol TEXT NOT NULL,
      sensor_id TEXT NOT NULL,
      first_seen TEXT NOT NULL,
      last_seen TEXT NOT NULL,
      sightings INTEGER NOT NULL DEFAULT 0,
      nights INTEGER NOT NULL DEFAULT 0,
      last_night TEXT,
      PRIMARY KEY (protocol, sensor_id)
    );
    CREATE TABLE IF NOT EXISTS vehicle_sightings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      protocol TEXT NOT NULL,
      sensor_id TEXT NOT NULL,
      pressure_psi REAL, temperature_c REAL, rssi REAL, snr REAL
    );
    CREATE INDEX IF NOT EXISTS ix_vs_ts ON vehicle_sightings(ts);
    CREATE INDEX IF NOT EXISTS ix_vs_id ON vehicle_sightings(protocol, sensor_id);
    """)
    con.commit()


def family(proto):
    if proto in FAMILY:
        return FAMILY[proto]
    base = (proto or "").split("-")[0]
    if base in FAMILY:
        return FAMILY[base]
    return ("unknown make", "decoder reported %r; not in the family table" % proto)


def ingest(con, s):
    """Read only what is new since last time. rtl_433 APPENDS to this file, so a
    byte offset is a safe cursor -- but verify it, because a truncation (log
    rotation, a fresh file) would otherwise make us skip everything."""
    if not os.path.exists(SRC):
        log("ingest: %s does not exist -- the tpms315 lane has never written" % SRC)
        return 0, []
    size = os.path.getsize(SRC)
    if size < s["offset"]:
        log("ingest: source shrank (%d < %d) -- file was rotated or truncated; "
            "re-reading from the start" % (size, s["offset"]))
        s["offset"] = 0

    new_rows, fresh = 0, []
    with open(SRC, "r", encoding="utf-8", errors="replace") as f:
        f.seek(s["offset"])
        # readline(), not `for line in f`: iteration enables a read-ahead buffer
        # and Python then refuses tell(), which is the cursor this whole design
        # rests on ("OSError: telling position disabled by next() call").
        while True:
            line = f.readline()
            if not line:
                break
            if not line.endswith("\n"):        # partial final line; leave it
                break
            s["offset"] = f.tell()
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            proto = o.get("model") or "?"
            sid = str(o.get("id"))
            ts = o.get("time") or now().isoformat(timespec="seconds")
            night = _night_of(ts)

            row = con.execute("SELECT first_seen,sightings,nights,last_night FROM "
                              "vehicle_sensors WHERE protocol=? AND sensor_id=?",
                              (proto, sid)).fetchone()
            if row is None:
                con.execute("INSERT INTO vehicle_sensors (protocol,sensor_id,first_seen,"
                            "last_seen,sightings,nights,last_night) VALUES (?,?,?,?,1,1,?)",
                            (proto, sid, ts, ts, night))
                fresh.append((proto, sid, ts))
            else:
                bump = 1 if row[3] != night else 0
                con.execute("UPDATE vehicle_sensors SET last_seen=?, sightings=sightings+1,"
                            " nights=nights+?, last_night=? WHERE protocol=? AND sensor_id=?",
                            (ts, bump, night, proto, sid))
            con.execute("INSERT INTO vehicle_sightings (ts,protocol,sensor_id,pressure_psi,"
                        "temperature_c,rssi,snr) VALUES (?,?,?,?,?,?,?)",
                        (ts, proto, sid, o.get("pressure_PSI"), o.get("temperature_C"),
                         o.get("rssi"), o.get("snr")))
            new_rows += 1
    con.commit()
    return new_rows, fresh


def _night_of(ts):
    """A night belongs to the DAY IT STARTED: 02:00 Saturday is Friday night.
    Without this a single night's traffic splits across two dates and every
    early-hours sensor looks like it appeared on a new night."""
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return "?"
    if d.hour < 12:
        d -= timedelta(days=1)
    return d.date().isoformat()


def cluster(rows):
    """rows: [(protocol, sensor_id, ts_iso)] -> list of clusters, by CO-OCCURRENCE.
    Transitive: A~B and B~C puts all three together."""
    items = []
    for proto, sid, ts in rows:
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            t = 0.0
        items.append((t, proto, sid))
    items.sort()
    out = []
    for t, proto, sid in items:
        if out and (t - out[-1]["last_t"]) <= CLUSTER_SEC:
            out[-1]["members"].append((proto, sid))
            out[-1]["last_t"] = t
        else:
            out.append({"first_t": t, "last_t": t, "members": [(proto, sid)]})
    return out


def adjacent(members):
    """Do these ids look like a factory set? Corroboration only."""
    ids = [m[1] for m in members]
    if len(ids) < 2:
        return False
    pre = os.path.commonprefix(ids)
    return len(pre) >= max(2, min(len(i) for i in ids) - 2)


def digest(con, s, hours=DIGEST_HOURS):
    cut = (now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT protocol, sensor_id, first_seen, sightings FROM vehicle_sensors "
        "WHERE first_seen >= ? ORDER BY first_seen", (cut,)).fetchall()
    total = con.execute("SELECT count(*) FROM vehicle_sensors").fetchone()[0]
    heard = con.execute("SELECT count(DISTINCT protocol||sensor_id) FROM vehicle_sightings "
                        "WHERE ts >= ?", (cut,)).fetchone()[0]

    if not rows:
        return ("Overnight vehicles: nothing new in %dh.\n%d sensor(s) heard, all "
                "already known. Known set: %d." % (hours, heard, total)), 0

    clusters = cluster([(r[0], r[1], r[2]) for r in rows])
    counts = {(r[0], r[1]): r[3] for r in rows}
    lines = ["Overnight vehicles: %d NEW wheel sensor(s) in %dh -> ~%d vehicle(s)."
             % (len(rows), hours, len(clusters)),
             "(%d sensors heard total; %d known overall)" % (heard, total), ""]
    for i, c in enumerate(clusters, 1):
        protos = sorted({m[0] for m in c["members"]})
        fam, basis = family(protos[0]) if len(protos) == 1 else ("mixed protocols", "; ".join(protos))
        when = datetime.fromtimestamp(c["first_t"], timezone.utc).astimezone()
        n = len(c["members"])
        wheels = ("%d wheels -- a full set" % n if n == 4 else
                  "%d wheel%s" % (n, "" if n == 1 else "s"))
        lines.append("%d) %s  [%s]" % (i, fam, wheels))
        lines.append("   first heard %s" % when.strftime("%a %H:%M"))
        lines.append("   basis: %s" % basis)
        if adjacent(c["members"]):
            lines.append("   ids run consecutively -- consistent with one factory set")
        ids = ", ".join("%s(x%d)" % (m[1], counts.get(m, 0)) for m in c["members"][:6])
        lines.append("   ids: %s" % ids)
        if all(counts.get(m, 0) <= 2 for m in c["members"]):
            lines.append("   heard once or twice -- passing through, not parked")
        lines.append("")
    lines.append("A TPMS id is a tyre sensor. Make is the sensor PROTOCOL's family, "
                 "not a model, and new tyres or a swapped sensor look identical to a "
                 "new vehicle.")
    return "\n".join(lines).strip(), len(clusters)


def _notify_send(text):
    """Hand the alert to the configured notifier.

    Every one of these tools used to carry its own copy of a Signal JSON-RPC
    call, with an account number baked in. Three copies meant three places to
    change a credential and three chances to leak one.
    """
    sys.path.insert(0, os.path.join(ROOT, "notify"))
    import notifiers
    return notifiers.send(cfg(), text)


def send(text):
    c = cfg()
    if not c.get("notify_enabled"):
        log("send suppressed (notify_enabled=false)")
        return
    ok, detail = _notify_send(text)
    log("sent" if ok else "SEND FAILED: %s" % (detail or "no detail")[:160])


def main():
    argv = sys.argv[1:]
    con = sqlite3.connect(DB, timeout=30)
    ensure_schema(con)
    s = load_state()

    n, fresh = ingest(con, s)
    first_run = not s["baselined"]
    s["baselined"] = True
    save_state(s)
    log("ingested %d new reading(s), %d sensor(s) not seen before" % (n, len(fresh)))

    if first_run:
        total = con.execute("SELECT count(*) FROM vehicle_sensors").fetchone()[0]
        msg = ("Vehicle watch: BASELINE ONLY. %d wheel sensors ingested from the "
               "existing log and recorded as known. This run cannot report anything "
               "as new -- there was nothing to compare against. From the next run on, "
               "new means new." % total)
        log(msg)
        if "--send" in argv:
            send(msg)
        return 0

    if "--digest" in argv:
        text, nclusters = digest(con, s)
        print(text)
        if "--send" in argv and nclusters > 0:
            send(text)
        elif "--send" in argv:
            log("nothing new -- not sending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
