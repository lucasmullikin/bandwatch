"""Satellite pass prediction from a TLE, stdlib only.

The weathersat module deliberately left this out and took externally-computed
passes instead, on the reasonable grounds that a hand-rolled propagator can be
plausibly wrong. But nothing on this machine computes them -- no predict, no
gpredict, no python-sgp4 -- so the lane could not be scheduled at all.

WHAT THIS IS: a Keplerian propagator with the J2 secular terms (nodal
regression and apsidal precession), evaluated in the TEME frame and converted
to topocentric look angles. That is NOT SGP4. It omits atmospheric drag,
short-period J2 oscillations and lunisolar perturbations.

WHAT THAT COSTS, stated plainly rather than hidden: for a sun-synchronous
NOAA POES satellite at ~850 km, propagating from a TLE less than a few days
old, this lands acquisition-of-signal within roughly a minute or two of a
true SGP4 solution. It degrades quickly with TLE age -- a two-week-old TLE can
be several minutes out, which for a 12-minute pass matters. So:

  * ALWAYS refresh the TLE before relying on a prediction.
  * Start the capture EARLY. `pass_window()` applies a margin for exactly this
    reason, because a pass caught late loses the top of the image and a pass
    caught early costs only disk.

The one thing this must never do is present itself as precise. Every returned
pass carries the TLE epoch age and a `precision` note.
"""
import math
from datetime import datetime, timedelta, timezone

MU = 398600.4418            # km^3/s^2, Earth gravitational parameter
RE = 6378.137               # km, equatorial radius (WGS-84)
J2 = 1.08262668e-3
FLATTENING = 1.0 / 298.257223563
SIDEREAL_DAY_S = 86164.0905
DEG = math.pi / 180.0


def _epoch_to_datetime(epoch_year, epoch_day):
    """TLE epoch: 2-digit year (57-99 => 19xx, 00-56 => 20xx) + fractional day."""
    year = 1900 + epoch_year if epoch_year >= 57 else 2000 + epoch_year
    return (datetime(year, 1, 1, tzinfo=timezone.utc)
            + timedelta(days=epoch_day - 1.0))


def parse_elements(line1, line2):
    """Pull the orbital elements out of a TLE pair.

    Fixed-column parsing, as the format requires -- splitting on whitespace
    breaks whenever a field runs into its neighbour, which happens routinely
    in the drag term and the epoch.
    """
    epoch_year = int(line1[18:20])
    epoch_day = float(line1[20:32])
    inc = float(line2[8:16]) * DEG
    raan = float(line2[17:25]) * DEG
    ecc = float("0." + line2[26:33].strip())
    argp = float(line2[34:42]) * DEG
    m0 = float(line2[43:51]) * DEG
    n = float(line2[52:63])                       # revolutions per day
    n_rad = n * 2.0 * math.pi / 86400.0           # rad/s
    a = (MU / (n_rad ** 2)) ** (1.0 / 3.0)        # km, semi-major axis
    return {
        "epoch": _epoch_to_datetime(epoch_year, epoch_day),
        "inc": inc, "raan": raan, "ecc": ecc, "argp": argp, "m0": m0,
        "n_rev_per_day": n, "n": n_rad, "a": a,
        "period_s": 2.0 * math.pi / n_rad,
        "altitude_km": a - RE,
    }


def _kepler(m, e):
    """Solve M = E - e sin E. Newton; e is tiny for these orbits."""
    E = m if e < 0.8 else math.pi
    for _ in range(30):
        d = (E - e * math.sin(E) - m) / (1 - e * math.cos(E))
        E -= d
        if abs(d) < 1e-12:
            break
    return E


def _j2_rates(el):
    """Secular nodal regression and apsidal precession, rad/s."""
    a, e, i, n = el["a"], el["ecc"], el["inc"], el["n"]
    p = a * (1 - e * e)
    k = 1.5 * J2 * n * (RE / p) ** 2
    return -k * math.cos(i), k * (2.0 - 2.5 * math.sin(i) ** 2)


def eci_at(el, when):
    """Satellite position in TEME/ECI (km) at a UTC datetime."""
    dt = (when - el["epoch"]).total_seconds()
    draan, dargp = _j2_rates(el)
    m = el["m0"] + el["n"] * dt
    raan = el["raan"] + draan * dt
    argp = el["argp"] + dargp * dt
    e = el["ecc"]
    E = _kepler(m % (2 * math.pi), e)
    nu = 2.0 * math.atan2(math.sqrt(1 + e) * math.sin(E / 2),
                          math.sqrt(1 - e) * math.cos(E / 2))
    r = el["a"] * (1 - e * math.cos(E))
    # perifocal -> ECI
    xp, yp = r * math.cos(nu), r * math.sin(nu)
    co, so = math.cos(raan), math.sin(raan)
    cw, sw = math.cos(argp), math.sin(argp)
    ci, si = math.cos(el["inc"]), math.sin(el["inc"])
    x = xp * (co * cw - so * sw * ci) - yp * (co * sw + so * cw * ci)
    y = xp * (so * cw + co * sw * ci) - yp * (so * sw - co * cw * ci)
    z = xp * (sw * si) + yp * (cw * si)
    return x, y, z


def gmst(when):
    """Greenwich mean sidereal time, radians."""
    jd = when.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    s = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
         + 0.000387933 * t * t - t * t * t / 38710000.0)
    return (s % 360.0) * DEG


def observer_eci(lat_deg, lon_deg, alt_m, when):
    lat, lon = lat_deg * DEG, lon_deg * DEG
    theta = gmst(when) + lon
    f = FLATTENING
    c = 1.0 / math.sqrt(1 + f * (f - 2) * math.sin(lat) ** 2)
    s = (1 - f) ** 2 * c
    h = alt_m / 1000.0
    rx = (RE * c + h) * math.cos(lat) * math.cos(theta)
    ry = (RE * c + h) * math.cos(lat) * math.sin(theta)
    rz = (RE * s + h) * math.sin(lat)
    return (rx, ry, rz), theta, lat


def look_angles(el, lat_deg, lon_deg, alt_m, when):
    """Elevation and azimuth (degrees) plus slant range (km)."""
    sx, sy, sz = eci_at(el, when)
    (ox, oy, oz), theta, lat = observer_eci(lat_deg, lon_deg, alt_m, when)
    rx, ry, rz = sx - ox, sy - oy, sz - oz
    st, ct = math.sin(theta), math.cos(theta)
    sl, cl = math.sin(lat), math.cos(lat)
    south = sl * ct * rx + sl * st * ry - cl * rz
    east = -st * rx + ct * ry
    up = cl * ct * rx + cl * st * ry + sl * rz
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    el_deg = math.degrees(math.asin(max(-1.0, min(1.0, up / rng))))
    az = math.degrees(math.atan2(-east, south)) + 180.0
    return el_deg, az % 360.0, rng


def next_passes(line1, line2, lat, lon, alt_m, start=None, hours=24,
                min_elevation_deg=20.0, coarse_s=30, name=None):
    """Every pass whose maximum elevation clears min_elevation_deg.

    Coarse scan then bisection on the horizon crossings. 30s steps cannot miss
    a LEO pass: the shortest usable one is several minutes long.
    """
    el = parse_elements(line1, line2)
    start = start or datetime.now(timezone.utc)
    end = start + timedelta(hours=hours)
    epoch_age_days = abs((start - el["epoch"]).total_seconds()) / 86400.0

    def elev(t):
        return look_angles(el, lat, lon, alt_m, t)[0]

    passes = []
    t = start
    prev = elev(t)
    rise = None
    while t < end:
        t2 = t + timedelta(seconds=coarse_s)
        cur = elev(t2)
        if prev < 0 <= cur:
            rise = _bisect(elev, t, t2)
        elif prev >= 0 > cur and rise is not None:
            set_t = _bisect(elev, t, t2)
            peak_t, peak_el = _peak(elev, rise, set_t)
            if peak_el >= min_elevation_deg:
                _, az_rise, _ = look_angles(el, lat, lon, alt_m, rise)
                _, az_set, _ = look_angles(el, lat, lon, alt_m, set_t)
                passes.append({
                    "satellite": name,
                    "start": rise.isoformat(timespec="seconds"),
                    "end": set_t.isoformat(timespec="seconds"),
                    "duration_s": int((set_t - rise).total_seconds()),
                    "max_elevation_deg": round(peak_el, 1),
                    "peak_at": peak_t.isoformat(timespec="seconds"),
                    "azimuth_rise": round(az_rise),
                    "azimuth_set": round(az_set),
                    "tle_epoch": el["epoch"].isoformat(timespec="seconds"),
                    "tle_age_days": round(epoch_age_days, 2),
                    "precision": (
                        "Keplerian + J2 secular, NOT SGP4. Expect roughly a "
                        "minute of error on a fresh TLE and several minutes on "
                        "one over a week old (this one is %.1f days old). "
                        "Start the capture early." % epoch_age_days),
                })
            rise = None
        prev = cur
        t = t2
    return passes


def _bisect(f, lo, hi, iters=24):
    for _ in range(iters):
        mid = lo + (hi - lo) / 2
        if (f(lo) < 0) == (f(mid) < 0):
            lo = mid
        else:
            hi = mid
    return lo + (hi - lo) / 2


def _peak(f, lo, hi, iters=40):
    """Golden-section search for the maximum elevation."""
    gr = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c = b - (b - a) * gr
    d = a + (b - a) * gr
    for _ in range(iters):
        if f(c) < f(d):
            a = c
        else:
            b = d
        c = b - (b - a) * gr
        d = a + (b - a) * gr
    t = a + (b - a) / 2
    return t, f(t)


def pass_window(p, margin_s=60):
    """Capture window with a margin, because being early costs only disk."""
    s = datetime.fromisoformat(p["start"]) - timedelta(seconds=margin_s)
    e = datetime.fromisoformat(p["end"]) + timedelta(seconds=margin_s)
    return s, e, int((e - s).total_seconds())
