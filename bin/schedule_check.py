#!/usr/bin/env python3
"""Print the profile that SHOULD be running now, or nothing if it is already right.

A manual choice from the web UI writes override_until. The scheduler must not
stomp it -- picking "event-air" during an incident only to be switched back to
watch-day 30 seconds later would make the control useless. The override expires
on its own so a forgotten event mode cannot silently become permanent.
"""
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHED = os.path.join(
    os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
    "schedule.json")
STATE = os.path.join(ROOT, "var", "state.json")


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def current_profile():
    try:
        return json.load(open(STATE)).get("profile")
    except Exception:
        return None


def wanted(now=None):
    try:
        cfg = json.load(open(SCHED))
    except Exception:
        return None, "no schedule"
    if not cfg.get("enabled"):
        return None, "schedule disabled"

    ou = cfg.get("override_until")
    if ou:
        try:
            until = datetime.fromisoformat(ou)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < until:
                return cfg.get("override_profile"), "override until %s" % ou
        except Exception:
            pass

    now = now or datetime.now()          # local time: schedules are human hours
    mins = now.hour * 60 + now.minute
    for r in cfg.get("rules", []):
        a, b = hhmm(r["from"]), hhmm(r["to"])
        inside = (a <= mins < b) if a < b else (mins >= a or mins < b)
        if inside:
            return r["profile"], "rule %s-%s" % (r["from"], r["to"])
    return None, "no rule matches"


if __name__ == "__main__":
    want, why = wanted()
    cur = current_profile()
    if "--explain" in sys.argv:
        print("current=%s wanted=%s (%s)" % (cur, want, why))
        sys.exit(0)
    if "--want" in sys.argv:
        # unconditional: what SHOULD run, change or not
        if want:
            print(want)
        sys.exit(0)
    # print only when a CHANGE is required, so the supervisor stays quiet
    if want and want != cur:
        print(want)
