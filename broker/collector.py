#!/usr/bin/env python3
"""Collector: tails lane event files, normalises, stores, and evaluates alerts.

SQLite is the source of truth. MQTT is best-effort fan-out for dashboards --
if the broker is down, events still land. Nothing here calls a model; the
alert bar is rules only, so detection keeps working when every LLM host is off.
"""
import argparse
import json
import re
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chanmap  # noqa: E402
VAR = os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")
OFFSETS = os.path.join(VAR, "offsets.json")
CONFIG = os.path.join(ROOT, "collector.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  lane TEXT NOT NULL,
  kind TEXT NOT NULL,
  device_key TEXT,
  summary TEXT,
  raw TEXT NOT NULL,
  preserved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_dev ON events(device_key);

CREATE TABLE IF NOT EXISTS devices (
  device_key TEXT PRIMARY KEY,
  kind TEXT,
  first_seen TEXT,
  last_seen TEXT,
  seen_count INTEGER DEFAULT 0,
  label TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  rule TEXT NOT NULL,
  device_key TEXT,
  message TEXT NOT NULL,
  -- 0 pending/failed, 1 delivered, 2 withheld by policy (no_notify_rules).
  -- 2 exists so a deliberately silenced alert is not counted as a delivery
  -- failure, and does not consume the hourly send budget either.
  notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS voice (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  channel TEXT,
  freq_mhz REAL,
  audio_path TEXT UNIQUE,
  duration_s REAL,
  transcript TEXT,
  transcribed_at TEXT,
  watchlist_hit TEXT,
  preserved INTEGER DEFAULT 0,
  preserved_at TEXT,
  sha256 TEXT
);
CREATE INDEX IF NOT EXISTS ix_voice_ts ON voice(ts);

-- T1: the SBS stream carries lat/lon/alt/track and the collector was
-- discarding all of it. Everything geographic depends on this table.
CREATE TABLE IF NOT EXISTS aircraft_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  hex TEXT NOT NULL,
  callsign TEXT,
  alt_ft INTEGER,
  lat REAL,
  lon REAL,
  speed_kt INTEGER,
  track REAL,
  vert_rate INTEGER,
  squawk TEXT
);
CREATE INDEX IF NOT EXISTS ix_pos_hex ON aircraft_positions(hex);
CREATE INDEX IF NOT EXISTS ix_pos_ts ON aircraft_positions(ts);

-- T3: curated notable aircraft (government, military, law enforcement,
-- air ambulance) keyed by the same ICAO hex we already receive.
CREATE TABLE IF NOT EXISTS notable_aircraft (
  hex TEXT PRIMARY KEY,
  registration TEXT, operator TEXT, type TEXT,
  mil_civ TEXT, tags TEXT, category TEXT, link TEXT
);

-- T13: which lane was on which radio, and when. The coverage standard says the
-- lane log proves the receiver was configured as claimed -- but that log is a
-- rotating text file. This makes "were we even listening then?" a query.
CREATE TABLE IF NOT EXISTS coverage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL,
  lane TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  events INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cov_time ON coverage(started_at, ended_at);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

DEFAULT_CONFIG = {
    "retention_days": 14,
    "learning_hours": 24,
    "notify_enabled": False,
    "notifier": "none",
    "notify": {},
    "max_alerts_per_hour": 4,
    "mqtt_host": "127.0.0.1",
    "mqtt_port": 1883,
    "adsb_alert_below_ft": 0,
    "lanes": {
        "ism433":    {"file": "var/events/ism433.jsonl",  "kind": "ism",   "fmt": "rtl433"},
        "tpms315":   {"file": "var/events/tpms315.jsonl", "kind": "ism",   "fmt": "rtl433"},
        "ism915":    {"file": "var/events/ism915.jsonl",  "kind": "ism",   "fmt": "rtl433"},
        "adsb":      {"file": "var/events/adsb.sbs",      "kind": "adsb",  "fmt": "sbs"}
    }
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_ts(ts):
    """Force every stored timestamp to UTC.

    rtl_433 -M time:iso emits LOCAL time with no zone; SBS carries its own.
    Mixing them silently skews retention and staleness by the UTC offset.
    """
    if not ts:
        return now()
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG):
        cfg.update(json.load(open(CONFIG)))
    return cfg


MIGRATIONS = [
    ("voice", "claimed_by", "TEXT"),
    ("voice", "claimed_at", "TEXT"),
    ("voice", "no_speech_prob", "REAL"),
    ("voice", "avg_logprob", "REAL"),
    ("voice", "compression_ratio", "REAL"),
    ("voice", "rejected_because", "TEXT"),
    ("voice", "transcribed_by", "TEXT"),
    ("voice", "model", "TEXT"),
    ("voice", "needs_quality", "INTEGER DEFAULT 0"),
    ("voice", "preserved", "INTEGER DEFAULT 0"),
    ("voice", "preserved_at", "TEXT"),
    ("voice", "sha256", "TEXT"),
    ("events", "preserved", "INTEGER DEFAULT 0"),
]


def db_connect():
    os.makedirs(VAR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS does NOT add columns to a table that already
    # exists, so a schema change is invisible on an existing database until
    # something queries the new column and fails.
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if col not in cols:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
            print("%s  migrated: %s.%s added" % (now(), table, col), flush=True)
    con.commit()
    return con


def load_offsets():
    if os.path.exists(OFFSETS):
        try:
            return json.load(open(OFFSETS))
        except Exception:
            return {}
    return {}


def save_offsets(off):
    tmp = OFFSETS + ".tmp"
    json.dump(off, open(tmp, "w"), indent=2)
    os.replace(tmp, OFFSETS)


def parse_rtl433(line):
    """rtl_433 JSON -> (device_key, summary, ts)."""
    d = json.loads(line)
    model = d.get("model", "unknown")
    ident = d.get("id", d.get("channel", ""))
    key = "%s/%s" % (model, ident)
    bits = []
    for f, label in (("temperature_C", "%.1fC"), ("humidity", "%s%%RH"),
                     ("pressure_kPa", "%skPa"), ("battery_ok", "batt=%s")):
        if f in d:
            try:
                bits.append(label % d[f])
            except Exception:
                bits.append("%s=%s" % (f, d[f]))
    summary = "%s %s" % (key, " ".join(bits)) if bits else key
    return key, summary.strip(), d.get("time")


# The three international emergency squawks. These are STRUCTURED signals: they
# cannot be mis-transcribed, cost no GPU, and cannot be missed to a quiet
# squelch. Where a structured signal exists, prefer it over ASR every time.
EMERGENCY_SQUAWKS = {
    "7500": "HIJACK",
    "7600": "RADIO FAILURE",
    "7700": "GENERAL EMERGENCY",
}


def sbs_fields(line):
    """Full BaseStation record. Fields are 1-indexed in the spec:
    5 HexIdent, 11 Callsign, 12 Altitude, 13 GroundSpeed, 14 Track,
    15 Latitude, 16 Longitude, 17 VerticalRate, 18 Squawk.
    """
    p = line.rstrip("\n").split(",")
    if len(p) < 18 or p[0] != "MSG":
        return None
    def num(i, cast):
        try:
            v = p[i].strip()
            return cast(v) if v else None
        except (ValueError, IndexError):
            return None
    hexid = p[4].strip()
    if not hexid:
        return None
    return {
        "hex": hexid,
        "callsign": (p[10].strip() or None) if len(p) > 10 else None,
        "alt_ft": num(11, int), "speed_kt": num(12, int), "track": num(13, float),
        "lat": num(14, float), "lon": num(15, float),
        "vert_rate": num(16, int),
        "squawk": (p[17].strip() or None) if len(p) > 17 else None,
    }


def parse_sbs(line):
    """BaseStation CSV -> (icao, summary, ts). Only rows carrying real data."""
    p = line.rstrip("\n").split(",")
    if len(p) < 12 or p[0] != "MSG":
        return None
    icao = p[4].strip()
    if not icao:
        return None
    alt = p[11].strip() if len(p) > 11 else ""
    spd = p[12].strip() if len(p) > 12 else ""
    call = p[10].strip() if len(p) > 10 else ""
    bits = []
    if call:
        bits.append(call)
    if alt:
        bits.append("%sft" % alt)
    if spd:
        bits.append("%skt" % spd)
    ts = None
    if len(p) > 7 and p[6] and p[7]:
        ts = "%sT%s" % (p[6].replace("/", "-"), p[7].split(".")[0])
    return icao, ("%s %s" % (icao, " ".join(bits))).strip(), ts


VOICE_RE = re.compile(r"^(?P<ch>.+?)_(?P<d>\d{8})_(?P<t>\d{6})\.(mp3|wav)$")


def load_watchlist():
    """Terms that escalate a transcript to an immediate alert.

    NOTE: these are matched AFTER transcription and are never fed to Whisper as
    an initial_prompt. Priming the model with "Cedar Hollow" makes it emit
    "Cedar Hollow" -- the hit would be manufactured by the prompt, not heard.
    """
    path = os.path.join(
        os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
        "watchlist.json")
    if not os.path.exists(path):
        return []
    try:
        return [t.lower() for t in json.load(open(path)).get("terms", []) if t.strip()]
    except Exception:
        return []


def match_watchlist(text, terms):
    if not text:
        return None
    low = text.lower()
    hits = [t for t in terms if t in low]
    return ", ".join(hits) if hits else None


def ingest_voice(con, cfg):
    """Pick up rtl_airband recordings and queue them for transcription.

    rtl_airband names files <channel>_<YYYYMMDD>_<HHMMSS>.<ext>; the channel is
    the label from the config. audio_path is UNIQUE so re-scanning is a no-op.
    """
    vdir = os.path.join(ROOT, "var", "voice")
    if not os.path.isdir(vdir):
        return 0
    # The airband configs are the authority for what frequency a channel
    # label was tuned to. Reading them here means a new conf works without
    # anyone remembering to mirror it into collector.json -- which nobody
    # ever did, leaving freq_mhz NULL on all 411 recorded transmissions
    # and making every frequency-keyed view silently return nothing.
    freqs = chanmap.load(os.path.join(ROOT, "conf"))
    freqs.update(cfg.get("channel_freq", {}))   # explicit config still wins
    cur = con.cursor()
    added = 0
    for sub in sorted(os.listdir(vdir)):
        # NEVER descend into the quarantine. Scanning it re-prefixes every file
        # with "rejected_" on each pass, so names grow without bound and the
        # clips are re-ingested as fake channels
        # ("rejected_rejected_..._murs_MURS3"). Observed live.
        if sub == "rejected":
            continue
        d = os.path.join(vdir, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            m = VOICE_RE.match(fn)
            if not m:
                continue
            full = os.path.join(d, fn)
            # skip a file rtl_airband is still writing
            if time.time() - os.path.getmtime(full) < 3:
                continue
            # A sub-second clip is a squelch opening, not a transmission.
            # QUARANTINE rather than delete, so the gate itself can be audited:
            # if real speech is landing here the threshold is wrong.
            try:
                if os.path.getsize(full) < int(cfg.get("min_clip_bytes", 3500)):
                    rej = os.path.join(ROOT, "var", "voice", "rejected")
                    os.makedirs(rej, exist_ok=True)
                    os.replace(full, os.path.join(rej, "%s_%s" % (sub, fn)))
                    continue
            except Exception:
                pass
            ch = m.group("ch")
            ts = "%s-%s-%sT%s:%s:%s+00:00" % (
                m.group("d")[0:4], m.group("d")[4:6], m.group("d")[6:8],
                m.group("t")[0:2], m.group("t")[2:4], m.group("t")[4:6])
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO voice(ts,channel,freq_mhz,audio_path,duration_s) "
                    "VALUES (?,?,?,?,?)",
                    (ts, ch, freqs.get(ch), full,
                     audio_duration_s(full)))
                added += cur.rowcount
            except Exception:
                pass
    con.commit()
    return added


def parse_uat(line):
    """dump978 JSON -> (device_key, summary, ts).

    Keyed on the ICAO hex so a UAT contact joins the same aircraft as its 1090
    track, when it has one. UAT covers general aviation below 18,000 ft, which
    1090 largely does not, so most of these are aircraft 1090 never sees.

    The rule that a TIS-B rebroadcast must never read as an aircraft's own
    report lives in broker/uat.py with its tests.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import uat as _u
    r = _u.parse_line(line)
    if not r:
        return None
    return r["device_key"], r["summary"], None


def parse_vdl2(line):
    """dumpvdl2 JSON -> (device_key, summary, ts).

    Keyed on the ICAO hex when the frame carries one, which is the whole
    reason VDL2 is worth its radio time: ADS-B keys on the same hex, so an
    aircraft text message joins to a tracked aircraft as a FACT rather than
    through a registry lookup or a time-proximity guess.

    The heavy lifting -- and the rule that a ground-station frame must never
    be attributed to an aircraft -- lives in broker/vdl2.py with its tests.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import vdl2 as _v
    r = _v.parse_line(line)
    if not r:
        return None
    ts = None
    if r.get("ts_unix"):
        ts = (datetime.fromtimestamp(r["ts_unix"], timezone.utc)
              .isoformat(timespec="seconds"))
    return r["device_key"], r["summary"], ts


def parse_acars(line):
    """acarsdec JSON -> (tail, summary, ts).

    The tail number is a real, publicly registered aircraft identifier -- unlike
    a TPMS id, it genuinely identifies the airframe, so it is the device key.
    Messages without a tail (uplinks, acks) key on the label instead.
    """
    d = json.loads(line)
    tail = (d.get("tail") or "").strip()
    label = (d.get("label") or "").strip()
    key = "acars/%s" % (tail or ("label-" + label if label else "unknown"))
    bits = []
    if tail:
        bits.append(tail)
    if label:
        bits.append("[%s]" % label)
    txt = (d.get("text") or "").replace("\n", " ").strip()
    if txt:
        bits.append(txt[:160])
    if d.get("freq"):
        bits.append("%.3f" % float(d["freq"]))
    ts = None
    t = d.get("timestamp")
    if t:
        try:
            ts = datetime.fromtimestamp(float(t), timezone.utc).isoformat(timespec="seconds")
        except Exception:
            ts = None
    return key, " ".join(bits).strip(), ts


def parse_pocsag(line):
    """multimon-ng POCSAG/FLEX text -> (capcode, summary, ts).

    POCSAG1200: Address: 1234567  Function: 3  Alpha:   TEXT
    FLEX: 2026-08-30 12:00:00 1600/2/K/A 12.345 [001234567] ALN TEXT
    """
    line = line.strip()
    if line.startswith("POCSAG"):
        proto = line.split(":", 1)[0]
        addr = ""
        m = re.search(r"Address:\s*(\d+)", line)
        if m:
            addr = m.group(1)
        text = ""
        m = re.search(r"Alpha:\s*(.*)$", line)
        if m:
            text = m.group(1).strip()
        if not addr:
            return None
        return ("pager/%s" % addr, ("%s %s %s" % (proto, addr, text)).strip(), None)
    if line.startswith("FLEX"):
        m = re.search(r"\[(\d+)\]", line)
        if not m:
            return None
        addr = m.group(1)
        text = line.split("]", 1)[1].strip() if "]" in line else ""
        return ("pager/%s" % addr, ("FLEX %s %s" % (addr, text)).strip(), None)
    return None


def ingest(con, cfg, offsets):
    """Read new bytes from each lane file. Returns list of new event rows."""
    new_rows = []
    for lane, spec in cfg["lanes"].items():
        path = os.path.join(ROOT, spec["file"])
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        pos = offsets.get(lane, 0)
        if pos > size:      # file was truncated/rotated
            pos = 0
        if pos == size:
            continue
        with open(path, "r", errors="replace") as fh:
            fh.seek(pos)
            chunk = fh.read()
            offsets[lane] = fh.tell()
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                fmt = spec["fmt"]
                if fmt == "rtl433":
                    parsed = parse_rtl433(line)
                elif fmt == "pocsag":
                    parsed = parse_pocsag(line)
                elif fmt == "acars":
                    parsed = parse_acars(line)
                elif fmt == "vdl2":
                    parsed = parse_vdl2(line)
                elif fmt == "uat":
                    parsed = parse_uat(line)
                else:
                    parsed = parse_sbs(line)
            except Exception:
                continue
            if not parsed:
                continue
            key, summary, ts = parsed
            new_rows.append((normalise_ts(ts), lane, spec["kind"], key, summary, line))
    return new_rows


def store_positions(con, raw_lines):
    """T1: persist geography from the SBS stream.

    Only rows that carry a real fix. MSG type 3 has lat/lon, type 4 has speed
    and track -- storing both and letting nulls sit is simpler and lossless
    compared with trying to merge them at ingest.
    """
    cur = con.cursor()
    n = 0
    for ts, line in raw_lines:
        f = sbs_fields(line)
        if not f:
            continue
        if f["lat"] is None and f["alt_ft"] is None and f["speed_kt"] is None:
            continue
        cur.execute(
            "INSERT INTO aircraft_positions"
            "(ts,hex,callsign,alt_ft,lat,lon,speed_kt,track,vert_rate,squawk)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, f["hex"], f["callsign"], f["alt_ft"], f["lat"], f["lon"],
             f["speed_kt"], f["track"], f["vert_rate"], f["squawk"]))
        n += 1
    con.commit()
    return n


def store_readings(con, rows):
    """Lift numeric fields out of decoder JSON into the readings table.

    Best-effort by design: a failure here must never stop ingest. Losing a
    chart is survivable; losing the packet is not.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "analysis"))
        import readings as _r
    except Exception as e:
        print("%s  readings unavailable: %s: %s"
              % (now(), type(e).__name__, e), flush=True)
        return 0
    out = []
    for ts, _lane, kind, key, _summary, raw in rows:
        if kind != "ism" or not raw:
            continue
        out.extend(_r.parse_fields(raw, ts=ts, device_key=key))
    if not out:
        return 0
    try:
        return _r.store(con, out)
    except Exception as e:
        print("%s  readings store failed: %s" % (now(), e), flush=True)
        return 0


def store(con, rows):
    cur = con.cursor()
    cur.executemany(
        "INSERT INTO events(ts,lane,kind,device_key,summary,raw) VALUES (?,?,?,?,?,?)",
        rows)
    store_readings(con, rows)
    for ts, lane, kind, key, summary, raw in rows:
        cur.execute("""INSERT INTO devices(device_key,kind,first_seen,last_seen,seen_count)
                       VALUES (?,?,?,?,1)
                       ON CONFLICT(device_key) DO UPDATE SET
                         last_seen=excluded.last_seen,
                         seen_count=devices.seen_count+1""",
                    (key, kind, ts, ts))
    con.commit()


def learning_active(con, cfg):
    """During the learning window every device is 'new' -- alerting then is noise."""
    cur = con.cursor()
    cur.execute("SELECT v FROM meta WHERE k='learning_started'")
    row = cur.fetchone()
    if not row:
        started = datetime.now(timezone.utc)
        cur.execute("INSERT INTO meta(k,v) VALUES ('learning_started',?)",
                    (started.isoformat(timespec="seconds"),))
        con.commit()
    else:
        started = datetime.fromisoformat(row[0])
    # Evaluate the window even on the first pass. Returning True unconditionally
    # here made the gate untestable and silently suppressed every first-run alert.
    ends = started + timedelta(hours=cfg["learning_hours"])
    return datetime.now(timezone.utc) < ends


def aircraft_alerts(con, cfg, rows):
    """Structured aircraft alerts: emergency squawk, and unusually low.

    Deliberately NOT novelty-based -- every overflight is a new aircraft, which
    is what flooded Signal. These fire on BEHAVIOUR, and an emergency squawk
    fires even during the learning window: a hijack code is not something to
    sit on while building a baseline.
    """
    raised = []
    cur = con.cursor()
    low_ft = int(cfg.get("adsb_alert_below_ft", 0) or 0)
    seen = set()
    for ts, lane, kind, key, summary, raw in rows:
        if kind != "adsb":
            continue
        p = raw.split(",")
        squawk = p[17].strip() if len(p) > 17 else ""
        if squawk in EMERGENCY_SQUAWKS and (key, squawk) not in seen:
            seen.add((key, squawk))
            raised.append(("emergency_squawk", key,
                           "AIRCRAFT EMERGENCY: %s squawking %s (%s)"
                           % (key, squawk, EMERGENCY_SQUAWKS[squawk])))
            continue
        if low_ft:
            alt = p[11].strip() if len(p) > 11 else ""
            try:
                # alt_ft is uncorrected MSL. This receiver sits at ~2,871 ft MSL
                # (the reference airport field elevation), so a naive "below 2500 ft" threshold is
                # BELOW GROUND here and would fire on nothing, or on everything
                # once terrain is ignored. low_ft is configured as
                # site_elevation_ft + adsb_alert_agl_ft so it means AGL.
                if alt and int(alt) < low_ft and (key, "low") not in seen:
                    seen.add((key, "low"))
                    ok, why = _low_alert_allowed(con, cfg, key)
                    if not ok:
                        print("%s  low_aircraft %s suppressed: %s"
                              % (now(), key, why), flush=True)
                    else:
                        agl = int(alt) - int(cfg.get("site_elevation_ft", 0) or 0)
                        raised.append(("low_aircraft", key,
                                       "Low aircraft: %s at %s ft MSL (~%d ft AGL)"
                                       % (key, alt, agl)))
            except ValueError:
                pass
    for rule, key, msg in raised:
        cur.execute("INSERT INTO alerts(ts,rule,device_key,message) VALUES (?,?,?,?)",
                    (now(), rule, key, msg))
    con.commit()
    return raised


def evaluate_alerts(con, cfg, rows):
    """Rules only. No model. Returns alerts raised."""
    # structured aircraft alerts are exempt from the learning window
    air = aircraft_alerts(con, cfg, rows) + enrich_aircraft(con, rows)
    if learning_active(con, cfg):
        return air
    cur = con.cursor()
    raised = []
    seen_this_pass = set()
    # "New device" is meaningless for aircraft -- every plane overhead is new,
    # which sent 41 Signal messages in a day. What matters for aircraft is
    # BEHAVIOUR (loitering, low, emergency squawk), not novelty.
    no_novelty_alert = set(cfg.get("no_novelty_alert_kinds", ["adsb"]))
    for ts, lane, kind, key, summary, raw in rows:
        if kind in no_novelty_alert:
            continue
        if key in seen_this_pass:
            continue
        # A UAT ground station is INFRASTRUCTURE, not a device that appeared.
        # FIS-B is a weather uplink; a TIS-B target is the ground radar's view
        # of someone else's aircraft, rebroadcast. Announcing either as a "new
        # device" names a thing that does not exist -- one of them was reported
        # as a device called "3000ft", which is an altitude, not an identity.
        if _is_ground_infrastructure(key, summary):
            continue
        cur.execute("SELECT seen_count, first_seen FROM devices WHERE device_key=?", (key,))
        r = cur.fetchone()
        if not r:
            continue
        seen_count, first_seen = r
        # a device whose ONLY sightings are in this batch is genuinely new
        if seen_count <= len([x for x in rows if x[3] == key]):
            seen_this_pass.add(key)
            raised.append(("new_device", key,
                           "New %s device: %s (lane %s)" % (kind, summary, lane)))
    for rule, key, msg in raised:
        cur.execute("INSERT INTO alerts(ts,rule,device_key,message) VALUES (?,?,?,?)",
                    (now(), rule, key, msg))
    con.commit()
    return air + raised



def _is_ground_infrastructure(key, summary):
    """True for datalink ground stations, which are not devices at all.

    Keyed on the parser's own markers rather than on free text where possible:
    uat.py assigns FIS-B the synthetic key "uat/fisb", and marks a rebroadcast
    in the summary because the whole point of that label is that the report did
    NOT come from the aircraft.
    """
    k = (key or "").lower()
    if k.startswith("uat/") or k.startswith("vdl2/gs-"):
        return True
    return "TIS-B:" in (summary or "")


def _nm_between(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    import math
    a1, o1, a2, o2 = map(math.radians, (lat1, lon1, lat2, lon2))
    h = (math.sin((a2 - a1) / 2) ** 2
         + math.cos(a1) * math.cos(a2) * math.sin((o2 - o1) / 2) ** 2)
    return 2 * math.asin(math.sqrt(h)) * 3440.065


def _low_alert_allowed(con, cfg, key):
    """Is this genuinely notable, or just an aircraft using the airport?

    Returns (allowed, why_not). Suppresses when the aircraft is near the field,
    when its position is unknown, or when it already alerted recently.
    """
    from datetime import datetime, timedelta, timezone
    nm_limit = float(cfg.get("adsb_alert_airport_nm", 0) or 0)
    repeat = float(cfg.get("adsb_alert_repeat_min", 0) or 0)

    if repeat:
        since = (datetime.now(timezone.utc)
                 - timedelta(minutes=repeat)).isoformat(timespec="seconds")
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM alerts WHERE rule='low_aircraft' "
                "AND device_key=? AND ts>=?", (key, since)).fetchone()[0]
        except Exception:
            n = 0
        if n:
            return False, "already alerted within %.0f min" % repeat

    if not nm_limit:
        return True, ""                      # airport filtering switched off

    alat = cfg.get("adsb_alert_airport_lat")
    alon = cfg.get("adsb_alert_airport_lon")
    if alat is None or alon is None:
        return True, ""
    try:
        row = con.execute(
            "SELECT lat, lon FROM aircraft_positions WHERE hex=? "
            "AND lat IS NOT NULL AND lon IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (key,)).fetchone()
    except Exception:
        row = None
    if not row:
        # Position is known for only ~30% of ADS-B rows. Two miles from a
        # commercial field, low-and-unlocated is almost certainly approach
        # traffic; alerting anyway is what buried the channel.
        return False, "no position, and this receiver sits beside an airport"
    d = _nm_between(row[0], row[1], float(alat), float(alon))
    if d <= nm_limit:
        return False, "%.1f nm from the field -- routine approach/departure" % d
    return True, ""



def audio_duration_s(path):
    """Real duration of a clip, or None.

    rtl_airband writes VARIABLE bitrate mp3 (measured: 12.5-17 kbps on
    consecutive clips), so estimating from file size cannot work -- that is
    what made 72% of stored durations wrong, some by 10x. Returns None rather
    than a guess when it cannot be measured: the transcription queue treats
    NULL as "unknown, let it through", which is safe, while a wrong number
    silently mis-gates.
    """
    for probe in (os.path.join(os.path.expanduser("~"), "homebrew", "bin", "ffprobe"),
                  "ffprobe"):
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=15)
            v = (r.stdout or "").strip()
            if v:
                d = float(v)
                if d > 0:
                    return round(d, 2)
        except Exception:
            continue
    return None

def _is_critical(rule):
    try:
        sys.path.insert(0, os.path.join(ROOT, "analysis"))
        import alert_policy
        return alert_policy.classify({"rule": rule}) == "critical"
    except Exception:
        return rule in ("emergency_squawk", "watchlist_hit")


def signal_send(cfg, text):
    """Hand the alert to whichever notifier is configured.

    Named for history -- it is no longer Signal-specific, and no credential or
    endpoint is baked in here any more. See notify/README.md.

    Returns the backend's detail string. The caller treats an empty or
    error-shaped response as a failure, so a notifier that cannot deliver
    never silently marks an alert as sent.
    """
    sys.path.insert(0, os.path.join(ROOT, "notify"))
    try:
        import notifiers
    except Exception as e:                  # noqa: BLE001
        return '{"error": "notifier unavailable: %s"}' % e
    ok, detail = notifiers.send(cfg, text)
    return detail if ok else '{"error": %s}' % json.dumps(detail or "send failed")


def _local_now(cfg):
    """Quiet hours are wall-clock local; every ts in the DB is UTC.

    Handing UTC to a "quiet after 22:00" rule silences the wrong six hours.
    """
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo(cfg.get("timezone", "UTC")))
    except Exception:
        return _dt.now(timezone.utc)


def apply_policy(con, cfg, raised):
    """Split raised alerts into (send now, defer to digest) using T21 policy.

    Falls back to sending everything if the policy module is unavailable --
    losing severity ordering is survivable, silently dropping alerts is not.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "analysis"))
        import alert_policy
    except Exception as e:
        print("%s  alert policy unavailable (%s) -- sending unfiltered"
              % (now(), e), flush=True)
        return raised, []

    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = [{"rule": r[0], "ts": r[1]} for r in con.execute(
        "SELECT rule, ts FROM alerts WHERE notified=1 AND ts > ?", (hour_ago,))]
    local = _local_now(cfg)
    send, defer = [], []
    for item in raised:
        rule, key, msg = item
        ok, why = alert_policy.should_notify(
            {"rule": rule, "device_key": key, "message": msg,
             "ts": local.isoformat()},
            cfg, local, recent)
        (send if ok else defer).append(item)
        if not ok:
            print("%s  deferred [%s] %s" % (now(), rule, why), flush=True)
    return send, defer


def notify(con, cfg, raised):
    if not raised:
        return
    cur = con.cursor()

    # Rules worth RECORDING but not worth a push, listed in no_notify_rules.
    # A station close to a busy airport will want the aircraft rules in here:
    # aeroplanes being low is the normal state of that sky, and an alert that
    # fires constantly is one you learn to ignore. They still land in the
    # alerts table and the console -- only the push is withheld.
    quiet = set(cfg.get("no_notify_rules") or [])
    if quiet:
        held = [r for r in raised if r[0] in quiet]
        raised = [r for r in raised if r[0] not in quiet]
        if held:
            # Mark them WITHHELD (2), not left at 0. A deliberately silenced
            # alert is not an undelivered one, and leaving both at 0 makes the
            # undelivered count grow forever and mean nothing.
            keys = [k for _, k, _ in held]
            try:
                cur.execute(
                    "UPDATE alerts SET notified=2 WHERE notified=0 "
                    "AND device_key IN (%s)" % ",".join("?" * len(keys)), keys)
                con.commit()
            except Exception:
                pass
            print("%s  %d alert(s) recorded but withheld by policy (%s)"
                  % (now(), len(held),
                     ", ".join(sorted({r[0] for r in held}))), flush=True)
        if not raised:
            return

    raised, deferred = apply_policy(con, cfg, raised)
    if deferred and not raised:
        print("%s  %d alert(s) deferred, nothing to send"
              % (now(), len(deferred)), flush=True)
        return
    if not raised:
        return
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cur.execute("SELECT COUNT(*) FROM alerts WHERE notified=1 AND ts > ?", (hour_ago,))
    sent_recently = cur.fetchone()[0]
    budget = cfg["max_alerts_per_hour"] - sent_recently
    # a critical alert is never held back by the hourly budget
    if budget <= 0 and not any(_is_critical(r[0]) for r in raised):
        return
    batch = raised if budget <= 0 else raised[:budget]
    text = "SDR: %d new\n%s" % (len(raised), "\n".join(m for _, _, m in batch))
    if len(raised) > len(batch):
        text += "\n(+%d more suppressed)" % (len(raised) - len(batch))
    if cfg["notify_enabled"]:
        resp = signal_send(cfg, text)
        ok = '"error"' not in resp
        # Mark ONLY what actually went out. This used to be
        #   UPDATE alerts SET notified=? WHERE notified=0
        # with no batch bound, so every alert the hourly budget had SUPPRESSED
        # was recorded as delivered -- the column proved intent, not delivery,
        # and a suppressed alert could never be retried because it looked sent.
        if ok and batch:
            keys = [k for _, k, _ in batch]
            cur.execute(
                "UPDATE alerts SET notified=1 WHERE notified=0 AND device_key IN (%s)"
                % ",".join("?" * len(keys)), keys)
        con.commit()
        # log the TEXT, not just the response: without it there is no record of
        # what reached the phone, which made a stray alert impossible to trace
        print("%s  notify sent=%s (%d of %d) resp=%.80s\n%s"
              % (now(), ok, len(batch), len(raised), resp, text), flush=True)
    else:
        print("%s  notify DRY-RUN (notify_enabled=false):\n%s" % (now(), text), flush=True)


def publish_mqtt(cfg, rows):
    """Best-effort fan-out. A dead broker must not stop ingest."""
    if not rows:
        return
    try:
        for ts, lane, kind, key, summary, raw in rows[-50:]:
            subprocess.run(
                ["mosquitto_pub", "-h", cfg["mqtt_host"], "-p", str(cfg["mqtt_port"]),
                 "-t", "sdr/%s/%s" % (kind, lane),
                 "-m", json.dumps({"ts": ts, "lane": lane, "kind": kind,
                                   "device": key, "summary": summary})],
                capture_output=True, timeout=5)
    except Exception:
        pass


_ACDB = {"mod": None, "loaded": False}


def aircraft_db():
    """T2/T3: hex -> registration/type, and the notable-aircraft list.

    Loaded once, lazily. If the datasets are missing, enrichment silently
    returns None and ingest continues -- a missing lookup must never break
    event capture.
    """
    if _ACDB["loaded"]:
        return _ACDB["mod"]
    _ACDB["loaded"] = True
    try:
        sys.path.insert(0, os.path.join(ROOT, "enrich"))
        import aircraft_db as m
        m.load(os.path.join(ROOT, "enrich", "data"))
        _ACDB["mod"] = m
    except Exception as e:
        print("%s  aircraft enrichment unavailable: %s: %s"
              % (now(), type(e).__name__, e), flush=True)
    return _ACDB["mod"]


def enrich_aircraft(con, rows):
    """Attach registration/type to ADS-B events, and alert on NOTABLE aircraft.

    A registration is a publicly registered identifier -- unlike a TPMS id it
    genuinely identifies the airframe, so it is defensible under COVERAGE.md.
    Notable hits are alerted because operator+category is a fact, not an
    inference: 'a law-enforcement aircraft is overhead' needs no model.
    """
    m = aircraft_db()
    if not m:
        return []
    alerts = []
    cur = con.cursor()
    seen = set()
    for ts, lane, kind, key, summary, raw in rows:
        if kind != "adsb" or key in seen:
            continue
        seen.add(key)
        info = m.lookup(key)
        if info:
            label = ("%s %s" % (info.get("registration") or "",
                                info.get("type") or "")).strip()
            # params must be ONE sequence: execute(sql, a, b) is a TypeError,
            # and this one killed the collector whenever a registered aircraft
            # flew over, which restarted the whole stack and dropped both radios
            cur.execute("UPDATE devices SET label=? WHERE device_key=? AND "
                        "(label IS NULL OR label='')", (label, key))
        n = m.notable(key)
        if n:
            already = cur.execute(
                "SELECT 1 FROM alerts WHERE rule='notable_aircraft' AND device_key=? "
                "AND ts > datetime('now','-6 hours')", (key,)).fetchone()
            if not already:
                msg = ("NOTABLE AIRCRAFT: %s (%s) %s -- %s / %s"
                       % (key, n.get("registration") or "?", n.get("type") or "",
                          n.get("operator") or "?", n.get("category") or "?"))
                cur.execute("INSERT INTO alerts(ts,rule,device_key,message) "
                            "VALUES (?,?,?,?)", (now(), "notable_aircraft", key, msg))
                alerts.append(("notable_aircraft", key, msg))
    con.commit()
    return alerts


def _table_exists(con, name):
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


SURVEY_LANES = {
    "milair_survey": "milair", "p25_survey": "p25", "survey_fm": "fm",
    "air_survey": "airband", "noaa_wx": "noaa", "murs_survey": "murs",
    "frs_survey": "frs", "airband_survey": "airband2",
    "pager_survey": "pager", "lora915": "ism915",
}


def analyse_lora(path):
    """T17: report LoRa-width activity in a 902-928 sweep.

    Detection only. LoRa is chirp spread spectrum and demodulating it with an
    RTL-SDR is not practical -- the signal is usually below the noise floor,
    which is the point of the modulation. This says whether mesh-shaped traffic
    exists, when, and on which channel. Reading it needs a LoRa radio.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "analysis"))
        import lora
        import csv as _csv
    except Exception as e:
        return "LoRa detection unavailable: %s: %s" % (type(e).__name__, e)
    # An rtl_power file holds MANY sweeps, so the same frequency appears once
    # per sweep. Concatenating them gives duplicate frequencies, the median bin
    # step computes as 0, and the detector silently returns nothing -- it
    # reported "no activity" for a band known to contain 23 strong carriers.
    # Peak-hold per bin instead: a LoRa burst lasts milliseconds and will only
    # ever appear in one sweep row, so the maximum is the right reduction.
    peak = {}
    try:
        with open(path) as fh:
            for row in _csv.reader(fh):
                if len(row) < 7:
                    continue
                try:
                    lo, step = float(row[2]), float(row[4])
                except ValueError:
                    continue
                for i, cell in enumerate(row[6:]):
                    try:
                        v = float(cell)
                    except ValueError:
                        continue
                    f = lo + i * step
                    if f not in peak or v > peak[f]:
                        peak[f] = v
    except OSError as e:
        return "LoRa sweep unreadable: %s" % e
    pts = sorted(peak.items())
    if not pts:
        return None
    return lora.summarise(lora.detect(pts))


def analyse_surveys(con, cfg, seen_mtimes):
    """T8: every survey CSV was written and never read. Now they are.

    rtl_power TRUNCATES its file each run, so a changed mtime means a NEW
    sweep -- that is the trigger. Only carrier peaks are stored: 56,889 raw
    bins per sweep would be ~82M rows/day, versus ~72k for peaks.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "analysis"))
        import spectrum
    except Exception as e:
        # a bare except here hid a plain NameError (sys was never imported) and
        # made survey analysis look like it had simply found nothing to do
        print("%s  survey analysis unavailable: %s: %s"
              % (now(), type(e).__name__, e), flush=True)
        return 0, []
    found = 0
    news = []
    evdir = os.path.join(ROOT, "var", "events")
    for lane, band in SURVEY_LANES.items():
        path = os.path.join(evdir, lane + ".csv")
        if not os.path.exists(path):
            continue
        if lane == "lora915":
            msg = analyse_lora(path)
            if msg:
                print("%s  lora915: %s" % (now(), msg), flush=True)
        m = os.path.getmtime(path)
        if seen_mtimes.get(lane) == m:
            continue          # unchanged since last pass
        seen_mtimes[lane] = m
        try:
            bins = spectrum.parse(path)
            if not bins:
                continue
            # On the FIRST sweep of a band every carrier is "new" -- that is the
            # same trap that made novelty alerts flood Signal with aircraft.
            # Establish the baseline silently, then report genuine changes.
            has_history = con.execute(
                "SELECT COUNT(*) FROM spectrum WHERE band=?", (band,)).fetchone()[0] > 0 \
                if _table_exists(con, "spectrum") else False
            fresh = (spectrum.new_carriers(con, band, bins, history_days=3)
                     if has_history else [])
            spectrum.store(con, band, bins)
            found += 1
            for c in fresh:
                news.append((band, c))
        except Exception as e:
            print("%s  survey analysis failed for %s: %s" % (now(), lane, e), flush=True)
    return found, news


def prune(con, cfg):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg["retention_days"])).isoformat()
    cur = con.cursor()
    # preserved rows are EXEMPT: 14 days is a monitoring retention, not an
    # evidentiary one, and a story that develops over a month would otherwise
    # find its own source material already deleted.
    cur.execute("DELETE FROM events WHERE ts < ? AND COALESCE(preserved,0)=0", (cutoff,))
    n = cur.rowcount
    cur.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
    vcut = (datetime.now(timezone.utc)
            - timedelta(days=cfg.get("audio_retention_days", 7))).isoformat()
    for (vid, path) in list(cur.execute(
            "SELECT id, audio_path FROM voice WHERE ts < ? AND COALESCE(preserved,0)=0",
            (vcut,))):
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass
        cur.execute("DELETE FROM voice WHERE id=?", (vid,))
    rcut = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    rej = os.path.join(ROOT, "var", "voice", "rejected")
    if os.path.isdir(rej):
        for fn in os.listdir(rej):
            fp = os.path.join(rej, fn)
            try:
                if datetime.fromtimestamp(os.path.getmtime(fp), timezone.utc).isoformat() < rcut:
                    os.unlink(fp)
            except Exception:
                pass
    con.commit()
    if n:
        print("%s  pruned %d events older than %d days" % (now(), n, cfg["retention_days"]),
              flush=True)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    cfg = load_config()
    con = db_connect()
    offsets = load_offsets()
    last_prune = 0
    survey_mtimes = {}

    while True:
        nv = ingest_voice(con, cfg)
        if nv:
            print("%s  queued %d new recordings for transcription" % (now(), nv), flush=True)
        rows = ingest(con, cfg, offsets)
        if rows:
            store(con, rows)
            npos = store_positions(con, [(r[0], r[5]) for r in rows if r[2] == "adsb"])
            if npos:
                print("%s  stored %d aircraft positions" % (now(), npos), flush=True)
            save_offsets(offsets)
            raised = evaluate_alerts(con, cfg, rows)
            publish_mqtt(cfg, rows)
            notify(con, cfg, raised)
            print("%s  ingested %d events, %d alerts" % (now(), len(rows), len(raised)),
                  flush=True)
        nsurv, newcar = analyse_surveys(con, cfg, survey_mtimes)
        if nsurv:
            print("%s  analysed %d survey sweep(s)" % (now(), nsurv), flush=True)
        for band, c in newcar:
            # a carrier that was not there yesterday is genuinely worth knowing
            msg = ("New carrier on %s: %.4f MHz at +%.1f dB over floor"
                   % (band, c["freq_hz"] / 1e6, c["over_floor"]))
            print("%s  %s" % (now(), msg), flush=True)
            con.execute("INSERT INTO alerts(ts,rule,device_key,message) VALUES (?,?,?,?)",
                        (now(), "new_carrier", "%s/%.4f" % (band, c["freq_hz"] / 1e6), msg))
            con.commit()
        if time.time() - last_prune > 3600:
            prune(con, cfg)
            last_prune = time.time()
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
