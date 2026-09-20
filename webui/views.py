"""Read-only query layer for the map, pattern and entity views (T9/T10/T11).

Kept out of server.py deliberately: these are pure SQL-to-JSON functions with no
process control, no device access and no side effects, so they can be tested
against a synthetic database without starting a radio.

Two things this module refuses to fake:

* The map centre is DERIVED FROM THE DATA (median of received positions), not
  from a hardcoded home coordinate. The operator's address lives in a gitignored
  watchlist and does not belong in a committed source file.
* Hour-of-day patterns are reported in LOCAL time. Every ts in the database is
  UTC, and a "busy at 3pm" claim made from UTC timestamps would be wrong by the
  UTC offset -- the same class of error that already skewed rtl_433 retention
  when `-M time:iso` turned out to mean local time.
"""
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

try:                                     # local time for the pattern views
    from zoneinfo import ZoneInfo
    # UTC unless told otherwise. Quiet hours and the hourly histograms are
    # wall-clock local, so set BANDWATCH_TZ (or timezone in bandwatch.json) to
    # your own IANA zone -- an unset zone buckets your evening into someone
    # else's afternoon, which looks like a perfectly ordinary histogram.
    LOCAL = ZoneInfo(os.environ.get("BANDWATCH_TZ", "UTC"))
except Exception:                        # no tzdata available at all
    LOCAL = timezone.utc

MAX_TRACK_POINTS = 140      # per aircraft, after downsampling
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _con(db):
    con = sqlite3.connect(db, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _cut(hours):
    return (datetime.now(timezone.utc)
            - timedelta(hours=float(hours))).isoformat(timespec="seconds")


def _has(con, table):
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


# ------------------------------------------------------------------ T9 map

def _downsample(points, cap=MAX_TRACK_POINTS):
    """Keep the shape of a track while capping payload size.

    Always keeps the first and last fix -- a truncated tail would silently
    move an aircraft's last known position, which is the one thing a map
    reader trusts most.
    """
    n = len(points)
    if n <= cap:
        return points
    step = n / float(cap - 1)
    out = [points[int(i * step)] for i in range(cap - 1)]
    out.append(points[-1])
    return out


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def tracks(db, since_h=6, hex_filter=None, limit_tracks=60, min_points=2):
    """Aircraft tracks for the map, newest activity first."""
    con = _con(db)
    try:
        if not _has(con, "aircraft_positions"):
            return {"tracks": [], "center": None, "bounds": None,
                    "since_h": since_h, "note": "no position table yet"}
        args = [_cut(since_h)]
        sql = ("SELECT ts, hex, callsign, alt_ft, lat, lon, speed_kt, track, "
               "       vert_rate, squawk "
               "FROM aircraft_positions "
               "WHERE ts>=? AND lat IS NOT NULL AND lon IS NOT NULL")
        if hex_filter:
            sql += " AND hex=?"
            args.append(str(hex_filter).upper())
        sql += " ORDER BY ts ASC"
        rows = con.execute(sql, args).fetchall()

        labels = {}
        for r in con.execute("SELECT device_key,label FROM devices WHERE kind='adsb'"):
            if r["label"]:
                labels[str(r["device_key"]).upper()] = r["label"]

        by_hex = {}
        for r in rows:
            h = (r["hex"] or "").upper()
            if not h:
                continue
            by_hex.setdefault(h, []).append(r)

        out = []
        lats, lons = [], []
        for h, pts in by_hex.items():
            if len(pts) < min_points:
                continue
            alts = [p["alt_ft"] for p in pts if p["alt_ft"] is not None]
            call = next((p["callsign"] for p in reversed(pts) if p["callsign"]), None)
            sq = next((p["squawk"] for p in reversed(pts) if p["squawk"]), None)
            for p in pts:
                lats.append(p["lat"])
                lons.append(p["lon"])
            out.append({
                "hex": h,
                "label": labels.get(h),
                "callsign": (call or "").strip() or None,
                "squawk": sq,
                "n": len(pts),
                "first": pts[0]["ts"],
                "last": pts[-1]["ts"],
                "min_alt": min(alts) if alts else None,
                "max_alt": max(alts) if alts else None,
                "points": [[p["ts"], round(p["lat"], 5), round(p["lon"], 5),
                            p["alt_ft"], p["speed_kt"]]
                           for p in _downsample(pts)],
            })
        out.sort(key=lambda t: t["last"], reverse=True)
        truncated = len(out) > limit_tracks
        out = out[:limit_tracks]

        center = None
        if lats:
            center = {"lat": round(_median(lats), 5), "lon": round(_median(lons), 5),
                      "source": "median of received positions"}
        cfg = _site_from_config(db)
        if cfg:
            center = cfg
        bounds = None
        if lats:
            bounds = {"lat_min": min(lats), "lat_max": max(lats),
                      "lon_min": min(lons), "lon_max": max(lons)}
        return {"tracks": out, "center": center, "bounds": bounds,
                "since_h": since_h, "total_tracks": len(by_hex),
                "truncated": truncated}
    finally:
        con.close()


def _site_from_config(db):
    """Optional explicit site coordinates. Absent by default and NOT committed."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(db))),
                        "site.json")
    try:
        c = json.load(open(path))
        return {"lat": float(c["lat"]), "lon": float(c["lon"]),
                "source": "site.json"}
    except Exception:
        return None


# -------------------------------------------------------------- T10 patterns

def patterns(db, days=14, kinds=None):
    """Hour-of-day and day-of-week activity, in LOCAL time.

    Returns raw counts plus the observed-hours denominator, because a bucket
    with a high count may simply be a bucket the radios spent more time on --
    lanes rotate, so coverage is not uniform across the clock.
    """
    con = _con(db)
    try:
        cut = _cut(days * 24)
        rows = con.execute(
            "SELECT ts, kind FROM events WHERE ts>=?", (cut,)).fetchall()
        want = set(kinds) if kinds else None

        by_kind = {}
        dow_hour = {}
        daily = {}
        total_hour = [0] * 24
        for r in rows:
            k = r["kind"] or "?"
            if want and k not in want:
                continue
            d = _parse(r["ts"])
            if not d:
                continue
            loc = d.astimezone(LOCAL)
            h, w = loc.hour, loc.weekday()
            by_kind.setdefault(k, [0] * 24)[h] += 1
            grid = dow_hour.setdefault(k, [[0] * 24 for _ in range(7)])
            grid[w][h] += 1
            daily.setdefault(k, {}).setdefault(loc.date().isoformat(), 0)
            daily[k][loc.date().isoformat()] += 1
            total_hour[h] += 1

        # coverage denominator: seconds each device actually spent on a lane
        cov = {}
        if _has(con, "coverage"):
            for c in con.execute(
                    "SELECT lane, started_at, ended_at FROM coverage WHERE ended_at>=?",
                    (cut,)):
                a, b = _parse(c["started_at"]), _parse(c["ended_at"])
                if not a or not b or b <= a:
                    continue
                cur = a
                while cur < b:
                    loc = cur.astimezone(LOCAL)
                    nxt = min(b, (cur + timedelta(hours=1)).replace(
                        minute=0, second=0, microsecond=0))
                    cov.setdefault(c["lane"] or "?", [0.0] * 24)
                    cov[c["lane"] or "?"][loc.hour] += (nxt - cur).total_seconds()
                    cur = nxt

        busiest = max(range(24), key=lambda h: total_hour[h]) if any(total_hour) else None
        quietest = min(range(24), key=lambda h: total_hour[h]) if any(total_hour) else None
        return {
            "days": days,
            "tz": str(LOCAL),
            "hourly": by_kind,
            "dow_hour": dow_hour,
            "daily": {k: sorted(v.items()) for k, v in daily.items()},
            "total_hour": total_hour,
            "coverage_seconds_by_hour": {k: [round(x) for x in v]
                                         for k, v in cov.items()},
            "busiest_hour": busiest,
            "quietest_hour": quietest,
            "dow_labels": DOW,
            "caveat": ("counts are raw; lanes rotate, so an hour with more "
                       "events may only be an hour the radio spent there"),
        }
    finally:
        con.close()


def voice_patterns(db, days=14):
    """Same clock, but for voice -- kept separate because voice lives in its
    own table and carries a transcript quality flag worth surfacing."""
    con = _con(db)
    try:
        cut = _cut(days * 24)
        by_ch = {}
        for r in con.execute(
                "SELECT ts, channel, transcript, rejected_because "
                "FROM voice WHERE ts>=?", (cut,)):
            d = _parse(r["ts"])
            if not d:
                continue
            ch = r["channel"] or "?"
            e = by_ch.setdefault(ch, {"hours": [0] * 24, "n": 0,
                                      "transcribed": 0, "rejected": 0})
            e["hours"][d.astimezone(LOCAL).hour] += 1
            e["n"] += 1
            if r["transcript"]:
                e["transcribed"] += 1
            if r["rejected_because"]:
                e["rejected"] += 1
        return {"days": days, "tz": str(LOCAL), "channels": by_ch}
    finally:
        con.close()


# ------------------------------------------------------------- T11 entities

def entity(db, key, event_limit=60):
    """Everything the system knows about one device_key, in one payload."""
    con = _con(db)
    try:
        row = con.execute(
            "SELECT device_key, kind, first_seen, last_seen, seen_count, label "
            "FROM devices WHERE device_key=?", (key,)).fetchone()
        if not row:
            return {"error": "unknown entity", "key": key}
        e = dict(row)
        e["events"] = [dict(r) for r in con.execute(
            "SELECT id, ts, lane, kind, summary, preserved FROM events "
            "WHERE device_key=? ORDER BY ts DESC LIMIT ?",
            (key, event_limit))]
        e["event_total"] = con.execute(
            "SELECT COUNT(*) FROM events WHERE device_key=?", (key,)).fetchone()[0]
        e["alerts"] = [dict(r) for r in con.execute(
            "SELECT id, ts, rule, message, notified FROM alerts "
            "WHERE device_key=? ORDER BY ts DESC LIMIT 25", (key,))] \
            if _has(con, "alerts") else []

        if e["kind"] == "adsb":
            e["aircraft"] = _aircraft_detail(con, key)
        # VDL2 and UAT carry the ICAO hex, so their messages belong on the
        # SAME entity page as the aircraft's ADS-B track -- joined on a hex
        # match, which is a fact rather than a registry lookup or a
        # time-proximity guess.
        if e["kind"] in ("adsb", "vdl2", "uat"):
            e["datalink"] = _datalink_messages(con, key)
        elif e["kind"] == "ism":
            e["readings"] = _ism_detail(con, key)
        elif e["kind"] == "voice":
            e["voice"] = _voice_detail(con, key)

        # activity histogram across the whole retained history, local time
        hours = [0] * 24
        for r in con.execute("SELECT ts FROM events WHERE device_key=?", (key,)):
            d = _parse(r["ts"])
            if d:
                hours[d.astimezone(LOCAL).hour] += 1
        e["hours_local"] = hours
        e["tz"] = str(LOCAL)
        return e
    finally:
        con.close()


def _datalink_messages(con, key):
    """VDL2 / UAT messages for this aircraft, newest first.

    Keyed on the ICAO hex, which all three of ADS-B, VDL2 and UAT use. The
    source is kept per row: a UAT TIS-B contact is ground radar's rebroadcast
    and must not be read as the aircraft's own report.
    """
    rows = con.execute(
        "SELECT ts, kind, summary, raw FROM events WHERE device_key=? "
        "AND kind IN ('vdl2','uat') ORDER BY ts DESC LIMIT 60",
        (str(key).upper(),)).fetchall()
    out = []
    for r in rows:
        out.append({
            "ts": r["ts"],
            "source": "VDL2" if r["kind"] == "vdl2" else "UAT",
            "summary": r["summary"],
            # a rebroadcast is second-hand evidence; say so at the row level
            "rebroadcast": "TIS-B" in (r["summary"] or ""),
        })
    return out


def _aircraft_detail(con, key):
    h = str(key).upper()
    if not _has(con, "aircraft_positions"):
        return None
    n = con.execute("SELECT COUNT(*) FROM aircraft_positions WHERE hex=?",
                    (h,)).fetchone()[0]
    if not n:
        return {"positions": 0}
    r = con.execute(
        "SELECT MIN(alt_ft) lo, MAX(alt_ft) hi, MAX(speed_kt) vmax, "
        "       MIN(ts) first, MAX(ts) last FROM aircraft_positions WHERE hex=?",
        (h,)).fetchone()
    calls = [x[0] for x in con.execute(
        "SELECT DISTINCT callsign FROM aircraft_positions "
        "WHERE hex=? AND callsign IS NOT NULL AND callsign<>''", (h,))]
    sq = [x[0] for x in con.execute(
        "SELECT DISTINCT squawk FROM aircraft_positions "
        "WHERE hex=? AND squawk IS NOT NULL AND squawk<>''", (h,))]
    track = [[p["ts"], p["lat"], p["lon"], p["alt_ft"]] for p in con.execute(
        "SELECT ts,lat,lon,alt_ft FROM aircraft_positions "
        "WHERE hex=? AND lat IS NOT NULL ORDER BY ts", (h,))]
    return {"positions": n, "alt_min": r["lo"], "alt_max": r["hi"],
            "speed_max": r["vmax"], "first": r["first"], "last": r["last"],
            "callsigns": [c.strip() for c in calls if c and c.strip()],
            "squawks": sq, "track": _downsample(track, 300)}


def _ism_detail(con, key):
    """Recent decoded values, parsed out of the stored raw JSON where possible."""
    out = []
    for r in con.execute(
            "SELECT ts, raw, summary FROM events WHERE device_key=? "
            "ORDER BY ts DESC LIMIT 300", (key,)):
        rec = {"ts": r["ts"], "summary": r["summary"]}
        try:
            d = json.loads(r["raw"] or "{}")
            for f in ("temperature_C", "humidity", "battery_ok", "wind_avg_km_h",
                      "rain_mm", "pressure_hPa", "moisture"):
                if f in d:
                    rec[f] = d[f]
        except Exception:
            pass
        out.append(rec)
    return out


def _voice_detail(con, key):
    ch = key.split("/", 1)[-1] if "/" in key else key
    rows = con.execute(
        "SELECT id, ts, channel, freq_mhz, duration_s, transcript, "
        "       watchlist_hit, preserved, rejected_because, model "
        "FROM voice WHERE channel=? ORDER BY ts DESC LIMIT 80", (ch,)).fetchall()
    return [dict(r) for r in rows]


def search_entities(db, q, limit=40):
    """Free-text lookup across device_key and label, for the entity picker."""
    con = _con(db)
    try:
        like = "%" + (q or "").strip() + "%"
        return [dict(r) for r in con.execute(
            "SELECT device_key, kind, label, seen_count, last_seen FROM devices "
            "WHERE device_key LIKE ? OR IFNULL(label,'') LIKE ? "
            "ORDER BY last_seen DESC LIMIT ?", (like, like, limit))]
    finally:
        con.close()


# ------------------------------------------------------------------ geometry

def bearing_range(lat0, lon0, lat, lon):
    """Great-circle bearing (deg true) and range (nm) from site to target."""
    p0, p1 = math.radians(lat0), math.radians(lat)
    dl = math.radians(lon - lon0)
    y = math.sin(dl) * math.cos(p1)
    x = math.cos(p0) * math.sin(p1) - math.sin(p0) * math.cos(p1) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    a = (math.sin((p1 - p0) / 2) ** 2
         + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2)
    nm = 2 * 3440.065 * math.asin(min(1.0, math.sqrt(a)))
    return round(brg, 1), round(nm, 2)


# ------------------------------------------------- station recent activity

def station_activity(db, hours=24, tol_mhz=0.0006):
    """Per-frequency voice activity, for the "recently active" view on Tune.

    Keyed by frequency because that is what the station directory keys on.
    A transmission is evidence the frequency was BUSY; it is not evidence the
    transcript is right, so counts and timing are kept separate from any text.
    """
    con = _con(db)
    try:
        cut = _cut(hours)
        rows = con.execute(
            "SELECT ts, channel, freq_mhz, duration_s, transcript, "
            "       watchlist_hit, rejected_because "
            "FROM voice WHERE ts>=? AND freq_mhz IS NOT NULL ORDER BY ts",
            (cut,)).fetchall()
        by_freq = {}
        for r in rows:
            f = round(float(r["freq_mhz"]), 4)
            e = by_freq.setdefault(f, {
                "freq": f, "n": 0, "last": None, "first": None,
                "channel": r["channel"], "seconds": 0.0,
                "transcribed": 0, "rejected": 0, "watchlist": 0,
                "last_text": None})
            e["n"] += 1
            e["first"] = e["first"] or r["ts"]
            e["last"] = r["ts"]
            e["seconds"] += float(r["duration_s"] or 0)
            if r["transcript"]:
                e["transcribed"] += 1
                e["last_text"] = r["transcript"][:160]
            if r["rejected_because"]:
                e["rejected"] += 1
            if r["watchlist_hit"]:
                e["watchlist"] += 1
        for e in by_freq.values():
            e["seconds"] = round(e["seconds"], 1)
        return {"hours": hours, "by_freq": by_freq, "tol_mhz": tol_mhz}
    finally:
        con.close()


def stations_with_activity(stations, activity):
    """Merge activity onto a station directory, matching on frequency.

    Frequencies match within a tolerance rather than by equality: the directory
    stores 4dp and the recorder writes a float, and an exact-match join here
    silently produces zero hits while looking like a quiet band.
    """
    tol = activity.get("tol_mhz", 0.0006)
    by_freq = activity.get("by_freq", {})
    matched = set()
    for g in stations.get("groups", []):
        for st in g.get("stations", []):
            f = st.get("f")
            if f is None:
                continue
            best = None
            for ff, e in by_freq.items():
                if abs(ff - float(f)) <= tol:
                    if best is None or e["n"] > best["n"]:
                        best = e
            if best:
                st["activity"] = {k: best[k] for k in
                                  ("n", "last", "seconds", "transcribed",
                                   "rejected", "watchlist", "last_text", "channel")}
                matched.add(best["freq"])
    unmatched = [e for f, e in by_freq.items() if f not in matched]
    stations["activity_window_h"] = activity.get("hours")
    stations["unmatched_activity"] = sorted(
        unmatched, key=lambda e: e["last"] or "", reverse=True)
    return stations
