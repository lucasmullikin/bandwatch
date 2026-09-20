#!/usr/bin/env python3
"""Aircraft behaviour rules (T4).

Novelty alerting on aircraft is useless -- every overflight is a "new"
aircraft, and novelty alerting once sent 41 messages in a day (see
collector.py's aircraft_alerts / no_novelty_alert_kinds). BEHAVIOUR is the
signal: is this aircraft circling, holding low, or descending fast, not
"have we logged this hex before".

Pure stdlib + sqlite3. Read-only against aircraft_positions. Many rows carry
altitude with no lat/lon fix -- every function here skips those points for
whichever check needs a position, rather than raising.
"""
import math
from datetime import datetime, timedelta

# Mean Earth radius (IUGG), km.
EARTH_RADIUS_KM = 6371.0088

# A genuine loiter/orbit, even flown loosely, returns to roughly where it
# started. At a typical small-aircraft/helicopter orbit radius of 1-3 km
# (see detect_loiter's default below), the orbit diameter is 2-6 km. 5 km
# comfortably contains a real 360 while still rejecting a point-to-point
# transit whose course happened to swing through 270+ cumulative degrees
# (e.g. a multi-waypoint arrival with a couple of course reversals).
ORBIT_MAX_NET_DISPLACEMENT_KM = 5.0


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _bearing_deg(p1, p2):
    """Initial great-circle bearing from p1 to p2, degrees 0-360."""
    lat1, lat2 = math.radians(p1["lat"]), math.radians(p2["lat"])
    dlon = math.radians(p2["lon"] - p1["lon"])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _fixed_points(track):
    """Points with a usable lat/lon fix. See module docstring."""
    return [p for p in track if p.get("lat") is not None and p.get("lon") is not None]


def _centroid(points):
    lat = sum(p["lat"] for p in points) / len(points)
    lon = sum(p["lon"] for p in points) / len(points)
    return lat, lon


def _span_minutes(points):
    if len(points) < 2:
        return 0.0
    t0, t1 = _parse_ts(points[0]["ts"]), _parse_ts(points[-1]["ts"])
    if t0 is None or t1 is None:
        return 0.0
    return (t1 - t0).total_seconds() / 60.0


_POS_COLS = ["ts", "hex", "callsign", "alt_ft", "lat", "lon",
             "speed_kt", "track", "vert_rate", "squawk"]


def track_for(conn, hex, minutes=30):
    """Ordered position list for one aircraft, covering the `minutes` before
    its own most recent logged position.

    The window is anchored to the aircraft's last-seen ts, not wall-clock
    "now": this is offline analysis over a historical log, and a wall-clock
    window would silently return nothing for any aircraft that isn't
    overhead at the exact moment the query runs (backfills, replays, tests).
    """
    row = conn.execute(
        "SELECT MAX(ts) FROM aircraft_positions WHERE hex = ?", (hex,)
    ).fetchone()
    latest = row[0] if row else None
    if not latest:
        return []
    end_dt = _parse_ts(latest)
    if end_dt is None:
        return []
    start_iso = (end_dt - timedelta(minutes=minutes)).isoformat()

    cur = conn.execute(
        "SELECT " + ", ".join(_POS_COLS) + " "
        "FROM aircraft_positions WHERE hex = ? AND ts >= ? AND ts <= ? "
        "ORDER BY ts",
        (hex, start_iso, latest),
    )
    return [dict(zip(_POS_COLS, r)) for r in cur.fetchall()]


def detect_loiter(track, radius_km=3.0, min_minutes=8.0):
    """True when the aircraft stayed inside a small radius for a while.

    radius_km=3.0: roughly the turn diameter of a light aircraft or
    helicopter flying a standard-rate orbit at typical GA speeds (~120 kt).
    Tight enough that an ordinary cross-country transit -- even one that
    curves for routing -- won't stay confined inside it; loose enough not to
    miss a real, slightly wandering orbit.

    min_minutes=8: a light aircraft's standard-rate turn takes ~2 minutes
    per lap, so 8 minutes is about 4 orbits. Below that, a momentary cluster
    of points near an airport (approach sequencing, a single lazy turn) can
    look confined by accident; a genuine surveillance/observation loiter
    (traffic watch, law enforcement, wildfire recon) holds the pattern for
    several laps, not one.
    """
    pts = _fixed_points(track)
    if len(pts) < 2:
        return False
    lat_c, lon_c = _centroid(pts)
    max_dist = max(haversine_km(lat_c, lon_c, p["lat"], p["lon"]) for p in pts)
    return max_dist <= radius_km and _span_minutes(pts) >= min_minutes


def detect_orbit(track, min_turn_deg=270.0):
    """True when cumulative heading change is large but net displacement is
    small -- i.e. the track turned through most or all of a circle and
    ended up near where it began.

    min_turn_deg=270 (three-quarters of a full circle): above what a single
    course reversal produces (a go-around, an instrument approach procedure
    turn tops out around 180 degrees of cumulative change), but below a full
    360, so an orbit broken off early (diverted after 3/4 of a lap) still
    counts -- it's behaviourally the same loiter, just interrupted.

    Prefers each fix's own reported `track` (course over ground, as decoded
    from the aircraft, more reliable than anything recomputed from sparse
    fixes). Falls back to a bearing computed between consecutive fixes only
    when fewer than 2 points carry a track field.
    """
    pts = _fixed_points(track)
    if len(pts) < 2:
        return False

    headings = [p["track"] for p in pts if p.get("track") is not None]
    if len(headings) < 2:
        headings = [_bearing_deg(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if len(headings) < 2:
        return False

    cumulative = 0.0
    for a, b in zip(headings, headings[1:]):
        diff = (b - a + 180) % 360 - 180
        cumulative += abs(diff)

    net_km = haversine_km(pts[0]["lat"], pts[0]["lon"], pts[-1]["lat"], pts[-1]["lon"])
    return cumulative >= min_turn_deg and net_km <= ORBIT_MAX_NET_DISPLACEMENT_KM


def detect_low(track, below_ft=2500):
    """True if any point in the track reports altitude below below_ft.

    CAVEAT (confirm before wiring an alert to this): alt_ft is raw
    barometric/MSL altitude off the wire, uncorrected for local field
    elevation -- the same convention collector.py's existing
    adsb_alert_below_ft config already uses. That's fine over most terrain,
    but a station on a plateau or a high valley floor may sit at 2500-2700 ft
    MSL itself, close to this function's own default. Set below_ft to your own
    ground elevation plus the AGL height you actually care about, or this
    fires on aircraft that are level with the terrain rather than low over it.
    This is the single most common way a low-aircraft alert becomes noise.
    """
    return any(p.get("alt_ft") is not None and p["alt_ft"] < below_ft for p in track)


def detect_rapid_descent(track, ft_per_min=2000):
    """True if the aircraft's vertical rate indicates a descent steeper than
    ft_per_min at any point in the track.

    ft_per_min=2000: scheduled airline/GA descents for passenger comfort
    typically run 500-1500 ft/min. 2000 sits clearly above any normal
    arrival profile, so it won't fire on ordinary traffic, while still
    catching a genuinely steep/uncontrolled descent -- the kind of thing
    worth pairing with collector.py's existing emergency_squawk rule.

    Prefers each fix's own `vert_rate` (ft/min; negative = descending, the
    standard BaseStation/Mode-S convention dump1090 emits) since it needs no
    position fix at all -- it works even on the altitude-but-no-lat/lon rows
    called out in the module docstring. Falls back to a rate computed from
    consecutive altitude readings for rows where vert_rate is missing.
    """
    for p in track:
        vr = p.get("vert_rate")
        if vr is not None and vr <= -ft_per_min:
            return True

    alt_pts = [p for p in track if p.get("alt_ft") is not None]
    for a, b in zip(alt_pts, alt_pts[1:]):
        ta, tb = _parse_ts(a["ts"]), _parse_ts(b["ts"])
        if ta is None or tb is None:
            continue
        dt_min = (tb - ta).total_seconds() / 60.0
        if dt_min <= 0:
            continue
        rate = (a["alt_ft"] - b["alt_ft"]) / dt_min
        if rate >= ft_per_min:
            return True
    return False


def analyse(conn, hex, minutes=30):
    """List of {"rule", "detail"} for whatever behaviour rules fired over
    the last `minutes` of this aircraft's track."""
    track = track_for(conn, hex, minutes=minutes)
    findings = []

    if detect_loiter(track):
        pts = _fixed_points(track)
        lat_c, lon_c = _centroid(pts)
        max_dist = max(haversine_km(lat_c, lon_c, p["lat"], p["lon"]) for p in pts)
        findings.append({
            "rule": "loiter",
            "detail": "%s held within %.1f km for %.0f min"
                      % (hex, max_dist, _span_minutes(pts)),
        })

    if detect_orbit(track):
        findings.append({
            "rule": "orbit",
            "detail": "%s completed a sustained turn (>=270 deg) with little "
                      "net displacement" % hex,
        })

    if detect_low(track):
        low_alts = [p["alt_ft"] for p in track if p.get("alt_ft") is not None]
        findings.append({
            "rule": "low_altitude",
            "detail": "%s reported as low as %d ft" % (hex, min(low_alts)),
        })

    if detect_rapid_descent(track):
        findings.append({
            "rule": "rapid_descent",
            "detail": "%s descending faster than 2000 ft/min" % hex,
        })

    return findings
