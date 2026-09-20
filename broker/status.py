#!/usr/bin/env python3
"""Views for bandwatch: status, lanes, events."""
import json
import sqlite3
import sys
from datetime import datetime, timezone

STALE_H = 3


def show_lanes(path):
    d = json.load(open(path))
    print(d["name"], "--", d.get("description", ""))
    for dev, spec in sorted(d["devices"].items()):
        on = [l for l in spec["lanes"] if l["enabled"]]
        cycle = sum(l["seconds"] for l in on)
        print("\nDEVICE %s -- %s   cycle=%ds (%.1f min)"
              % (dev, spec["label"], cycle, cycle / 60))
        for l in spec["lanes"]:
            print("  %s %-16s %4ds  %s"
                  % ("ON " if l["enabled"] else "off", l["id"], l["seconds"], l["note"][:64]))


def show_events(db):
    c = sqlite3.connect(db)
    print("--- events by lane ---")
    for r in c.execute("SELECT lane,kind,COUNT(*) FROM events GROUP BY lane,kind ORDER BY 3 DESC"):
        print("  %-12s %-6s %d" % r)
    print("--- devices heard (most recent) ---")
    for r in c.execute("SELECT device_key,kind,seen_count,last_seen FROM devices "
                       "ORDER BY last_seen DESC LIMIT 20"):
        print("  %-30s %-6s x%-6d %s" % r)
    print("--- recent alerts ---")
    rows = list(c.execute("SELECT ts,message,notified FROM alerts ORDER BY id DESC LIMIT 10"))
    if not rows:
        print("  none")
    for ts, msg, n in rows:
        print("  %s  %s  notified=%s" % (ts, msg, n))


def show_status(state_path, root):
    state = json.load(open(state_path))
    prof = json.load(open(os.path.join(
        os.environ.get("BANDWATCH_CONFIG") or os.path.join(root, "config"),
        "profiles", state["profile"] + ".json")))
    print("broker    : %s" % ("RUNNING pid %s" % state["pid"] if state.get("pid") else "stopped"))
    print("profile   : %s" % state["profile"])
    print("started   : %s" % state.get("started_at"))
    now = datetime.now(timezone.utc)
    bad = 0
    for dev, ds in sorted(state.get("devices", {}).items()):
        spec = {l["id"]: l for l in prof["devices"][dev]["lanes"]}
        cur = ds.get("current_lane")
        onair = ("%s (%ss dwell, since %s)" % (cur["id"], cur["dwell_s"], cur["started_at"])
                 if cur else "-- between lanes")
        print("\nDEVICE %s -- %s" % (dev, ds.get("label", "")))
        print("  cycles %s | on air: %s" % (ds.get("cycles", 0), onair))
        print("  %-16s %5s %8s %7s %6s  %s"
              % ("LANE", "RUNS", "EVENTS", "LAST", "EARLY", "HEALTH"))
        for lid, ls in ds.get("lanes", {}).items():
            sp = spec.get(lid, {})
            minph = sp.get("expect_min_events_per_hour", 0)
            last = ls.get("last_event_at")
            runs = ls.get("runs", 0)
            if sp.get("self_terminating") and ls.get("events_total", 0) > 0:
                health = "ok (self-terminating by design)"
            elif ls.get("early_exits", 0) > 0 and ls["early_exits"] >= max(runs, 1):
                health = "FAILED (exits early every run)"; bad += 1
            elif minph > 0 and ls.get("events_total", 0) == 0 and runs >= 3:
                health = "FAILED (zero events, expected >=%d/h)" % minph; bad += 1
            elif minph > 0 and last:
                age = (now - datetime.fromisoformat(last)).total_seconds() / 3600
                if age > STALE_H:
                    health = "STALE (%.1fh since an event)" % age; bad += 1
                else:
                    health = "ok"
            else:
                health = "ok" if minph > 0 else "ok (no minimum declared)"
            print("  %-16s %5d %8d %7s %6d  %s"
                  % (lid, runs, ls.get("events_total", 0),
                     "+%d" % ls.get("last_run_events", 0), ls.get("early_exits", 0), health))
    print("\nA lane logging nothing is indistinguishable from a quiet band --")
    print("that is why every lane declares a minimum rate. %d lane(s) unhealthy." % bad)


if __name__ == "__main__":
    if sys.argv[1] == "--lanes":
        show_lanes(sys.argv[2])
    elif sys.argv[1] == "--events":
        show_events(sys.argv[2])
    else:
        show_status(sys.argv[1], sys.argv[2])
