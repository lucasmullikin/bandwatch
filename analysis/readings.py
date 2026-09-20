"""Numeric time series lifted out of decoded sensor packets.

Every 433 MHz sensor packet already lands in `events` with its full decoder JSON
in `events.raw`. A LaCrosse thermometer has reported 168 times and an inFactory
temp/humidity unit 132 times, each carrying real numbers -- and none of it can
be plotted, because the value lives inside a text summary and a blob.

This is the missing step: pull the numeric fields out at ingest into one table
that every sensor shares, so a temperature is a number with a unit and a time
rather than a sentence.

Two things it refuses to do:

* It never invents a unit. rtl_433 emits `temperature_C` and `temperature_F`
  depending on the decoder, and guessing wrong turns 88F into 88C. The unit
  comes from the field name, and an unrecognised field is skipped rather than
  stored with an assumed unit.

* It never claims a sensor is yours. Received does not mean owned -- the
  inFactory unit at 132 sightings belongs to a neighbour. Ownership comes from
  an explicit nomination file, and anything not nominated is reported as
  third-party rather than quietly presented as your own data.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  device_key TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  unit TEXT,
  quality TEXT DEFAULT 'measured',
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_dev_ts ON readings(device_key, ts);
CREATE INDEX IF NOT EXISTS idx_readings_metric_ts ON readings(metric, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_unique
  ON readings(device_key, metric, ts);
"""

# field name -> (canonical metric, unit). The unit is DECLARED here, never
# inferred at read time: rtl_433 emits temperature_C or temperature_F depending
# on the decoder, and getting that backwards turns 88F into 88C.
FIELDS = {
    "temperature_C":  ("temperature", "C"),
    "temperature_F":  ("temperature", "F"),
    "humidity":       ("humidity", "%"),
    "pressure_PSI":   ("pressure", "PSI"),
    "pressure_kPa":   ("pressure", "kPa"),
    "pressure_hPa":   ("pressure", "hPa"),
    "wind_avg_km_h":  ("wind_avg", "km/h"),
    "wind_max_km_h":  ("wind_max", "km/h"),
    "wind_dir_deg":   ("wind_dir", "deg"),
    "rain_mm":        ("rain", "mm"),
    "moisture":       ("moisture", "%"),
    "battery_ok":     ("battery_ok", "bool"),
    "rssi":           ("rssi", "dB"),
    "snr":            ("snr", "dB"),
    "noise":          ("noise", "dB"),
    "consumption":    ("consumption", "units"),
    "Consumption":    ("consumption", "units"),
}

# Metrics that describe the LINK rather than the world. Useful for judging
# whether a reading is trustworthy, but they are not environment data and the
# panel should not chart them beside temperature.
LINK_METRICS = {"rssi", "snr", "noise"}


def ensure_schema(con):
    con.executescript(SCHEMA)
    con.commit()


def parse_fields(raw, ts=None, device_key=None, source="rtl_433"):
    """Extract every recognised numeric field from one decoder JSON blob."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, dict):
        return []
    out = []
    for field, (metric, unit) in FIELDS.items():
        if field not in raw:
            continue
        v = raw[field]
        if isinstance(v, bool):
            v = 1.0 if v else 0.0
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue          # a non-numeric value is not a reading
        if v != v:            # NaN compares False against every threshold
            continue
        out.append({"ts": ts, "device_key": device_key, "metric": metric,
                    "value": v, "unit": unit, "quality": "measured",
                    "source": source})
    return out


def store(con, rows):
    """Insert readings, ignoring exact duplicates. Returns rows written."""
    if not rows:
        return 0
    ensure_schema(con)
    before = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    con.executemany(
        "INSERT OR IGNORE INTO readings(ts,device_key,metric,value,unit,"
        "quality,source) VALUES(:ts,:device_key,:metric,:value,:unit,"
        ":quality,:source)", rows)
    con.commit()
    return con.execute("SELECT COUNT(*) FROM readings").fetchone()[0] - before


def backfill(con, limit=None, kinds=("ism",)):
    """Recover readings from packets already stored in events.raw.

    The history is on disk already -- days of temperature nobody could plot.
    Idempotent: the unique index makes a second run a no-op.
    """
    ensure_schema(con)
    q = ("SELECT ts, device_key, raw FROM events WHERE kind IN (%s) "
         "AND raw IS NOT NULL" % ",".join("?" * len(kinds)))
    if limit:
        q += " LIMIT %d" % int(limit)
    rows = []
    for ts, key, raw in con.execute(q, tuple(kinds)):
        rows.extend(parse_fields(raw, ts=ts, device_key=key))
    return store(con, rows)


# ------------------------------------------------------------------ ownership

def load_owned(root):
    """Device keys the operator has explicitly claimed as their own.

    Absent by default. Receiving a sensor is not owning it, and presenting a
    neighbour's indoor temperature as your own reading would be a quiet
    falsehood, so nothing is claimed unless it is named here.
    """
    path = os.path.join(root, "sensors.json")
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except Exception:
        return set(), {}
    owned = set(cfg.get("mine") or [])
    labels = dict(cfg.get("labels") or {})
    return owned, labels


def ownership(device_key, owned):
    return "mine" if device_key in owned else "third-party"


# ------------------------------------------------------------------ queries

def _parse_ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def series(con, device_key, metric, hours=24, max_points=240):
    """One metric's history, downsampled but keeping the newest point.

    The last reading is what a panel shows as the current value, so it must
    survive downsampling exactly -- a trimmed tail would display a stale
    number as though it were now.
    """
    ensure_schema(con)
    cut = (datetime.now(timezone.utc)
           - timedelta(hours=float(hours))).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT ts, value, unit FROM readings WHERE device_key=? AND metric=? "
        "AND ts>=? ORDER BY ts", (device_key, metric, cut)).fetchall()
    if len(rows) <= max_points:
        return [list(r) for r in rows]
    step = len(rows) / float(max_points - 1)
    out = [list(rows[int(i * step)]) for i in range(max_points - 1)]
    out.append(list(rows[-1]))
    return out


def latest(con, root=None, hours=48, stale_after_min=90):
    """Current value per device+metric, with age and an explicit staleness call.

    A panel tile that shows a number without its age is making a claim it
    cannot support. Age and staleness travel with the value.
    """
    ensure_schema(con)
    owned, labels = load_owned(root) if root else (set(), {})
    cut = (datetime.now(timezone.utc)
           - timedelta(hours=float(hours))).isoformat(timespec="seconds")
    rows = con.execute(
        "SELECT r.device_key, r.metric, r.value, r.unit, r.ts "
        "FROM readings r "
        "JOIN (SELECT device_key, metric, MAX(ts) mts FROM readings "
        "      WHERE ts>=? GROUP BY device_key, metric) m "
        "  ON r.device_key=m.device_key AND r.metric=m.metric AND r.ts=m.mts "
        "ORDER BY r.device_key, r.metric", (cut,)).fetchall()

    now = datetime.now(timezone.utc)
    out = []
    for key, metric, value, unit, ts in rows:
        if metric in LINK_METRICS:
            continue
        d = _parse_ts(ts)
        age_min = None if d is None else (now - d).total_seconds() / 60.0
        out.append({
            "device_key": key,
            "label": labels.get(key),
            "owner": ownership(key, owned),
            "metric": metric, "value": value, "unit": unit, "ts": ts,
            "age_min": None if age_min is None else round(age_min, 1),
            # stale is a STATE, not an alert -- the operator asked for a panel,
            # not Signal traffic, but a two-hour-old number must still look
            # two hours old rather than current
            "stale": (age_min is None or age_min > stale_after_min),
        })
    return out


def device_summary(con, root=None, hours=24):
    """Per-device rollup for the Environment panel."""
    rows = latest(con, root=root, hours=max(hours, 48))
    by_dev = {}
    for r in rows:
        d = by_dev.setdefault(r["device_key"], {
            "device_key": r["device_key"], "label": r["label"],
            "owner": r["owner"], "metrics": [], "stale": True,
            "age_min": None})
        d["metrics"].append(r)
        if not r["stale"]:
            d["stale"] = False
        if r["age_min"] is not None:
            d["age_min"] = (r["age_min"] if d["age_min"] is None
                            else min(d["age_min"], r["age_min"]))
    out = sorted(by_dev.values(),
                 key=lambda d: (d["stale"], d["age_min"] if d["age_min"] is not None else 1e9))
    return out
