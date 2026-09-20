#!/usr/bin/env python3
"""Notification policy for alerts (T21).

collector.py's notify() today fires instantly on every raised alert, with a
single global per-hour budget and no idea of the hour of day. That flooded
Signal on ordinary nights and gives every rule equal standing, so a
new_carrier blip gets the same treatment as an emergency squawk. This module
is the missing decision layer in front of that send: given one alert, should
it go out now, and if not, why not.

Design choices worth stating up front, because they are judgment calls, not
facts from the schema:

  * `now` is a plain datetime and is used two ways: its wall-clock hour/
    minute decides quiet hours, and its distance from each recent alert's
    `ts` decides rate limiting. Quiet hours are inherently a LOCAL-time
    concept ("22:00-07:00"), while every ts in the database is UTC. This
    module does not do timezone-name conversion (no tz name is available in
    cfg) -- it trusts the caller to pass `now` already in local time for the
    quiet-hours check. Rate-limit deltas use `now` minus each alert's ts
    directly; if `now` is local and ts is UTC with a nonzero offset, that
    delta is off by the offset. Tests and callers should keep `now` and the
    `ts` values in `recent_alerts` on one consistent clock.

  * An alert whose rule is not in any known set defaults to "normal", never
    "critical" (which would bypass every guard) and never "low" (which
    could bury something real). This is a deliberate middle default.

  * On any unexpected error, should_notify() fails OPEN (returns True) with
    a reason naming the failure, rather than silently swallowing what might
    be a real alert. A dropped emergency alert is a worse failure than one
    extra Signal message.

Pure stdlib. Read-only -- this module makes a decision, it does not touch
the database or send anything itself.

Run the tests: python3 analysis/test_export_policy.py -v
"""
from datetime import datetime, time as time_cls, timedelta, timezone

# --- rule -> severity ----------------------------------------------------
#
# emergency_squawk, notable_aircraft, new_device, new_carrier and
# low_aircraft are the rule names collector.py's aircraft_alerts() and
# evaluate_alerts() actually insert into alerts.rule today. "watchlist_hit"
# is anticipated, not yet emitted anywhere in this codebase snapshot -- a
# transcript watchlist hit (voice.watchlist_hit) does not yet raise an
# alerts row -- but COVERAGE.md is explicit that a watchword hit "pages you
# now", so it is wired in here as critical ahead of that producer existing,
# under both the column name and a plain alias.
CRITICAL_RULES = frozenset({"emergency_squawk", "watchlist_hit", "watchlist"})
NORMAL_RULES = frozenset({"notable_aircraft", "new_device"})
LOW_RULES = frozenset({"new_carrier", "low_aircraft"})

DEFAULT_PER_RULE_LIMIT = 10
DEFAULT_GLOBAL_LIMIT = 30
RATE_WINDOW = timedelta(hours=1)


def classify(alert):
    """"critical" | "normal" | "low". Never raises: an alert of the wrong
    shape, or an unrecognised rule, classifies as "normal" rather than
    erroring.
    """
    try:
        rule = alert.get("rule") if isinstance(alert, dict) else getattr(alert, "rule", None)
        if rule in CRITICAL_RULES:
            return "critical"
        if rule in NORMAL_RULES:
            return "normal"
        if rule in LOW_RULES:
            return "low"
        return "normal"
    except Exception:
        # e.g. rule is an unhashable type and `in frozenset` raises TypeError
        return "normal"


def _parse_ts(ts):
    """str -> aware datetime, or None. Never raises."""
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_hhmm(s):
    """"HH:MM" -> datetime.time, or None on anything malformed."""
    if not isinstance(s, str):
        return None
    parts = s.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return time_cls(h, m)


def _wall_time(now):
    """Extract a comparable time-of-day from `now`, whether it's a
    datetime or already a time. Returns None if it's neither.
    """
    if isinstance(now, datetime):
        return now.time()
    if isinstance(now, time_cls):
        return now
    return None


def in_quiet_hours(now, quiet_start, quiet_end):
    """(bool, reason). Handles a window that wraps midnight
    (e.g. 22:00-07:00): if start > end, "in the window" means
    t >= start OR t < end, rather than the impossible start <= t < end.

    A missing or malformed start/end, or an unusable `now`, is treated as
    "not configured" -- i.e. NOT in quiet hours -- so a config typo fails
    open (over-notifies) rather than silently suppressing everything.
    """
    start = _parse_hhmm(quiet_start)
    end = _parse_hhmm(quiet_end)
    if start is None or end is None:
        return False, "quiet hours not configured or malformed (start=%r end=%r)" % (
            quiet_start, quiet_end)

    t = _wall_time(now)
    if t is None:
        return False, "now (%r) is not a usable time value" % (now,)

    if start == end:
        # Degenerate config (identical start/end). Treat as "no window"
        # rather than "quiet all day" -- see module docstring on failing
        # open on ambiguous config.
        return False, "quiet_start == quiet_end (%s) -- treated as no quiet window" % quiet_start

    if start < end:
        hit = start <= t < end
    else:
        hit = t >= start or t < end

    if hit:
        return True, "%s falls within quiet hours %s-%s" % (
            t.strftime("%H:%M"), quiet_start, quiet_end)
    return False, "%s is outside quiet hours %s-%s" % (
        t.strftime("%H:%M"), quiet_start, quiet_end)


def _safe_delta(now, ts_dt):
    """now - ts_dt as a timedelta, or None if they can't be compared
    (e.g. one is naive and the other aware). Never raises.
    """
    try:
        return now - ts_dt
    except TypeError:
        return None


def _count_recent(recent_alerts, now, rule=None, window=RATE_WINDOW):
    """How many entries in recent_alerts have a parseable ts within
    `window` before `now` (and matching `rule`, if given). Malformed
    entries are skipped, never fatal.
    """
    count = 0
    for a in recent_alerts or []:
        try:
            a_rule = a.get("rule") if isinstance(a, dict) else getattr(a, "rule", None)
            if rule is not None and a_rule != rule:
                continue
            ts = a.get("ts") if isinstance(a, dict) else getattr(a, "ts", None)
            dt = _parse_ts(ts)
            if dt is None:
                continue
            delta = _safe_delta(now, dt)
            if delta is None:
                continue
            if timedelta(0) <= delta <= window:
                count += 1
        except Exception:
            continue
    return count


def should_notify(alert, cfg, now, recent_alerts):
    """(bool, reason). Priority order:

      1. severity "critical" always goes through -- including during quiet
         hours, and even past the rate limits below. An emergency squawk or
         a watchword hit must never be suppressed.
      2. quiet hours (cfg["quiet_start"]/cfg["quiet_end"], "HH:MM", local
         time, may wrap midnight) -> defer any non-critical alert.
      3. per-rule rate limit: cfg["max_per_rule_per_hour"]
         (default DEFAULT_PER_RULE_LIMIT) -- at most N of the SAME rule per
         rolling hour.
      4. global rate limit: cfg["max_alerts_per_hour"]
         (default DEFAULT_GLOBAL_LIMIT) -- at most N alerts of ANY rule per
         rolling hour.

    cfg, alert and recent_alerts entries may be missing keys or malformed;
    this never raises. On a truly unexpected error it fails OPEN (notifies)
    rather than risk silently dropping a real alert.
    """
    try:
        cfg = cfg if isinstance(cfg, dict) else {}
        severity = classify(alert)
        rule = alert.get("rule") if isinstance(alert, dict) else getattr(alert, "rule", None)

        if severity == "critical":
            return True, "critical severity (rule=%r) bypasses quiet hours and rate limits" % rule

        quiet_start = cfg.get("quiet_start")
        quiet_end = cfg.get("quiet_end")
        if quiet_start is not None or quiet_end is not None:
            quiet, qreason = in_quiet_hours(now, quiet_start, quiet_end)
            if quiet:
                return False, "deferred (severity=%s): %s" % (severity, qreason)

        per_rule_limit = cfg.get("max_per_rule_per_hour", DEFAULT_PER_RULE_LIMIT)
        # rule=None means "this alert doesn't identify a rule" -- there is
        # nothing to count IT against per-rule, so skip straight to the
        # global limit rather than have rule=None collide with
        # _count_recent's own "no filter" sentinel and silently become a
        # second global check.
        if per_rule_limit is not None and rule is not None:
            per_rule_count = _count_recent(recent_alerts, now, rule=rule)
            if per_rule_count >= per_rule_limit:
                return False, (
                    "rate-limited: %d '%s' alert(s) already in the past hour "
                    "(limit %d)" % (per_rule_count, rule, per_rule_limit))

        global_limit = cfg.get("max_alerts_per_hour", DEFAULT_GLOBAL_LIMIT)
        if global_limit is not None:
            global_count = _count_recent(recent_alerts, now, rule=None)
            if global_count >= global_limit:
                return False, (
                    "rate-limited: %d alert(s) of any rule already in the "
                    "past hour (limit %d)" % (global_count, global_limit))

        return True, "delivered: severity=%s, outside quiet hours, within rate limits" % severity
    except Exception as exc:
        return True, "should_notify failed open after unexpected error: %r" % (exc,)


def _fmt_ts(ts):
    dt = _parse_ts(ts)
    return dt.strftime("%H:%M") if dt else None


def build_digest(deferred_alerts):
    """A compact multi-line summary of deferred_alerts, suitable for one
    Signal message: grouped by rule, with identical messages within a rule
    collapsed to a count rather than repeated.
    """
    if not deferred_alerts:
        return "No deferred alerts."

    groups = {}
    for a in deferred_alerts:
        try:
            rule = a.get("rule") if isinstance(a, dict) else getattr(a, "rule", None)
        except Exception:
            rule = None
        groups.setdefault(rule or "unknown", []).append(a)

    lines = ["Deferred alerts (%d):" % len(deferred_alerts)]
    for rule in sorted(groups, key=lambda r: (-len(groups[r]), r)):
        items = groups[rule]

        message_counts = {}
        order = []
        for a in items:
            try:
                msg = (a.get("message") if isinstance(a, dict) else getattr(a, "message", None)) or "(no message)"
            except Exception:
                msg = "(no message)"
            if msg not in message_counts:
                order.append(msg)
            message_counts[msg] = message_counts.get(msg, 0) + 1

        times = sorted(t for t in (
            _fmt_ts(a.get("ts") if isinstance(a, dict) else getattr(a, "ts", None))
            for a in items
        ) if t)
        window = " %s-%s" % (times[0], times[-1]) if times else ""

        if len(message_counts) == 1:
            msg = order[0]
            lines.append("- %s x%d%s: %s" % (rule, len(items), window, msg))
        else:
            lines.append("- %s x%d%s" % (rule, len(items), window))
            for msg in order:
                lines.append("    x%d %s" % (message_counts[msg], msg))

    return "\n".join(lines)
