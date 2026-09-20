"""Group TPMS sensors into vehicles and track tyre pressure over time.

A car's four TPMS sensors are four unrelated device ids until something says
they belong together. sensors.json says so, and the data corroborates it:
identical sighting counts and a 10 dB signal advantage over passing traffic.

Two things this is careful about, because both are easy to get wrong and both
would produce a confident falsehood:

* SILENCE IS NOT A FAULT. TPMS transmits only while the vehicle is moving or
  has recently moved. A parked car is silent by design, and reporting "sensor
  offline" for a car sitting on the drive would be noise. The panel reports
  the vehicle as AWAY or PARKED, never as failed.

* TYRE POSITION IS UNKNOWN. The sensor id carries no position and nothing
  observed here can establish which one is front-left. They are labelled A-D
  until a human checks them against the car. Guessing would put a real
  pressure reading against the wrong wheel, which is worse than no label.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Below this, a tyre is low enough to be worth a human looking. Deliberately
# conservative: most passenger cars run 30-35 PSI cold, and a warm tyre reads
# a few PSI higher than the same tyre cold.
LOW_PSI = 28.0
# a pressure this far below the vehicle's own other tyres is worth flagging
# even when it is above the absolute floor
IMBALANCE_PSI = 4.0


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_vehicles(root):
    try:
        cfg = json.load(open(os.path.join(root, "sensors.json")))
    except Exception:
        return {}
    return cfg.get("vehicles") or {}


def vehicle_status(db, root, away_after_min=90):
    """Per-vehicle tyre state, with presence stated honestly."""
    vehicles = load_vehicles(root)
    if not vehicles:
        return []
    con = sqlite3.connect(db, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        out = []
        now = datetime.now(timezone.utc)
        labels = {}
        try:
            labels = (json.load(open(os.path.join(root, "sensors.json")))
                      .get("labels") or {})
        except Exception:
            pass

        for vid, v in vehicles.items():
            sensors = [s for s in (v.get("sensors") or [])]
            if not sensors:
                continue
            tyres, newest = [], None
            for s in sensors:
                row = con.execute(
                    "SELECT value, unit, ts FROM readings WHERE device_key=? "
                    "AND metric='pressure' ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
                temp = con.execute(
                    "SELECT value, ts FROM readings WHERE device_key=? "
                    "AND metric='temperature' ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
                d = _parse(row["ts"]) if row else None
                if d and (newest is None or d > newest):
                    newest = d
                tyres.append({
                    "sensor": s,
                    "label": labels.get(s, s.split("/")[-1]),
                    "pressure": row["value"] if row else None,
                    "unit": row["unit"] if row else None,
                    "temperature_c": temp["value"] if temp else None,
                    "ts": row["ts"] if row else None,
                    "age_min": None if not d else round((now - d).total_seconds() / 60, 1),
                })

            age = None if newest is None else (now - newest).total_seconds() / 60
            # a silent TPMS means the car is not here or not moving. It is NOT
            # a dead sensor, and calling it one would be a false alarm.
            if age is None:
                presence, why = "never seen", "no pressure reading has ever arrived"
            elif age <= 10:
                presence, why = "here, active", "reporting now"
            elif age <= away_after_min:
                presence, why = "parked", ("last heard %.0f min ago; TPMS goes "
                                           "quiet when stationary" % age)
            else:
                presence, why = "away", ("last heard %.1f h ago" % (age / 60.0))

            have = [t for t in tyres if t["pressure"] is not None]
            concerns = []
            if have:
                lo = min(t["pressure"] for t in have)
                hi = max(t["pressure"] for t in have)
                for t in have:
                    if t["pressure"] < LOW_PSI:
                        concerns.append("%s at %.2f PSI is below %.0f"
                                        % (t["label"], t["pressure"], LOW_PSI))
                if hi - lo >= IMBALANCE_PSI:
                    concerns.append("%.2f PSI spread across the set (%.2f-%.2f)"
                                    % (hi - lo, lo, hi))

            out.append({
                "id": vid,
                "label": v.get("label", vid),
                "presence": presence,
                "presence_reason": why,
                "age_min": None if age is None else round(age, 1),
                "tyres": tyres,
                "concerns": concerns,
                "note": v.get("_note"),
            })
        return out
    finally:
        con.close()


def pressure_history(db, sensor, hours=168, max_points=200):
    """One tyre's pressure over time, newest point always kept."""
    con = sqlite3.connect(db, timeout=10)
    try:
        cut = (datetime.now(timezone.utc)
               - timedelta(hours=float(hours))).isoformat(timespec="seconds")
        rows = con.execute(
            "SELECT ts, value FROM readings WHERE device_key=? AND "
            "metric='pressure' AND ts>=? ORDER BY ts", (sensor, cut)).fetchall()
        if len(rows) <= max_points:
            return [list(r) for r in rows]
        step = len(rows) / float(max_points - 1)
        out = [list(rows[int(i * step)]) for i in range(max_points - 1)]
        out.append(list(rows[-1]))       # the current value must survive exactly
        return out
    finally:
        con.close()
