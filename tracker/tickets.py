#!/usr/bin/env python3
"""Ticket tracker for bandwatch.

Scope grew past what a conversation can hold. This is the record of what is
planned, what is done, and what each thing depends on -- so nothing is quietly
dropped and "is it finished?" has an answer that is not from memory.

  tickets.py                 summary
  tickets.py list [status]   list, optionally filtered
  tickets.py show <id>       one ticket in full
  tickets.py start <id>      mark in_progress
  tickets.py done <id> [msg] mark done, with an evidence note
  tickets.py block <id> msg  mark blocked, with the reason
  tickets.py next            what is ready to work on now (deps satisfied)
"""
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "tickets.json")
GROUPS = {"A": "Aircraft intelligence", "B": "Correlation", "C": "Baselines",
          "D": "Interface", "E": "Output & integrity", "F": "Infrastructure",
          "G": "Presence sensing", "H": "Suggestions"}
MARK = {"done": "[x]", "in_progress": "[~]", "blocked": "[!]", "todo": "[ ]"}


def load():
    return json.load(open(DB))


def save(d):
    d["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = DB + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2)
    os.replace(tmp, DB)


def find(d, tid):
    for t in d["tickets"]:
        if t["id"].lower() == tid.lower():
            return t
    return None


def summary(d):
    ts = d["tickets"]
    done = [t for t in ts if t["status"] == "done"]
    prog = [t for t in ts if t["status"] == "in_progress"]
    blocked = [t for t in ts if t["status"] == "blocked"]
    todo = [t for t in ts if t["status"] == "todo"]
    pct = 100 * len(done) / max(len(ts), 1)
    bar = "#" * int(pct / 4) + "." * (25 - int(pct / 4))
    print("bandwatch tickets   %s  %d/%d done (%.0f%%)"
          % (bar, len(done), len(ts), pct))
    if prog:
        print("\nIN PROGRESS")
        for t in prog:
            print("  %s %-5s %s" % (MARK[t["status"]], t["id"], t["title"]))
    if blocked:
        print("\nBLOCKED")
        for t in blocked:
            print("  %s %-5s %s" % (MARK[t["status"]], t["id"], t["title"]))
            print("        %s" % t.get("notes", ""))
    print("\nBY GROUP")
    for g, label in GROUPS.items():
        rows = [t for t in ts if t["group"] == g]
        if not rows:
            continue
        dn = len([t for t in rows if t["status"] == "done"])
        print("  %s  %-24s %d/%d" % (g, label, dn, len(rows)))
    print("\n%d todo. `tickets.py next` for what is ready." % len(todo))


def ready(d):
    """Tickets whose dependencies are all done."""
    done = {t["id"] for t in d["tickets"] if t["status"] == "done"}
    out = []
    for t in d["tickets"]:
        if t["status"] not in ("todo",):
            continue
        deps = [x.strip() for x in (t.get("depends") or "").split(",") if x.strip()]
        if all(x in done for x in deps):
            out.append(t)
    return out


def main():
    a = sys.argv[1:]
    d = load()
    if not a:
        summary(d); return 0
    cmd = a[0]
    if cmd == "list":
        want = a[1] if len(a) > 1 else None
        for t in d["tickets"]:
            if want and t["status"] != want:
                continue
            print("  %s %-5s %-1s %-52s %s"
                  % (MARK[t["status"]], t["id"], t["effort"], t["title"][:52],
                     t["group"]))
        return 0
    if cmd == "next":
        r = ready(d)
        if not r:
            print("  nothing unblocked"); return 0
        print("READY TO BUILD (dependencies satisfied)")
        for t in r:
            print("  %-5s %-1s %s" % (t["id"], t["effort"], t["title"]))
            print("        %s" % t["why"])
        return 0
    if cmd == "show":
        t = find(d, a[1]) if len(a) > 1 else None
        if not t:
            print("  no such ticket"); return 1
        print("%s  %s" % (t["id"], t["title"]))
        print("  group   : %s (%s)" % (t["group"], GROUPS.get(t["group"], "")))
        print("  status  : %s   effort: %s" % (t["status"], t["effort"]))
        if t.get("depends"):
            print("  depends : %s" % t["depends"])
        print("  why     : %s" % t["why"])
        if t.get("notes"):
            print("  notes   : %s" % t["notes"])
        return 0
    if cmd in ("start", "done", "block"):
        if len(a) < 2:
            print("  need a ticket id"); return 1
        t = find(d, a[1])
        if not t:
            print("  no such ticket"); return 1
        t["status"] = {"start": "in_progress", "done": "done", "block": "blocked"}[cmd]
        if len(a) > 2:
            note = " ".join(a[2:])
            t["notes"] = (t.get("notes", "") + " | " + note).strip(" |")
        t["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save(d)
        print("  %s %s -> %s" % (MARK[t["status"]], t["id"], t["status"]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
