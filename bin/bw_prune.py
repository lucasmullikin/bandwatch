#!/usr/bin/env python3
"""Disk budget enforcer for bandwatch.

Keeps the whole project under a hard byte budget, evicting in a defined order
so the least valuable bytes go first and nothing irreplaceable goes at all.

Two limits, and they are answered DIFFERENTLY:

  * budget_gb    -- our own footprint. Exceeding it is ours to fix, and the way
                    to fix it is to delete our own data.
  * min_free_gb  -- free space that must remain on the volume. A budget bigger
                    than the disk is not a budget, so this has to exist. But on
                    a shared disk it is usually somebody else's doing, and then
                    deleting our data does not free the space -- it just costs
                    us the data too. So volume pressure buys the cheap bytes and
                    raises an alarm; it does not reach the recordings.

These used to be collapsed with max(). On a real station another project filled
the volume and bandwatch answered by deleting 1,068,613 of its own event rows
over six hours to free 16 MB, against a 14-day retention. Two days survived.

NEVER evicted: preserved recordings and events (they are the copy of record),
anything inside the retention window, models/, tools/, .git/, and any code or
config. Those are either irreplaceable or a re-download, and neither is the
thing that grows.

Eviction order, cheapest regret first:
  1. var/voice/rejected/   -- clips the ingest gate already judged worthless
  2. rotated/oversized logs
  -- volume-only pressure stops here --
  3. non-preserved audio, oldest first
  4. non-preserved event rows outside retention, oldest first (then VACUUM),
     measuring what each block actually freed rather than estimating it
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")
CONFIG = os.path.join(
    os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
    "bandwatch.json")

PROTECTED = ("models", "tools", ".git", "docs", "conf", "profiles", "bin",
             "broker", "webui")


def cfg():
    d = {"budget_gb": 50.0, "min_free_gb": 15.0, "log_max_mb": 64.0}
    try:
        d.update({k: v for k, v in json.load(open(CONFIG)).items() if k in d})
    except Exception:
        pass
    return d


def tree_bytes(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def footprint():
    parts = {}
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name)
        parts[name] = tree_bytes(p) if os.path.isdir(p) else (
            os.path.getsize(p) if os.path.exists(p) else 0)
    return parts, sum(parts.values())


def free_bytes():
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize


def gb(n):
    return n / (1024 ** 3)


def protected_floor():
    """Bytes we will never evict: models, tools, git, code, config, docs.

    A budget below this floor is UNREACHABLE. Without this check the pruner
    deletes every prunable byte chasing a target it cannot hit, destroying all
    data and still failing -- observed in testing with a 0.7 GB budget against
    a 745 MB protected floor.
    """
    total = 0
    for name in os.listdir(ROOT):
        if name == "var":
            continue
        p = os.path.join(ROOT, name)
        total += tree_bytes(p) if os.path.isdir(p) else os.path.getsize(p)
    return total


def budget_sanity(c):
    """Return an error string when the configured budget cannot be honoured."""
    floor = protected_floor()
    need = floor + 0.25 * 1024 ** 3        # floor plus a working set
    if c["budget_gb"] * 1024 ** 3 < need:
        return ("budget %.2f GB is BELOW the protected floor of %.2f GB "
                "(models, tools, git, code) plus a 0.25 GB working set. "
                "Refusing to prune: pruning cannot reach this target and would "
                "delete everything for nothing."
                % (c["budget_gb"], gb(need)))
    return None


def over_by(c):
    """The two pressures, kept APART, and the footprint they were measured on.

    These used to be collapsed with max() on the reasoning that the stricter
    limit should win. They are not the same kind of fact. Exceeding our budget
    is ours to fix by deleting our data; a volume running out of free space on
    a shared disk usually is not, and the max() meant another project filling
    the Mini read here as "bandwatch must delete eleven days of events".
    """
    _, total = footprint()
    need_budget = max(total - c["budget_gb"] * 1024 ** 3, 0)
    need_volume = max(c["min_free_gb"] * 1024 ** 3 - free_bytes(), 0)
    return need_budget, need_volume, total


# How many event rows to delete between measurements. Small enough that a run
# cannot overshoot by much, large enough that the VACUUM cost is amortised.
EVICT_BLOCK = 5000

# Only used when a caller does not pass one. The collector owns the real value;
# this exists so a direct run of this tool cannot default to "no floor at all",
# which is what the absence of a floor amounted to before.
RETENTION_DEFAULT = 14


def db_bytes():
    """Size of the event store, including its WAL -- the number VACUUM moves."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(DB + suffix)
        except OSError:
            pass
    return total


def preserved_paths(con):
    return {r[0] for r in con.execute(
        "SELECT audio_path FROM voice WHERE COALESCE(preserved,0)=1 "
        "AND audio_path IS NOT NULL")}


def plan(need_budget, need_volume=0, retention_days=None, apply=False):
    """Return the eviction steps, executing them when apply=True.

    TWO pressures, and they are not interchangeable:

      need_budget   our own footprint exceeds our own budget. Ours to fix, and
                    evicting our data is the way to fix it.
      need_volume   the VOLUME is short of free space. On a shared disk this is
                    usually somebody else's doing, and deleting our data does
                    not fix it -- it just costs us the data as well.

    Conflating them cost a real station eleven days of its event history: the
    Mini's disk filled, and because the stricter limit won, bandwatch spent six
    hours deleting its own events -- 1,068,613 rows to free 16 MB -- against a
    target that was never its to reach. So volume pressure buys only the cheap
    bytes (steps 1 and 2) and then says plainly whose problem it is.

    retention_days is a FLOOR. Nothing inside the window is evicted at any
    pressure; if that means the target cannot be reached, the target cannot be
    reached, and the honest thing is to report it rather than keep deleting.
    """
    freed = 0
    steps = []
    need = max(need_budget, need_volume, 0)
    con = sqlite3.connect(DB) if os.path.exists(DB) else None
    keep = preserved_paths(con) if con else set()

    # 1. rejected clips -- already judged worthless at ingest
    rej = os.path.join(VAR, "voice", "rejected")
    if os.path.isdir(rej) and freed < need:
        files = sorted((os.path.join(rej, f) for f in os.listdir(rej)),
                       key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        n = 0
        for fp in files:
            if freed >= need:
                break
            try:
                sz = os.path.getsize(fp)
                if apply:
                    os.unlink(fp)
                freed += sz
                n += 1
            except OSError:
                pass
        if n:
            steps.append(("rejected clips", n, freed))

    # 2. oversized logs -- truncate, never delete (they are the audit trail)
    logs = os.path.join(VAR, "logs")
    cap = cfg()["log_max_mb"] * 1024 ** 2
    if os.path.isdir(logs):
        for f in sorted(os.listdir(logs)):
            fp = os.path.join(logs, f)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz > cap:
                if apply:
                    with open(fp, "rb") as fh:
                        fh.seek(-int(cap // 2), os.SEEK_END)
                        tail = fh.read()
                    with open(fp, "wb") as fh:
                        fh.write(b"--- truncated by sdr-prune ---\n" + tail)
                freed += sz - cap // 2
                steps.append(("truncated %s" % f, 1, freed))

    # Everything below deletes RECORDED DATA rather than waste. That is only
    # ever justified by our own budget: if the volume is short but we are inside
    # our budget, our bytes are not the problem and deleting them would not fix
    # it. Report it instead -- a disk filling up is worth an alert, and this is
    # the only process in a position to raise one.
    if freed < need and need_budget <= freed:
        short = gb(need_volume - freed)
        steps.append((
            "DISK PRESSURE IS NOT OURS -- refusing to evict recordings or events;"
            " the volume is %.2f GB short while bandwatch is inside its own"
            " budget. Deleting our data would not free the space someone else is"
            " using." % short, 0, freed))
        if con:
            con.close()
        return steps, freed

    # 3. non-preserved audio, oldest first
    if con and freed < need:
        rows = list(con.execute(
            "SELECT id, audio_path FROM voice "
            "WHERE COALESCE(preserved,0)=0 AND audio_path IS NOT NULL "
            "ORDER BY ts ASC"))
        n = 0
        for vid, path in rows:
            if freed >= need:
                break
            if not path or path in keep or not os.path.exists(path):
                continue
            try:
                sz = os.path.getsize(path)
                if apply:
                    os.unlink(path)
                    con.execute("DELETE FROM voice WHERE id=?", (vid,))
                freed += sz
                n += 1
            except OSError:
                pass
        if n:
            if apply:
                con.commit()
            steps.append(("non-preserved recordings", n, freed))

    # 4. non-preserved event rows OUTSIDE the retention window, oldest first.
    #
    # The window is the floor. Previously there was none: an event row was just
    # the last thing left to delete, so a target the pruner could not reach ate
    # the whole table regardless of the retention the operator had configured.
    if con and freed < need:
        days = RETENTION_DEFAULT if retention_days is None else retention_days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        evictable = con.execute(
            "SELECT COUNT(*) FROM events WHERE COALESCE(preserved,0)=0 AND ts < ?",
            (cutoff,)).fetchone()[0]
        if evictable:
            before = db_bytes()
            freed_so_far = freed
            deleted = 0
            # Delete in blocks and MEASURE, rather than estimating from a
            # bytes-per-row constant and reporting the estimate. The old code
            # carried the constant, never re-measured, and so reported "freed
            # 0.000 GB" after deleting a million rows -- which left the next
            # hourly run facing the same shortfall and doing it again.
            while deleted < evictable and freed < need:
                ids = [r[0] for r in con.execute(
                    "SELECT id FROM events WHERE COALESCE(preserved,0)=0 "
                    "AND ts < ? ORDER BY ts ASC LIMIT ?", (cutoff, EVICT_BLOCK))]
                if not ids:
                    break
                if apply:
                    con.executemany("DELETE FROM events WHERE id=?",
                                    [(i,) for i in ids])
                    con.commit()
                    con.execute("VACUUM")          # pages go back to the OS
                deleted += len(ids)
                if apply:
                    freed = freed_so_far + (before - db_bytes())
                else:
                    break                          # nothing shrank; do not spin
            if deleted:
                steps.append(("oldest event rows outside retention", deleted, freed))
        remaining_short = need - freed
        if remaining_short > 0:
            steps.append((
                "STILL %.2f GB SHORT and nothing else may go: everything left is"
                " inside the %d-day retention window or preserved. Raise the"
                " budget, lower retention_days, or move the data -- the pruner"
                " will not delete inside the window to hit a number."
                % (gb(remaining_short), days), 0, freed))
    if con:
        con.close()
    return steps, freed


def prune_presence(apply=True):
    """Enforce the presence retention rule from docs/COVERAGE.md.

    presence/fingerprint.py implements it correctly -- raw frames 24h,
    fingerprints 30 days -- and NOTHING CALLED IT. Nothing imported the
    presence package at all. A retention rule that is written down and never
    executed is not a rule, and this one governs data about people rather than
    tyre sensors, so it is the last one that should have been left declarative.
    """
    db = os.path.join(VAR, "events.db")
    if not os.path.exists(db):
        return None
    try:
        sys.path.insert(0, os.path.join(ROOT, "presence"))
        import fingerprint
    except Exception as e:
        return "presence retention UNAVAILABLE: %s" % e
    import sqlite3
    con = sqlite3.connect(db, timeout=10)
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'presence_%'")}
        if not have:
            return None          # capability not online yet; nothing to prune
        before = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                  for t in sorted(have)}
        if not apply:
            return "presence rows: %s (dry run)" % before
        fingerprint.prune(con)
        con.commit()
        after = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                 for t in sorted(have)}
        dropped = {t: before[t] - after[t] for t in before if before[t] != after[t]}
        return ("presence retention applied, dropped %s" % dropped) if dropped else None
    except Exception as e:
        return "presence retention FAILED: %s" % e
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually evict")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    c = cfg()
    parts, total = footprint()
    need_budget, need_volume, _ = over_by(c)
    need = max(need_budget, need_volume)

    if a.json:
        print(json.dumps({"config_error": budget_sanity(c),
                          "total_bytes": total, "budget_gb": c["budget_gb"],
                          "free_gb": round(gb(free_bytes()), 2),
                          "min_free_gb": c["min_free_gb"],
                          "over_by_bytes": int(need),
                          "over_budget_bytes": int(need_budget),
                          "volume_short_bytes": int(need_volume),
                          "parts": parts}))
        return 0

    msg = prune_presence(apply=a.apply)
    if msg:
        print(msg)

    print("bandwatch disk budget")
    print("  budget      : %.1f GB" % c["budget_gb"])
    print("  min free    : %.1f GB   (volume has %.1f GB free)"
          % (c["min_free_gb"], gb(free_bytes())))
    print("  footprint   : %.3f GB" % gb(total))
    for k, v in sorted(parts.items(), key=lambda x: -x[1])[:8]:
        if v > 1024 ** 2:
            print("      %-14s %8.1f MB%s"
                  % (k, v / 1024 ** 2, "  [protected]" if k in PROTECTED else ""))
    bad = budget_sanity(c)
    if bad:
        print("  STATUS      : CONFIG ERROR")
        print("  %s" % bad)
        return 2
    if need <= 0:
        print("  STATUS      : within budget, nothing to evict")
        return 0
    if need_budget > 0:
        print("  OVER BUDGET : %.3f GB   (ours to fix)" % gb(need_budget))
    if need_volume > 0:
        print("  VOLUME SHORT: %.3f GB   (the whole disk, not just us)"
              % gb(need_volume))
    steps, freed = plan(need_budget, need_volume,
                        retention_days=c.get("retention_days"), apply=a.apply)
    verb = "evicted" if a.apply else "WOULD evict"
    for what, n, _ in steps:
        print("  %s: %s x%d" % (verb, what, n))
    print("  %s %.3f GB" % ("freed" if a.apply else "would free", gb(freed)))
    if not a.apply:
        print("  (dry run -- pass --apply to act)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
