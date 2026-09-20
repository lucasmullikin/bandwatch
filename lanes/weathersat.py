"""
T19 -- NOAA APT weather satellite lane.

137 MHz is inside the RTL-SDR v3's range and is the only lane in this system
that yields pictures instead of text/audio. The hard part is not decoding the
signal, it is knowing WHEN to point a dongle at 137 MHz, because this system
runs a fixed lane rotation on two exclusive dongles and a pass lasts about
10-15 minutes and cannot be rescheduled once missed.

PROPAGATION DECISION
---------------------
This module does NOT implement orbital propagation (no SGP4/SDP4, simplified
or otherwise). It accepts pre-computed pass predictions instead. Reasons:

  1. A "simplified" from-scratch propagator is exactly the kind of thing that
     produces a plausible-looking but wrong pass time -- the failure mode this
     whole ticket is trying to prevent. SGP4 has real subtleties (drag terms,
     deep-space vs near-earth switch, WGS-72 constants) that are easy to get
     subtly wrong while still returning *a* answer that looks like a pass.
  2. There is no way to validate a from-scratch propagator's accuracy inside
     this constraint set. The stdlib-only / no-network rule that's correctly
     applied to the runtime lane code also removes every independent way to
     check a self-written propagator's output (no skyfield/pyephem to diff
     against, no network fetch of a published pass time to compare to). An
     unvalidated propagator that "looks right" is worse than no propagator,
     because it hides its own error instead of surfacing it.
  3. Free, already-correct pass predictors exist and require no library in
     *this* process: the Unix `predict` CLI (van Diepen/OZ9AEC lineage),
     gpredict's CSV/JSON pass export, or a lookup against celestrak/n2yo.
     Those tools carry SGP4 implementations that are actually validated
     against published ephemerides. Re-deriving that here, unvalidated, adds
     risk without adding capability.

So: `load_passes()` below is the seam. Something outside this module (a small
script wrapping the `predict` binary, or a manual paste from gpredict) computes
AOS/LOS/max-elevation for NOAA-15/18/19 and hands it in as plain dicts. This
module's job starts there: validate the TLE that produced those predictions
wasn't corrupted, tell the operator what a given pass costs the rotation, and
build the (never-executed) capture command line.

I did not implement `next_passes(tle, lat, lon, alt_m, start, hours)` from the
ticket for the reasons above -- there is no orbital math in this file.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# --- site -------------------------------------------------------------
# From config, never hardcoded. Satellite geometry is computed FROM this point:
# a wrong coordinate does not fail, it predicts passes over somewhere else and
# looks exactly like a correct prediction.
#
# Keep it approximate. Three decimal places is a city block and buys no useful
# accuracy for a pass prediction; your exact address should not appear in
# source, in a config file you might share, or in any request built from these
# constants.
# Resolved LAZILY, on call. Reading config at import time would make this
# module unimportable -- and therefore untestable, and impossible to run in CI
# -- on any machine without a configured station.
def site():
    """(lat, lon, alt_m) of the receiving site, from config. Raises if unset."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import bandwatch_config as C
    return C.station()

# NOAA APT is FM, ~34 kHz wide, transmitted continuously (not scheduled) by
# whichever of these satellites is above the horizon.
NOAA_APT_FREQS_MHZ = {
    "NOAA-15": 137.6200,
    "NOAA-18": 137.9125,
    "NOAA-19": 137.1000,
}

# What every APT decoder (wxtoimg, noaa-apt, aptdec) expects as input.
APT_SAMPLE_RATE_HZ = 11025


# --- 1. TLE parsing + integrity check ----------------------------------

def _tle_line_checksum(line: str) -> int:
    """Mod-10 checksum over columns 1-68: digits count as themselves,
    '-' counts as 1, everything else (letters, '.', '+', spaces) counts as 0.
    """
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def _tle_line_valid(line: str) -> bool:
    if len(line) < 69:
        return False
    check_char = line[68]
    if not check_char.isdigit():
        return False
    return _tle_line_checksum(line) == int(check_char)


def parse_tle(text: str) -> dict:
    """Parse 3-line TLE text (name, line1, line2, repeated) into
    {name: (line1, line2)}.

    A satellite entry whose line1 or line2 fails its column-69 checksum is
    silently corrupt data -- it will still parse into a pass predictor and
    produce a plausible but wrong pass time. So a bad checksum causes that
    satellite to be REJECTED (excluded from the returned dict) rather than
    passed through. Malformed or empty input returns {} rather than raising --
    "no TLEs" is a normal, expected state (e.g. a stale/not-yet-fetched catalog).
    """
    if not text or not text.strip():
        return {}

    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip() != ""]

    result = {}
    i = 0
    n = len(lines)
    while i < n:
        if i + 2 >= n:
            break
        name_line, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
        if line1.startswith("1 ") and line2.startswith("2 "):
            if _tle_line_valid(line1) and _tle_line_valid(line2):
                result[name_line.strip()] = (line1, line2)
            i += 3
        else:
            # Not a 3-line record starting here -- resync by one line rather
            # than giving up on the whole file.
            i += 1
    return result


# --- pre-computed pass ingestion ----------------------------------------

def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"expected datetime or ISO8601 string, got {type(value)!r}")


def load_passes(entries):
    """Normalize externally-computed pass predictions (e.g. from the `predict`
    CLI, gpredict's pass export, or a manual n2yo lookup) into this module's
    pass shape.

    Each input entry is a dict with at least:
        satellite, freq_mhz, start, end, max_elevation_deg
    `start`/`end` may be datetimes or ISO8601 strings. An optional `device`
    key (default "0") names which dongle the capture would use.

    A malformed entry raises ValueError naming its index -- a bad prediction
    must never be silently dropped into the schedule, it has to be visible.
    """
    required = ("satellite", "freq_mhz", "start", "end", "max_elevation_deg")
    normalized = []
    for i, e in enumerate(entries):
        missing = [k for k in required if k not in e]
        if missing:
            raise ValueError(f"pass entry {i} missing fields: {missing}")
        start = _as_dt(e["start"])
        end = _as_dt(e["end"])
        if end <= start:
            raise ValueError(f"pass entry {i} has end <= start")
        elev = float(e["max_elevation_deg"])
        if not (0 <= elev <= 90):
            raise ValueError(f"pass entry {i} max_elevation_deg out of range: {elev}")
        normalized.append(
            {
                "satellite": e["satellite"],
                "freq_mhz": float(e["freq_mhz"]),
                "start": start,
                "end": end,
                "max_elevation_deg": elev,
                "device": str(e.get("device", "0")),
            }
        )
    return normalized


# --- 3. schedule conflicts ------------------------------------------------

def _overlap_seconds(a_start, a_end, b_start, b_end) -> float:
    latest_start = max(a_start, b_start)
    earliest_end = min(a_end, b_end)
    delta = (earliest_end - latest_start).total_seconds()
    return delta if delta > 0 else 0.0


def schedule_conflicts(passes, profile, cursor=None):
    """For each predicted pass, report which lanes on its device it would
    displace and for how long.

    profile shape: {"devices": {"<id>": {"lanes": [{"id":.., "seconds":..,
    "enabled": bool}, ...]}}} -- the fixed rotation this system already runs.

    cursor (optional): {"<device_id>": {"lane_index": int,
    "elapsed_seconds": number, "as_of": datetime|iso str}} -- a snapshot of
    where the live rotation currently sits on that device. Without a real
    snapshot the exact phase of a continuously-cycling rotation is unknowable
    from this module alone, so a device with no cursor entry is treated as if
    its rotation is starting fresh (lane 0, 0 elapsed) at the pass's own start
    time. That is a documented, conservative placeholder, not a measurement --
    pass the scheduler's real cursor in for exact lane-by-lane numbers.

    A pass on a device that isn't in the profile, or whose device has no
    enabled lanes, reports zero conflicts (never raises).
    """
    cursor = cursor or {}
    devices = profile.get("devices", {})
    results = []

    for p in passes:
        start = _as_dt(p["start"])
        end = _as_dt(p["end"])
        device_id = str(p.get("device", "0"))
        device = devices.get(device_id)

        entry = {
            "satellite": p.get("satellite"),
            "start": start,
            "end": end,
            "device": device_id,
            "displaced_lanes": [],
            "displaced_seconds": 0.0,
        }

        if device is None:
            results.append(entry)
            continue

        lanes = [ln for ln in device.get("lanes", []) if ln.get("enabled", True)]
        if not lanes:
            results.append(entry)
            continue

        cyc = cursor.get(device_id)
        if cyc is not None:
            lane_index = cyc.get("lane_index", 0) % len(lanes)
            elapsed = float(cyc.get("elapsed_seconds", 0.0))
            as_of = _as_dt(cyc["as_of"])
        else:
            lane_index = 0
            elapsed = 0.0
            as_of = start

        t = as_of - timedelta(seconds=elapsed)
        idx = lane_index
        displaced = {}
        guard = 0
        max_iter = 100_000  # safety cap against a misconfigured 0-second lane
        while t < end and guard < max_iter:
            lane = lanes[idx % len(lanes)]
            dur = float(lane.get("seconds", 0))
            guard += 1
            if dur <= 0:
                idx += 1
                continue
            lane_start = t
            lane_end = t + timedelta(seconds=dur)
            overlap = _overlap_seconds(lane_start, lane_end, start, end)
            if overlap > 0:
                bucket = displaced.setdefault(
                    lane["id"], {"seconds_displaced": 0.0, "occurrences": 0}
                )
                bucket["seconds_displaced"] += overlap
                bucket["occurrences"] += 1
            t = lane_end
            idx += 1

        entry["displaced_lanes"] = [
            {"lane_id": lane_id, **vals} for lane_id, vals in displaced.items()
        ]
        entry["displaced_seconds"] = sum(
            v["seconds_displaced"] for v in displaced.values()
        )
        results.append(entry)

    return results


# --- 4. capture command (argv only, never executed) ----------------------

def apt_lane_config(freq_mhz: float, seconds: int, out_dir: str, now: datetime = None):
    """Build the argv lists for an rtl_fm | sox pipeline that records `seconds`
    of a pass at `freq_mhz` to a WAV at APT_SAMPLE_RATE_HZ. Returns data --
    never spawns a process.

    Returns a dict with two SEPARATE argv lists ("rtl_fm", "sox"), meant to be
    run as two processes connected by a pipe (e.g.
    Popen(rtl_fm_argv, stdout=PIPE) -> Popen(sox_argv, stdin=that stdout)).
    Neither is a shell string, so there is no shell-injection surface even if
    out_dir ever came from less-trusted input.
    """
    if freq_mhz <= 0:
        raise ValueError("freq_mhz must be positive")
    if seconds <= 0:
        raise ValueError("seconds must be positive")

    if now is None:
        now = datetime.now(timezone.utc)

    freq_hz = int(round(freq_mhz * 1_000_000))
    in_rate = 60_000  # rtl_fm demod rate; comfortably wider than APT's ~34 kHz
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    filename = f"apt_{freq_mhz:.4f}mhz_{ts}.wav"
    out_path = os.path.join(out_dir, filename)

    rtl_fm_argv = [
        "timeout", str(int(seconds)),
        "rtl_fm",
        "-f", str(freq_hz),
        "-s", str(in_rate),
        "-g", "48",
        "-p", "0",
        "-F", "9",
        "-E", "dc",
        "-",
    ]
    # ffmpeg, not sox: sox is not installed on this machine and ffmpeg is,
    # and is already the encoder behind the live listening path. A lane whose
    # command names a missing binary fails at dispatch -- and a satellite pass
    # cannot be rescheduled, so that means losing the pass outright.
    resample_argv = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", "pipe:0",
        "-ar", str(APT_SAMPLE_RATE_HZ), "-ac", "1",
        "-c:a", "pcm_s16le", "-f", "wav", out_path,
    ]

    return {
        "rtl_fm": rtl_fm_argv,
        "resample": resample_argv,
        # kept under the old key too so any existing caller keeps working
        "sox": resample_argv,
        "output_path": out_path,
        "duration_seconds": seconds,
    }


# --- 5. pass quality -------------------------------------------------------

def pass_quality(max_elevation_deg: float):
    """Classify a pass by its maximum elevation.

    Below 20 degrees the slant path through the atmosphere is long and
    terrain/building horizon obstructions (very likely at a fixed home
    antenna, unlike a clear-horizon field station) dominate -- the resulting
    APT image is mostly noise. This threshold is the standard rule of thumb
    used across APT-decoding tools/guides (wxtoimg et al.), not something
    derived here.
    """
    if not (0 <= max_elevation_deg <= 90):
        raise ValueError(
            f"max_elevation_deg must be within [0, 90], got {max_elevation_deg}"
        )

    if max_elevation_deg < 20:
        return (
            "unusable",
            "below 20 deg: long atmospheric slant path plus likely horizon "
            "obstructions make the image mostly noise",
        )
    if max_elevation_deg < 30:
        return (
            "poor",
            "20-30 deg: usable but expect noise/fading near the pass ends",
        )
    if max_elevation_deg < 60:
        return (
            "good",
            "30-60 deg: solid signal for most of the pass",
        )
    return (
        "excellent",
        "60+ deg: near-overhead, shortest and cleanest signal path",
    )
