#!/usr/bin/env python3
"""Per-device temporal baselines (T7).

The console currently treats every device and every hour as equal, so
"unusual" cannot be expressed -- a device is either "seen before" or "new".
This builds a per-device occupancy histogram over hour-of-day and
day-of-week, so a repeat device that shows up somewhere it never has before
can be flagged, which is a far rarer and more useful signal than novelty:

    "a device that has only ever appeared on weekday mornings just
    appeared at 3am"

is a better alert than "new device" -- it fires once in a long while, on a
device we already trust, at the moment it stops behaving like itself.

Pure stdlib + sqlite3. Read-only against events/devices.
"""
from datetime import datetime

DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]

# --- thresholds -------------------------------------------------------
#
# Both of these gate "this device has NEVER been seen at hour/day X" claims
# on having enough prior sightings that a zero bucket means something.
# Without a gate, a device's first-ever sighting would look "anomalous" in
# 23 of 24 hour buckets and 6 of 7 day buckets simultaneously, which is
# exactly the novelty-spam problem this module exists to avoid.
#
# The two thresholds differ because the two histograms have different
# cardinality. hour_hist has 24 buckets: under a hypothetically-uniform
# arrival pattern, the chance a SPECIFIC bucket is still empty after n
# sightings is (23/24)^n. That is ~43% at n=20, ~29% at n=30, ~14% at n=50.
# 30 is picked as the point where an empty hour bucket stops being the
# likely outcome of an unremarkable device we simply haven't watched long
# enough, while still being reachable in practice (the LaCrosse sensor in
# this deployment has 82 sightings; TPMS drive-bys will accumulate slower
# and correctly report "insufficient data" until they do).
MIN_TOTAL_FOR_HOUR_BASELINE = 30

# dow_hist has only 7 buckets, so far fewer trials are needed for a zero
# bucket to be informative. 14 is two full weekly cycles -- enough that a
# day-of-week with zero sightings reflects an actual pattern (a commute, a
# scheduled job) rather than "we've only been watching for four days".
MIN_TOTAL_FOR_DOW_BASELINE = 14


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_baseline(conn, device_key, exclude_ts=None):
    """Occupancy histograms for one device from its full events history.

    exclude_ts drops one specific ts (by exact string match) from the
    histograms before they're built. This exists for rank_devices: without
    it, testing whether a device's OWN latest sighting is anomalous against
    a baseline built from all its history (including that very sighting)
    would always find the bucket non-empty, since the sighting itself put a
    1 there. Excluding it produces the baseline as it looked the instant
    before that sighting arrived, which is the question we actually want
    answered: was this a place/time this device had ever occupied before?
    """
    cur = conn.execute(
        "SELECT ts FROM events WHERE device_key = ? ORDER BY ts", (device_key,)
    )
    hour_hist = [0] * 24
    dow_hist = [0] * 7
    first_seen = None
    last_seen = None
    total = 0
    for (ts,) in cur.fetchall():
        if ts == exclude_ts:
            continue
        dt = _parse_ts(ts)
        if dt is None:
            continue
        hour_hist[dt.hour] += 1
        dow_hist[dt.weekday()] += 1
        total += 1
        if first_seen is None:
            first_seen = ts
        last_seen = ts
    return {
        "hour_hist": hour_hist,
        "dow_hist": dow_hist,
        "total": total,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


def is_anomalous(baseline, ts):
    """(bool, reason) -- True when ts falls in an hour/day this device has
    essentially never occupied, given enough history to trust that claim.

    "Essentially never" is treated as a strict zero count in the relevant
    bucket, gated by MIN_TOTAL_FOR_*_BASELINE above. Either dimension being
    a first-ever occurrence is sufficient on its own (OR, not AND): a device
    that normally runs Tuesday mornings and shows up Tuesday at 3am has a
    perfectly normal day-of-week bucket but a never-seen hour bucket, and
    that case -- an unprecedented time of day on an otherwise ordinary day
    -- is the flagship example this module is built for. Requiring both
    dimensions to be simultaneously empty would miss it.
    """
    dt = _parse_ts(ts)
    if dt is None:
        return False, "unparseable timestamp"

    total = baseline.get("total", 0)
    hour_hist = baseline.get("hour_hist") or [0] * 24
    dow_hist = baseline.get("dow_hist") or [0] * 7
    hour, dow = dt.hour, dt.weekday()

    hour_checked = total >= MIN_TOTAL_FOR_HOUR_BASELINE
    dow_checked = total >= MIN_TOTAL_FOR_DOW_BASELINE

    if hour_checked and hour_hist[hour] == 0:
        return True, (
            "device has %d sightings on record and has never once been seen "
            "at %02d:00 UTC before" % (total, hour)
        )
    if dow_checked and dow_hist[dow] == 0:
        return True, (
            "device has %d sightings on record and has never once been seen "
            "on a %s before" % (total, DOW_NAMES[dow])
        )
    if not hour_checked and not dow_checked:
        return False, (
            "only %d sightings on record -- not enough history to call "
            "anything unusual yet (need >= %d)" % (total, MIN_TOTAL_FOR_DOW_BASELINE)
        )
    return False, (
        "%02d:00 UTC seen %d times before, %s seen %d times before -- "
        "within this device's normal pattern"
        % (hour, hour_hist[hour], DOW_NAMES[dow], dow_hist[dow])
    )


def _unusualness(baseline, ts):
    """Continuous [0, 1] score used only for ranking, not by is_anomalous.

    0 means "ordinary, or we can't judge yet". 1 means "never happened
    before" (matches is_anomalous's zero-bucket trigger exactly). In
    between, it's 1 minus the observed rate of whichever dimension -- hour
    or day-of-week -- is rarer, since that's the dimension is_anomalous
    would key off if it crossed to zero. A brand-new device with no
    baseline scores 0, not 1: rank_devices ranks BEHAVIOURAL surprise, and
    ranking every new device top would just reproduce the novelty-spam
    problem this module replaces.
    """
    dt = _parse_ts(ts)
    total = baseline.get("total", 0)
    if dt is None or total < MIN_TOTAL_FOR_DOW_BASELINE:
        return 0.0
    dow_rate = baseline["dow_hist"][dt.weekday()] / total
    if total < MIN_TOTAL_FOR_HOUR_BASELINE:
        return 1.0 - dow_rate
    hour_rate = baseline["hour_hist"][dt.hour] / total
    return 1.0 - min(hour_rate, dow_rate)


def rank_devices(conn):
    """Devices ordered by how unusual their latest sighting is (most first).

    Each entry: device_key, latest_ts, anomalous, reason, score, total_prior.
    Built with the leave-one-out baseline from build_baseline(exclude_ts=...)
    so a device's own latest sighting is judged against its history up to
    that point, not against a histogram that already contains it.
    """
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT device_key FROM events WHERE device_key IS NOT NULL"
    ).fetchall()]

    results = []
    for key in keys:
        row = conn.execute(
            "SELECT ts FROM events WHERE device_key = ? ORDER BY ts DESC LIMIT 1",
            (key,),
        ).fetchone()
        if not row:
            continue
        latest_ts = row[0]
        baseline = build_baseline(conn, key, exclude_ts=latest_ts)
        anomalous, reason = is_anomalous(baseline, latest_ts)
        score = _unusualness(baseline, latest_ts)
        results.append({
            "device_key": key,
            "latest_ts": latest_ts,
            "anomalous": anomalous,
            "reason": reason,
            "score": score,
            "total_prior": baseline["total"],
        })

    # Most unusual first; ties broken by more prior history (a surprise
    # backed by 200 sightings of "normal" is more trustworthy than one
    # backed by 15).
    results.sort(key=lambda r: (r["score"], r["total_prior"]), reverse=True)
    return results
