#!/usr/bin/env python3
"""Record what the system is DOING, periodically, so tuning can be evidenced.

Every tuning decision made today rested on a number somebody had to go and
measure by hand: ADS-B returning 839 events per minute against everything
else's single digits, VDL2 arriving 10 dB below what it needs, a noise floor
that moved 0.56 dB over six hours. None of that was recorded anywhere, so the
next question ("is it better now?") would have needed the same manual dig.

This writes one row per run into a `health` table: lane yields, per-kind data
rates, process state, watchdog freshness, disk, and the receiver noise floor.
Trends then answer questions that a single snapshot cannot -- did the antenna
change help, is a lane decaying, is the disk budget drifting.

Deliberately cheap and side-effect free: it reads existing tables and process
state, takes no radio, sends no alerts, and a failure in any one section is
recorded as null rather than aborting the row. A monitor that can break the
thing it monitors is worse than no monitor.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS health(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  section TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL,
  text TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health(ts);
CREATE INDEX IF NOT EXISTS idx_health_metric ON health(section, metric, ts);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe(fn, default=None):
    """A broken probe records nothing rather than killing the whole run."""
    try:
        return fn()
    except Exception:
        return default


def running(pattern):
    """Is anything matching this pattern alive, and WHAT matched?

    Scans ps rather than calling pgrep. pgrep recorded the supervisor as DOWN
    in nearly every sample while it was demonstrably running, and the single
    condition that could not be reproduced from outside was being a CHILD of
    the process being looked for, sharing its process group. Reading ps has no
    such semantics -- the matching rule lives here, in sight, instead of in a
    BSD man page.

    The [x] bracket form is stripped before matching: that trick exists only to
    stop pgrep matching its own argv, and does nothing here.

    Returns (bool, detail). The detail is stored with the sample, because a
    monitor that says "down" without saying what it looked at cannot be
    debugged -- adding it is how this bug was finally cornered.
    """
    needle = pattern.replace("[", "").replace("]", "")
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, timeout=8)
        me = str(os.getpid())
        pids = []
        for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            pid, _, cmd = line.partition(" ")
            if pid == me or "ps -axo" in cmd or needle not in cmd:
                continue
            pids.append(pid)
        return bool(pids), ",".join(pids[:6]) or "no match"
    except Exception as e:
        return False, "probe failed: %s" % str(e)[:60]


def collect(con):
    rows = []
    ts = now_iso()

    def add(section, metric, value=None, text=None):
        rows.append((ts, section, metric, value, text))

    # --- processes: the cheapest thing to get wrong and the worst to miss
    for name, pat in (("broker", "[b]roker.py"), ("collector", "[c]ollector.py"),
                      ("webui", "webui/[s]erver.py"),
                      # the full script name, not a path substring
                      ("supervisor", "[s]dr-supervise.sh"),
                      ("satwatch", "[s]dr-satwatch")):
        alive, detail = running(pat)
        add("process", name, 1.0 if alive else 0.0, "%s -> %s" % (pat, detail))

    # --- data volume by kind, last hour, so decay is visible
    cut = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    for kind, n in safe(lambda: con.execute(
            "SELECT kind, COUNT(*) FROM events WHERE ts>=? GROUP BY kind",
            (cut,)).fetchall(), []) or []:
        add("events_1h", kind or "?", float(n))
    add("events_1h", "_total", float(safe(lambda: con.execute(
        "SELECT COUNT(*) FROM events WHERE ts>=?", (cut,)).fetchone()[0], 0) or 0))

    for t, col in (("voice", "ts"), ("readings", "ts"), ("aircraft_positions", "ts")):
        add("rows_1h", t, float(safe(lambda t=t, col=col: con.execute(
            "SELECT COUNT(*) FROM %s WHERE %s>=?" % (t, col), (cut,)).fetchone()[0],
            0) or 0))

    # --- freshness: how stale is each stream right now
    for t, col in (("events", "ts"), ("voice", "ts"), ("readings", "ts"),
                   ("coverage", "ended_at")):
        v = safe(lambda t=t, col=col: con.execute(
            "SELECT MAX(%s) FROM %s" % (col, t)).fetchone()[0])
        if v:
            try:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if not d.tzinfo:
                    d = d.replace(tzinfo=timezone.utc)
                add("age_min", t,
                    round((datetime.now(timezone.utc) - d).total_seconds() / 60, 2))
            except ValueError:
                pass

    # --- lane yield: the measure every tuning decision today rested on
    try:
        sys.path.insert(0, os.path.join(ROOT, "webui"))
        import panels
        ly = panels.lane_yield(DB, ROOT, hours=24)
        for lane in ly.get("lanes", []):
            add("lane_min_24h", lane["lane"], lane["minutes"])
            add("lane_per_min", lane["lane"], lane["per_min"])
    except Exception as e:
        add("lane_yield", "_error", None, str(e)[:180])

    # --- watchdog: is the CHECKER itself alive
    w = safe(lambda: json.load(open(os.path.join(VAR, "watchdog.json"))), {}) or {}
    if w.get("last_check"):
        try:
            d = datetime.fromisoformat(w["last_check"].replace("Z", "+00:00"))
            add("watchdog", "age_min",
                round((datetime.now(timezone.utc) - d).total_seconds() / 60, 2))
        except ValueError:
            pass
    add("watchdog", "faults", float(w.get("last_fault_count") or 0))

    # --- disk against the 50 GB budget
    try:
        total = 0
        for dirpath, _dn, files in os.walk(VAR):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        add("disk", "var_mb", round(total / 1e6, 1))
    except Exception:
        pass
    try:
        du = shutil.disk_usage(ROOT)
        add("disk", "free_gb", round(du.free / 1e9, 2))
    except Exception:
        pass

    # --- feeds: contributing or merely configured
    try:
        sys.path.insert(0, os.path.join(ROOT, "webui"))
        import feeds
        for f in feeds.status(ROOT).get("feeds", []):
            add("feed", f["id"], 1.0 if f["state"] == "feeding" else 0.0,
                f["state"])
    except Exception:
        pass

    # --- dongles present. A replug reorders these; the count is what matters.
    try:
        r = subprocess.run([os.path.join(os.path.expanduser("~"), "homebrew",
                                         "bin", "rtl_test"), "-t"],
                           capture_output=True, text=True, timeout=25,
                           stdin=subprocess.DEVNULL)
        import re
        found = re.findall(r"^\s*(\d+):.*SN:\s*(\S+)", r.stdout + r.stderr, re.M)
        add("radio", "dongles", float(len(found)),
            ", ".join("%s=%s" % (i, s) for i, s in found))
    except Exception:
        pass

    return rows


def main():
    if not os.path.exists(DB):
        print("no database", file=sys.stderr)
        return 2
    con = sqlite3.connect(DB, timeout=20)
    con.executescript(SCHEMA)
    rows = collect(con)
    con.executemany(
        "INSERT INTO health(ts,section,metric,value,text) VALUES(?,?,?,?,?)", rows)
    # 90 days of history: enough to see a seasonal or antenna change, small
    # enough that it never threatens the disk budget
    con.execute("DELETE FROM health WHERE ts < ?",
                ((datetime.now(timezone.utc) - timedelta(days=90))
                 .isoformat(timespec="seconds"),))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM health").fetchone()[0]
    con.close()
    if "--quiet" not in sys.argv:
        print("%s  recorded %d metrics (%d rows retained)"
              % (now_iso(), len(rows), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
