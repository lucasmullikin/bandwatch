#!/usr/bin/env python3
"""bandwatch web UI -- unified activity feed.

Stdlib only, no dependencies. Serves the feed page, a JSON API over the event
store, and the voice recordings themselves.

The design problem this solves: ADS-B produces ~760 events an hour against a
handful of voice transmissions. Shown raw, aircraft bury everything. So
consecutive events from the same device inside ROLLUP_WINDOW collapse into one
row carrying a count and a time span -- 332 messages from ADD0CB become one
line, and a single walkie-talkie transmission keeps equal visual weight.
"""
import collections
import hmac
import secrets
import io
import json
import os
import re
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bandwatch_config as C  # noqa: E402

CONFIG = C.CONFIG
VAR = C.VAR
DB = os.path.join(VAR, "events.db")
VOICE_DIR = os.path.join(VAR, "voice")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import views  # noqa: E402
PORT = int(os.environ.get("BANDWATCH_UI_PORT", "9111"))
ROLLUP_WINDOW = timedelta(minutes=10)

KIND_LABEL = {"ism": "Sensor", "adsb": "Aircraft", "pager": "Pager", "voice": "Voice"}
# CONFIG is the config DIRECTORY (set above from bandwatch_config); this is
# the station settings file inside it. They were briefly the same name, which
# silently repointed SCHEDULE and STATIONS at a path inside a JSON file.
CONFIG_FILE = os.path.join(CONFIG, "bandwatch.json")
SDRCTL = os.path.join(ROOT, "bin", "bandwatch")

# Control actions are POST-only on purpose: a GET endpoint that stops the
# pipeline can be fired by any <img> tag on any page the browser loads.
ACTIONS = {"notify_on", "notify_off", "models_on", "models_off",
           "pipeline_start", "pipeline_stop", "profile",
           "schedule_on", "schedule_off",
           "transcribe_on", "transcribe_off", "listen", "listen_stop"}
SCHEDULE = os.path.join(CONFIG, "schedule.json")
# how long a hand-picked profile survives before the schedule takes over again.
# Event modes are deliberate and temporary; a forgotten one must not become the
# permanent configuration.
OVERRIDE_HOURS = 4
STATIONS = os.path.join(CONFIG, "stations.json")
PAUSE_FMT = os.path.join(tempfile.gettempdir(), "bandwatch_pause_dev%s")


def _tool(name):
    """Locate an external binary on PATH.

    These were hardcoded to one machine's Homebrew prefix, which is wrong on
    every other install: /opt/homebrew on Apple Silicon, /usr/local on Intel,
    anywhere at all for a per-user prefix. Returns the bare name if not found
    so the failure surfaces at exec time with the name in it, rather than as a
    confusing path that does not exist.
    """
    return shutil.which(name) or name


RTL_FM = _tool("rtl_fm")
LAME = _tool("lame")
FFMPEG = _tool("ffmpeg")

# one live listener at a time -- there is one spare radio, and the pipe is single
_listen = {"proc": None, "rtl": None, "freq": None, "mode": None, "device": None}
_listen_lock = threading.Lock()
# Exactly ONE client may read the tuner pipe. There is a single proc.stdout;
# two concurrent readers each get a fraction of the bytes and NEITHER gets a
# decodable MP3. That is why curl worked (one reader) while the browser did
# not -- the <audio> element, a page reload and a probe were splitting the
# stream between them. A new listener takes over and the previous one exits.
_stream_gen = [0]

# 48 kbit/s CBR == 6000 bytes/sec, so this is ~12 seconds of pre-roll.
PREBUFFER_BYTES = 72000
_buf = collections.deque()
_buf_bytes = [0]
_buf_cv = threading.Condition()
_buf_epoch = [0]        # bumped on every retune so stale audio is never served
_buf_seq = [0]          # monotonic chunk counter; see the reader loop
_pump = [None]


def _pump_stream(proc, epoch):
    """Own the encoder's stdout so no HTTP handler ever reads it directly.

    Reading the pipe from the request handler meant audio produced before the
    browser connected was discarded, and a second listener stole the stream
    from the first.
    """
    try:
        while True:
            chunk = proc.stdout.read(2048)
            if not chunk:
                break
            with _buf_cv:
                if _buf_epoch[0] != epoch:
                    return          # a retune happened; this stream is dead
                _buf_seq[0] += 1
                _buf.append((_buf_seq[0], chunk))
                _buf_bytes[0] += len(chunk)
                while _buf_bytes[0] > PREBUFFER_BYTES and len(_buf) > 1:
                    _buf_bytes[0] -= len(_buf.popleft()[1])
                _buf_cv.notify_all()
    except Exception:
        pass
    finally:
        with _buf_cv:
            if _buf_epoch[0] == epoch:
                _buf_cv.notify_all()


def _start_pump(proc):
    with _buf_cv:
        _buf_epoch[0] += 1
        epoch = _buf_epoch[0]
        _buf.clear()
        _buf_bytes[0] = 0
        _buf_seq[0] = 0
    t = threading.Thread(target=_pump_stream, args=(proc, epoch), daemon=True)
    t.start()
    _pump[0] = t
    return epoch


TOKEN_PATH = os.path.join(VAR, "worker_token")


def worker_token():
    """The shared secret a transcription worker must present.

    Generated on first use and stored under var/, which is gitignored --
    config files can be shared, so a secret in one would travel to the git
    remote. Mode 600 because any local user could otherwise read it and
    forge a watchlist alert.
    """
    try:
        tok = io.open(TOKEN_PATH, encoding="utf-8").read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    return tok


def token_ok(supplied):
    """Constant-time compare, so a wrong token cannot be found byte by byte."""
    if not supplied:
        return False
    return hmac.compare_digest(str(supplied), worker_token())


def read_config():
    try:
        return json.load(open(CONFIG_FILE))
    except Exception:
        return {}


def write_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_FILE)


def _bracketed(pattern):
    """Turn "broker.py" into "[b]roker.py" so pgrep cannot match a sibling pgrep.

    pgrep excludes its OWN pid but not another pgrep running concurrently with
    the same string in its argv, so two overlapping checks each report the
    other as a match. Measured here: a bare pattern returned three pids where
    the bracket form returned one. Idempotent -- an already-bracketed pattern
    is passed through untouched.
    """
    if "[" in pattern:
        return pattern
    for i, ch in enumerate(pattern):
        if ch.isalnum():
            return pattern[:i] + "[" + ch + "]" + pattern[i + 1:]
    return pattern


def running(pattern):
    try:
        return subprocess.run(["pgrep", "-f", _bracketed(pattern)],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def read_schedule():
    try:
        return json.load(open(SCHEDULE))
    except Exception:
        return {"enabled": False, "rules": []}


def write_schedule(sc):
    tmp = SCHEDULE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sc, fh, indent=2)
    os.replace(tmp, SCHEDULE)


def list_profiles():
    d = os.path.join(ROOT, "profiles")
    out = []
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".json"):
            continue
        try:
            p = json.load(open(os.path.join(d, fn)))
        except Exception:
            continue
        out.append({"name": p.get("name", fn[:-5]),
                    "description": p.get("description", ""),
                    "event": p.get("name", "").startswith("event-")})
    return out


def current_profile():
    try:
        return json.load(open(os.path.join(VAR, "state.json"))).get("profile")
    except Exception:
        return None


def controls():
    cfg = read_config()
    sc = read_schedule()
    return {
        "profiles": list_profiles(),
        "profile": current_profile(),
        "schedule_enabled": bool(sc.get("enabled")),
        "override_until": sc.get("override_until"),
        "override_profile": sc.get("override_profile"),
        "notify_enabled": bool(cfg.get("notify_enabled", False)),
        "models_enabled": bool(cfg.get("models_enabled", True)),
        "pipeline_running": running("broker.py"),
        "collector_running": running("collector.py"),
        "transcribe_enabled": bool(cfg.get("transcribe_enabled", False)),
        "listen": listen_state(),
        "preserved": preserved_counts(),
        "watchdog": watchdog_state(),
    }


def thumbnail(path, width=320):
    """A cached small copy. Returns the thumb path, or None to serve the original.

    Falls back silently: a missing thumbnailer should cost bandwidth, never the
    image itself.
    """
    d = os.path.join(os.path.dirname(path), ".thumbs")
    out = os.path.join(d, os.path.basename(path))
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
        os.makedirs(d, exist_ok=True)
        r = subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", path,
                            "-vf", "scale=%d:-1" % width, out],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    return None


def watchdog_state():
    """When the watchdog last RAN, not just what it last found.

    A quiet watchdog log is ambiguous between "checked, all well" and "has not
    run in two days" -- and the second case is what actually happened here.
    Surfacing the heartbeat makes the checker itself observable."""
    try:
        with open(os.path.join(VAR, "watchdog.json")) as fh:
            w = json.load(fh)
    except Exception:
        return {"last_check": None, "faults": None}
    return {"last_check": w.get("last_check"),
            "faults": w.get("last_fault_count"),
            "detail": w.get("last_faults") or []}


def do_action(action, arg=None):
    cfg = read_config()
    if action == "profile":
        names = {p["name"] for p in list_profiles()}
        if arg not in names:
            return controls()
        sc = read_schedule()
        sc["override_profile"] = arg
        sc["override_until"] = (datetime.now(timezone.utc)
                                + timedelta(hours=OVERRIDE_HOURS)
                                ).isoformat(timespec="seconds")
        write_schedule(sc)
        sh = shutil.which("bash") or "/bin/bash"
        subprocess.Popen([sh, SDRCTL, "profile", arg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return controls()
    if action in ("transcribe_on", "transcribe_off"):
        cfg["transcribe_enabled"] = (action == "transcribe_on")
        write_config(cfg)
        return controls()
    if action == "listen_stop":
        listen_stop()
        return controls()
    if action in ("schedule_on", "schedule_off"):
        sc = read_schedule()
        sc["enabled"] = (action == "schedule_on")
        if action == "schedule_on":
            sc["override_until"] = None     # resume the schedule immediately
            sc["override_profile"] = None
        write_schedule(sc)
        return controls()
    if action == "notify_on":
        cfg["notify_enabled"] = True; write_config(cfg)
    elif action == "notify_off":
        cfg["notify_enabled"] = False; write_config(cfg)
    elif action == "models_on":
        cfg["models_enabled"] = True; write_config(cfg)
    elif action == "models_off":
        # frees the GPU for anything else sharing this machine
        cfg["models_enabled"] = False; write_config(cfg)
    elif action in ("pipeline_start", "pipeline_stop"):
        # radios-off, not stop: the Scanning switch releases the dongles and
        # must leave this web UI running, or there is no way to switch it back
        verb = "radios-on" if action == "pipeline_start" else "radios-off"
        sh = shutil.which("bash") or "/bin/bash"
        subprocess.Popen([sh, SDRCTL, verb],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        # bandwatch runs detached, so controls() computed right now still sees
        # the OLD state and the toggle springs back to where it was -- it read
        # as a dead button. Wait, briefly and boundedly, for reality to match
        # the intent before answering. Bounded because a hung bandwatch must not
        # hang the console: after the timeout we return the truth, whatever it
        # is, rather than the intent.
        want = (verb == "radios-on")
        for _ in range(40):                 # up to ~10s
            if running("broker.py") == want:
                break
            time.sleep(0.25)
    return controls()


def q(con, sql, args=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args)]


def parse_ts(s):
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def rollup(rows):
    """Collapse all events from one device inside a window into a single row.

    Consecutive-run grouping is not enough: two busy aircraft interleave, so
    runs stay short and the same hex appears a dozen times. Grouping by
    (kind, device, time-bucket) turns 332 messages from one aircraft into one
    row, which is what keeps a single walkie-talkie transmission legible
    beside 700 ADS-B hits.

    ADS-B summaries each carry only one field (altitude OR speed OR callsign),
    so the group merges them into one line instead of picking arbitrarily.
    """
    win = int(ROLLUP_WINDOW.total_seconds())
    groups = {}
    order = []
    for r in rows:
        ts = parse_ts(r["ts"])
        if not ts:
            continue
        bucket = int(ts.timestamp()) // win
        key = (r["kind"], r["device_key"], bucket)
        g = groups.get(key)
        if g is None:
            g = {"ts": r["ts"], "oldest": r["ts"], "kind": r["kind"],
                 "lane": r["lane"], "device": r["device_key"],
                 "summary": r["summary"], "count": 0, "type": "event",
                 "_alt": None, "_spd": None, "_call": None, "_newest": ts}
            groups[key] = g
            order.append(key)
        g["count"] += 1
        if ts > g["_newest"]:
            g["_newest"] = ts
            g["ts"] = r["ts"]
        if r["ts"] < g["oldest"]:
            g["oldest"] = r["ts"]
        txt = r["summary"] or ""
        m = re.search(r"(\d+)ft", txt)
        if m:
            g["_alt"] = m.group(1)
        m = re.search(r"(\d+)kt", txt)
        if m:
            g["_spd"] = m.group(1)
        m = re.search(r"^\S+\s+([A-Z]{2,3}\d{1,4}\S*)", txt)
        if m:
            g["_call"] = m.group(1)
        if len(txt) > len(g["summary"] or ""):
            g["summary"] = txt

    out = []
    for key in order:
        g = groups[key]
        if g["kind"] == "adsb":
            bits = [b for b in (g["_call"],
                                "%s ft" % f"{int(g['_alt']):,}" if g["_alt"] else None,
                                "%s kt" % g["_spd"] if g["_spd"] else None) if b]
            g["summary"] = " · ".join(bits) if bits else "position only"
        for k in ("_alt", "_spd", "_call", "_newest"):
            g.pop(k, None)
        out.append(g)
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def feed(limit=250, kinds=None, search=None):
    con = sqlite3.connect(DB)
    items = []

    # --- voice: never rolled up, every transmission matters individually ---
    if not kinds or "voice" in kinds:
        sql = "SELECT * FROM voice"
        args = []
        if search:
            sql += " WHERE (transcript LIKE ? OR channel LIKE ?)"
            args += ["%%%s%%" % search, "%%%s%%" % search]
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        for v in q(con, sql, args):
            items.append({
                "id": v["id"], "preserved": bool(v["preserved"] or 0),
                "sha256": v["sha256"], "by": v["transcribed_by"], "model": v["model"],
                "type": "voice", "ts": v["ts"], "kind": "voice",
                "channel": v["channel"], "freq": v["freq_mhz"],
                "duration": v["duration_s"], "transcript": v["transcript"],
                "audio": os.path.basename(v["audio_path"] or ""),
                "hit": v["watchlist_hit"], "count": 1,
            })

    # --- everything else, rolled up ---
    ev_kinds = [k for k in (kinds or ["ism", "adsb", "pager"]) if k != "voice"]
    if ev_kinds:
        ph = ",".join("?" * len(ev_kinds))
        sql = "SELECT ts,lane,kind,device_key,summary FROM events WHERE kind IN (%s)" % ph
        args = list(ev_kinds)
        if search:
            sql += " AND (summary LIKE ? OR device_key LIKE ?)"
            args += ["%%%s%%" % search, "%%%s%%" % search]
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit * 8)          # over-fetch: rollup collapses many rows
        items.extend(rollup(q(con, sql, args)))

    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def devices(kind=None, since_h=None, search=None, order="last_seen"):
    """Every device ever heard, with counts and a recent-activity sparkline.

    This is the sensor browser: what have I seen, how often, when did it start,
    when did it last appear. Filterable by band, by age, and by text.
    """
    con = sqlite3.connect(DB)
    sql = ("SELECT d.device_key, d.kind, d.seen_count, d.first_seen, d.last_seen, d.label "
           "FROM devices d WHERE 1=1")
    args = []
    if kind and kind != "all":
        sql += " AND d.kind = ?"
        args.append(kind)
    if search:
        sql += " AND d.device_key LIKE ?"
        args.append("%%%s%%" % search)
    if since_h:
        cut = (datetime.now(timezone.utc) - timedelta(hours=float(since_h))).isoformat()
        sql += " AND d.last_seen >= ?"
        args.append(cut)
    col = {"last_seen": "d.last_seen DESC", "first_seen": "d.first_seen DESC",
           "count": "d.seen_count DESC", "name": "d.device_key ASC"}.get(order, "d.last_seen DESC")
    sql += " ORDER BY " + col + " LIMIT 500"
    rows = q(con, sql, args)

    # 24 hourly buckets per device so the browser can draw activity at a glance
    cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    spark = {}
    for r in q(con, "SELECT device_key, ts FROM events WHERE ts >= ?", (cut,)):
        dt = parse_ts(r["ts"])
        if not dt:
            continue
        age_h = int((datetime.now(timezone.utc) - dt).total_seconds() // 3600)
        if 0 <= age_h < 24:
            spark.setdefault(r["device_key"], [0] * 24)[23 - age_h] += 1
    for r in rows:
        r["spark"] = spark.get(r["device_key"], [0] * 24)
        r["summary"] = None
    if rows:
        keys = [r["device_key"] for r in rows]
        ph = ",".join("?" * len(keys))
        latest = {}
        for e in q(con, "SELECT device_key, summary, ts FROM events "
                        "WHERE device_key IN (%s) ORDER BY ts ASC" % ph, keys):
            latest[e["device_key"]] = e["summary"]
        for r in rows:
            r["summary"] = latest.get(r["device_key"])
    return rows


CLAIM_TTL_MIN = 5


def pending_voice(limit=20, worker=None, min_age_min=0, tier=None):
    """Hand out work, claiming it so several hosts can share one queue.

    Without claiming, the Studio and the M4 both fetch the same rows and
    transcribe them twice. A claim older than CLAIM_TTL_MIN is reclaimable, so
    a worker that dies mid-batch cannot strand its rows forever.
    """
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout = 5000")
    cfg = read_config()
    mode = cfg.get("transcribe_mode", "all")
    if mode == "off":
        return []
    # SCREEN tier = the cheap local model looking for watchwords.
    # QUALITY tier = the good model, and ONLY on clips that already hit a
    # watchword. In watchwords mode the GPU therefore runs on a handful of
    # clips a day instead of continuously.
    stale = (datetime.now(timezone.utc)
             - timedelta(minutes=CLAIM_TTL_MIN)).isoformat(timespec="seconds")
    # No backlog: a clip nobody transcribed within the window is abandoned
    # rather than queued forever. Old scanner audio has little value and an
    # unbounded queue is how a transcriber ends up running all night.
    max_age = float(cfg.get("transcribe_max_age_min", 0) or 0)
    floor_ts = ("0000" if not max_age else
                (datetime.now(timezone.utc)
                 - timedelta(minutes=max_age)).isoformat(timespec="seconds"))
    min_dur = float(cfg.get("transcribe_min_duration_s", 0) or 0)
    only_ch = cfg.get("transcribe_channels") or []
    # An overflow worker asks for min_age so it only picks up what the faster,
    # more accurate host did not get to. Without this the slow host races the
    # fast one and you get worse transcripts for no reason.
    age_cut = (datetime.now(timezone.utc)
               - timedelta(minutes=float(min_age_min or 0))).isoformat(timespec="seconds")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    q_extra = ""
    q_args = []
    if mode == "watchwords" and tier == "quality":
        # only clips the cheap pass already flagged
        q_extra += " AND watchlist_hit IS NOT NULL AND needs_quality=1"
    elif mode == "watchwords":
        q_extra += " AND COALESCE(needs_quality,0)=0"
    if min_dur:
        q_extra += " AND (duration_s IS NULL OR duration_s >= ?)"
        q_args.append(min_dur)
    if only_ch:
        q_extra += " AND channel IN (%s)" % ",".join("?" * len(only_ch))
        q_args.extend(only_ch)
    rows = [dict(zip(("id", "ts", "channel", "audio_path", "duration_s"), r))
            for r in cur.execute(
                "SELECT id, ts, channel, audio_path, duration_s FROM voice "
                "WHERE transcript IS NULL AND ts >= ? " + q_extra + " "
                # A worker must be able to see rows IT already claimed. The
                # outer 'is there work?' poll claims them, so excluding all
                # claimed rows made the worker starve itself: it took the GPU
                # lease, loaded the model, then found nothing to do.
                "AND (claimed_by IS NULL OR claimed_at < ? OR claimed_by = ?) "
                "AND ts <= ? "
                "ORDER BY ts DESC LIMIT ?",
                tuple([floor_ts] + q_args + [stale, worker or "anon", age_cut, limit]))]
    for r in rows:
        cur.execute("UPDATE voice SET claimed_by=?, claimed_at=? WHERE id=?",
                    (worker or "anon", now, r["id"]))
    con.commit()
    return rows


# Whisper emits these verbatim on silence or noise. A 30-second "Thank you."
# is not a transmission -- it is the model filling an empty channel. Treated as
# text they become phantom voice activity and eventually phantom alerts.
HALLUCINATIONS = {
    "", ".", "you", "thank you", "thank you.", "thanks for watching!",
    "thank you. thank you.", "thanks for watching.", "bye.", "so",
    "please subscribe", "subtitles by the amara.org community",
    "transcription by castingwords", "okay", "oh",
}


def looks_hallucinated(text):
    """True when the transcript is a known silence artefact, not speech."""
    if not text:
        return True
    t = " ".join(text.lower().split())
    # whisper.cpp annotates non-speech in brackets: [MUSIC PLAYING],
    # [BLANK_AUDIO], [SOUND]. A transcript that is ONLY annotation is the
    # model telling us there was no speech.
    if re.fullmatch(r"(\s*\[[^\]]*\]\s*)+", t):
        return True
    if t.strip(" .!?,") in {h.strip(" .!?,") for h in HALLUCINATIONS}:
        return True
    words = [w for w in re.findall(r"[a-z']+", t) if w]
    if not words:
        return True
    # a real transmission is not one word repeated for half a minute
    if len(set(words)) <= 2 and len(words) >= 4:
        return True
    return False


# Thresholds chosen from observed airband output: real ATC ("Bender Tower,
# Skyhawk 61557, inbound left traffic, 28L") scores well; fabrications
# ("ja ja ja", "Hello, Hello, Hello...") score badly on one of these.
NO_SPEECH_MAX = 0.60      # model itself says there was no speech
AVG_LOGPROB_MIN = -1.10   # confidence collapses when it is guessing
COMPRESSION_MAX = 2.40    # high ratio = the text is a repetition loop


MIN_SPEECH_S = 0.5


def duration_reject(duration):
    """A tenth of a second cannot contain a transmission.

    Observed: 0.1s clips producing 'that 2 and left 2 and left...' for pages.
    The clip gate stops new ones at ingest; this catches what is already stored.
    """
    try:
        return ("duration %.2fs" % float(duration)
                if float(duration) < MIN_SPEECH_S else None)
    except (TypeError, ValueError):
        return None


def confidence_reject(conf):
    """Return a reason string when Whisper's own numbers say this is not speech."""
    if not isinstance(conf, dict):
        return None
    # NaN compares False against EVERY threshold, so a NaN score would sail
    # through all three checks. Observed in practice as lp=nan.
    for k in ("no_speech_prob", "avg_logprob", "compression_ratio"):
        v = conf.get(k)
        if isinstance(v, float) and v != v:
            return "%s is NaN" % k
    if conf.get("no_speech_prob", 0) > NO_SPEECH_MAX:
        return "no_speech_prob %.2f" % conf["no_speech_prob"]
    if conf.get("avg_logprob", 0) < AVG_LOGPROB_MIN:
        return "avg_logprob %.2f" % conf["avg_logprob"]
    if conf.get("compression_ratio", 0) > COMPRESSION_MAX:
        return "compression_ratio %.2f" % conf["compression_ratio"]
    return None


def save_transcript(vid, text, duration=None, conf=None, by=None, model=None):
    cfg = read_config()
    terms = []
    wl = os.path.join(CONFIG, "watchlist.json")
    if os.path.exists(wl):
        try:
            terms = [t.lower() for t in json.load(open(wl)).get("terms", []) if t.strip()]
        except Exception:
            terms = []
    reason = duration_reject(duration) or confidence_reject(conf)
    kept = True
    if reason or looks_hallucinated(text):
        text = ""          # store empty rather than fiction
        kept = False
    low = (text or "").lower()
    hits = ", ".join([t for t in terms if t in low]) or None
    c = conf if isinstance(conf, dict) else {}
    con = sqlite3.connect(DB)
    cfgm = read_config().get("transcribe_mode", "all")
    cheap = bool(model and "whisper.cpp" in str(model))
    escalate = 1 if (cfgm == "watchwords" and hits and cheap) else 0
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout = 5000")
    if escalate:
        # re-open it for the accurate model: a watchword hit is exactly the
        # clip where transcript quality matters
        con.execute("UPDATE voice SET needs_quality=1, transcript=NULL, "
                    "transcribed_at=NULL, claimed_by=NULL, claimed_at=NULL, "
                    "watchlist_hit=? WHERE id=?", (hits, vid))
        con.commit()
        return {"id": vid, "watchlist_hit": hits, "kept": True,
                "escalated": True, "rejected_because": None}
    con.execute(
        "UPDATE voice SET transcript=?, transcribed_at=?, watchlist_hit=?, "
        # COALESCE(duration_s, ?) not COALESCE(?, duration_s): the ingest
        # MEASURES this from the file, while the worker sends the end time of
        # Whisper's last segment -- which on a hallucinated transcript runs
        # past the end of the audio (3.4s clips were stored as 32.9s). A
        # measured value always wins; the worker's is a fallback.
        "duration_s=COALESCE(duration_s,?), claimed_by=NULL, claimed_at=NULL, "
        "no_speech_prob=?, avg_logprob=?, compression_ratio=?, rejected_because=?, "
        "transcribed_by=?, model=? "
        "WHERE id=?",
        (text, datetime.now(timezone.utc).isoformat(timespec="seconds"),
         hits, duration,
         c.get("no_speech_prob"), c.get("avg_logprob"), c.get("compression_ratio"),
         reason, by, model, vid))
    con.commit()
    if hits and kept:
        raise_watchlist_alert(con, vid, hits, text)
    con.close()
    return {"id": vid, "watchlist_hit": hits, "kept": kept,
            "rejected_because": reason}


def raise_watchlist_alert(con, vid, hits, text):
    """Promote a watchlist hit to an alerts row and page the operator.

    Deliberately bypasses the hourly alert budget: this is the one rule where
    dropping the message to save budget defeats the point of having it.

    The transcript is NOT included in the notification. A transcript is a lead,
    not a fact, and quoting a possibly-hallucinated address into a push message
    is worse than a pointer to the clip -- the audio is one click away in the
    console.
    """
    try:
        row = con.execute(
            "SELECT channel, freq_mhz, ts FROM voice WHERE id=?", (vid,)).fetchone()
        channel = row[0] if row else "?"
        freq = row[1] if row else None
        msg = ("WATCHLIST: %s on %s%s -- clip #%d. Listen in the console; the "
               "transcript is a lead, not a fact."
               % (hits, channel,
                  (" %.4f MHz" % freq) if freq else "", vid))
        con.execute(
            "INSERT INTO alerts(ts,rule,device_key,message,notified) "
            "VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "watchlist_hit", "voice/%s" % channel, msg, 0))
        con.commit()
    except Exception as e:
        print("watchlist alert FAILED to record: %s" % e, flush=True)
        return

    cfg = read_config()
    if not cfg.get("notify_enabled"):
        print("watchlist alert recorded, notify disabled: %s" % msg, flush=True)
        return
    try:
        sys.path.insert(0, os.path.join(ROOT, "notify"))
        import notifiers
        ok, detail = notifiers.send(cfg, "bandwatch " + msg)
        # Mark delivered ONLY on success. The previous version fired curl and
        # set notified=1 unconditionally, so an unreachable notifier produced
        # a row that claimed the alert had been sent -- the worst possible
        # state, because nothing ever retries and nothing looks wrong.
        if ok:
            con.execute("UPDATE alerts SET notified=1 WHERE rule='watchlist_hit' "
                        "AND message=?", (msg,))
            con.commit()
            print("watchlist alert SENT: %s" % msg, flush=True)
        else:
            print("watchlist alert recorded but NOT sent: %s"
                  % (detail or "no detail"), flush=True)
    except Exception as e:
        print("watchlist alert recorded but NOT sent: %s" % e, flush=True)


def listen_state():
    with _listen_lock:
        p = _listen["proc"]
        alive = bool(p and p.poll() is None)
        return {"listening": alive, "freq": _listen["freq"] if alive else None,
                "mode": _listen["mode"] if alive else None,
                "device": _listen["device"] if alive else None}


def _device_still_held(dev):
    """Is any tuner process still holding this dongle?

    Checked by argv because rtl_fm and rtl_tcp take -d on the command line.
    (rtl_airband does NOT -- it reads its index from a config file -- but the
    broker owns those and stops them itself.)
    """
    try:
        out = subprocess.run(["pgrep", "-fl", "rtl_fm|rtl_tcp|rtl_power"],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if ("-d %s" % dev) in line or ("-d%s" % dev) in line:
            return True
    return False


def listen_stop():
    """Kill the tuner and hand the radio back to the broker.

    Order matters: the pause flag is released LAST. It is the only thing
    keeping the broker off this radio, so releasing it while a tuner still
    holds the device makes the broker hand a DEAD handle to every lane that
    follows -- which reads as a quiet band, not as an error.
    """
    with _listen_lock:
        for key in ("proc", "rtl"):
            pr = _listen.get(key)
            if pr and pr.poll() is None:
                try:
                    os.killpg(os.getpgid(pr.pid), 15)
                    pr.wait(timeout=5)
                except Exception:
                    pass
                # rtl_fm ignores SIGTERM, the same way rtl_airband does.
                # Observed: it survived a group TERM and kept the dongle.
                if pr.poll() is None:
                    for sig in (9,):
                        try:
                            os.killpg(os.getpgid(pr.pid), sig)
                        except Exception:
                            try:
                                pr.kill()
                            except Exception:
                                pass
                    try:
                        pr.wait(timeout=5)
                    except Exception:
                        pass
            _listen[key] = None
        dev = _listen.get("device")
        _listen.update({"freq": None, "mode": None, "device": None})

    if dev is not None:
        # do not free the broker until the radio is genuinely free
        for _ in range(20):
            if not _device_still_held(dev):
                break
            time.sleep(0.25)
        else:
            try:
                subprocess.run(["pkill", "-9", "-f", "rtl_fm.*-d *%s" % dev],
                               capture_output=True, timeout=6)
            except Exception:
                pass
        try:
            os.unlink(PAUSE_FMT % dev)
        except FileNotFoundError:
            pass
    return listen_state()


def listen_start(freq, mode, device):
    """Borrow a radio and stream it as MP3.

    The broker polls a pause flag and releases the dongle rather than fighting
    for it -- whoever loses that race gets a dead handle, not an error, which is
    how a lane goes silently deaf.
    """
    listen_stop()
    dev = str(int(device))
    open(PAUSE_FMT % dev, "w").write(str(os.getpid()))

    # Wait for the BROKER to confirm it released the radio, not for a command
    # line to look free. rtl_airband takes its device index from its CONFIG
    # FILE, so scanning argv for "-d 0" reports the device free while
    # rtl_airband still holds it -- rtl_fm then fails to open it and dies.
    # state.json paused=true is the broker's own authoritative signal.
    statef = os.path.join(VAR, "state.json")
    released = False
    for _ in range(150):          # 0.2s granularity instead of 1s
        try:
            st = json.load(open(statef))
            d = st.get("devices", {}).get(dev, {})
            if d.get("paused") and not d.get("current_lane"):
                released = True
                break
        except Exception:
            pass
        time.sleep(0.2)
    if not released:
        # broker may simply not be running -- then nothing holds the device
        if running("broker.py"):
            try:
                os.unlink(PAUSE_FMT % dev)
            except FileNotFoundError:
                pass
            raise RuntimeError("radio %s did not come free in 30s" % dev)

    mode = (mode or "nfm").lower()
    if mode == "wfm":
        rtl = [RTL_FM, "-d", dev, "-f", "%.4fM" % freq, "-M", "wbfm",
               "-s", "200000", "-r", "32000", "-g", "30", "-E", "dc", "-"]
        in_rate = "32000"
    else:
        m = "am" if mode == "am" else "fm"
        rtl = [RTL_FM, "-d", dev, "-f", "%.4fM" % freq, "-M", m,
               "-s", "22050", "-g", "40", "-E", "dc", "-"]
        in_rate = "22050"

    # ffmpeg, not lame. MEASURED on this box: lame took 3.0-3.8s to emit its
    # first mp3 byte because it buffers before encoding, against rtl_fm's own
    # 1.03s startup. ffmpeg with nobuffer/low_delay/flush_packets emits at
    # 1.03s -- effectively zero added latency. That is ~3 seconds of a live
    # transmission you no longer miss between clicking and hearing.
    # 44.1 kHz output keeps it MPEG-1, which browsers actually decode.
    enc = [FFMPEG, "-loglevel", "quiet",
           "-fflags", "nobuffer", "-flags", "low_delay",
           "-f", "s16le", "-ar", in_rate, "-ac", "1", "-i", "pipe:0",
           "-ar", "44100", "-ac", "1", "-b:a", "48k",
           "-f", "mp3", "-flush_packets", "1", "pipe:1"]

    with _listen_lock:
        errlog = open(os.path.join(VAR, "logs", "listen.log"), "ab")
        errlog.write(b"\n--- tune %s on dev %s ---\n"
                     % (str(freq).encode(), dev.encode()))
        errlog.flush()
        r = subprocess.Popen(rtl, stdout=subprocess.PIPE,
                             stderr=errlog, start_new_session=True)
        l = subprocess.Popen(enc, stdin=r.stdout, stdout=subprocess.PIPE,
                             stderr=errlog, start_new_session=True)
        r.stdout.close()
        _listen.update({"proc": l, "rtl": r, "freq": freq,
                        "mode": mode, "device": dev})
    # Own the encoder output immediately. The seconds between the radio coming
    # up and the browser issuing its GET used to be discarded outright, which
    # is most of the "we keep losing the beginnings" problem.
    _start_pump(l)
    return listen_state()


def preserve(kind, row_id, on=True):
    """Exempt a row from the retention sweep, and hash the audio when preserving.

    A preserved recording is a copy of record: the hash is taken at preservation
    time so a file produced later can be proven to be the same file. Preserve
    per item, deliberately -- widening the default retention just fills the disk
    with material nobody will review.
    """
    con = sqlite3.connect(DB)
    cur = con.cursor()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if kind == "voice":
        r = cur.execute("SELECT audio_path FROM voice WHERE id=?", (row_id,)).fetchone()
        if not r:
            return {"error": "no such recording"}
        digest = None
        if on and r[0] and os.path.exists(r[0]):
            h = hashlib.sha256()
            with open(r[0], "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        cur.execute("UPDATE voice SET preserved=?, preserved_at=?, "
                    "sha256=COALESCE(?,sha256) WHERE id=?",
                    (1 if on else 0, now if on else None, digest, row_id))
        con.commit()
        return {"ok": True, "id": row_id, "preserved": bool(on), "sha256": digest}
    if kind == "event":
        cur.execute("UPDATE events SET preserved=? WHERE id=?", (1 if on else 0, row_id))
        con.commit()
        return {"ok": True, "id": row_id, "preserved": bool(on)}
    return {"error": "kind must be voice or event"}


def preserved_counts():
    try:
        con = sqlite3.connect(DB)
        v = con.execute("SELECT COUNT(*) FROM voice WHERE COALESCE(preserved,0)=1").fetchone()[0]
        e = con.execute("SELECT COUNT(*) FROM events WHERE COALESCE(preserved,0)=1").fetchone()[0]
        return {"voice": v, "events": e}
    except Exception:
        return {"voice": 0, "events": 0}


def read_stations():
    try:
        return json.load(open(STATIONS))
    except Exception:
        return {"groups": []}


def stats():
    con = sqlite3.connect(DB)
    s = {}
    for r in q(con, "SELECT kind,COUNT(*) n FROM events GROUP BY kind"):
        s[r["kind"]] = r["n"]
    s["voice"] = q(con, "SELECT COUNT(*) n FROM voice")[0]["n"]
    s["devices"] = q(con, "SELECT COUNT(*) n FROM devices")[0]["n"]
    s["alerts"] = q(con, "SELECT COUNT(*) n FROM alerts")[0]["n"]
    return s


class Server(ThreadingHTTPServer):
    """A disconnecting client must not take the server down.

    Closing an audio stream mid-flight raises BrokenPipeError inside
    socketserver's own write path -- outside any handler try/except -- and that
    killed the whole web UI. Observed live after listening to /listen.mp3.
    """
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return                      # client went away; entirely normal
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler defaults to HTTP/1.0. Chrome's media stack sends a
    # Range request for <audio> and expects HTTP/1.1 semantics; on 1.0 with no
    # Content-Length it never starts decoding -- observed as readyState 0 and
    # buffered 0 while bytes were demonstrably flowing.
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_media(self, data):
        """Serve a finite audio body with Range support.

        A media element asks for a byte range and expects 206. Answering 200
        with Accept-Ranges: none left Chrome stuck in NETWORK_LOADING on a 7 KB
        file that curl fetched instantly.
        """
        total = len(data)
        rng = self.headers.get("Range")
        start, end = 0, total - 1
        code = 200
        if rng and rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else total - 1
                start = max(0, min(start, total - 1))
                end = max(start, min(end, total - 1))
                code = 206
            except ValueError:
                start, end, code = 0, total - 1, 200
        chunk = data[start:end + 1]
        try:
            self.send_response(code)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if code == 206:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, end, total))
            self.end_headers()
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        route = u.path.rstrip("/")
        if route not in ("/api/control", "/api/transcript", "/api/listen",
                         "/api/preserve"):
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        if route == "/api/preserve":
            try:
                rid = int(body.get("id"))
            except Exception:
                return self._send(400, json.dumps({"error": "id must be int"}))
            r = preserve(body.get("kind", "voice"), rid, bool(body.get("preserve", True)))
            code = 400 if "error" in r else 200
            return self._send(code, json.dumps(r))
        if route == "/api/listen":
            try:
                freq = float(body.get("freq"))
                dev = int(body.get("device", 1))
            except Exception:
                return self._send(400, json.dumps({"error": "freq and device required"}))
            if not (24.0 <= freq <= 1766.0):
                return self._send(400, json.dumps({"error": "frequency out of range"}))
            if dev not in (0, 1):
                return self._send(400, json.dumps({"error": "device must be 0 or 1"}))
            try:
                st = listen_start(freq, body.get("mode"), dev)
            except Exception as e:
                return self._send(503, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, "listen": st}))
        if route == "/api/transcript":
            # This endpoint rewrites the copy of record for what a
            # transmission said, and a watchlist match on the supplied text
            # sends a real Signal message. Unauthenticated, any device on the
            # LAN could page the operator with a fabricated hit, or quietly
            # launder a recording's transcript into something else.
            supplied = (self.headers.get("X-Worker-Token")
                        or body.get("worker_token"))
            if not token_ok(supplied):
                return self._send(401, json.dumps(
                    {"error": "worker token required"}))
            vid = body.get("id")
            if not isinstance(vid, int):
                return self._send(400, json.dumps({"error": "id must be int"}))
            r = save_transcript(vid, body.get("text") or "",
                                body.get("duration"), body.get("conf"),
                                body.get("by"), body.get("model"))
            return self._send(200, json.dumps({"ok": True, **r}))
        action = body.get("action")
        if action not in ACTIONS:
            return self._send(400, json.dumps({"error": "unknown action"}))
        return self._send(200, json.dumps({
            "ok": True, "controls": do_action(action, body.get("profile"))}))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(u.query)

        if path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")

        if path == "/api/feed":
            kinds = qs.get("kind", [None])[0]
            kinds = kinds.split(",") if kinds and kinds != "all" else None
            search = (qs.get("q", [""])[0] or "").strip() or None
            return self._send(200, json.dumps({
                "items": feed(kinds=kinds, search=search), "stats": stats(),
                "controls": controls()}))

        if path == "/api/devices":
            return self._send(200, json.dumps({"devices": devices(
                kind=(qs.get("kind", ["all"])[0]),
                since_h=(qs.get("since", [None])[0]),
                search=(qs.get("q", [""])[0] or "").strip() or None,
                order=(qs.get("order", ["last_seen"])[0]))}))

        if path == "/api/pending":
            # transcription paused -> hand the worker nothing; it idles cleanly
            if not read_config().get("transcribe_enabled", False):
                return self._send(200, json.dumps({"pending": [], "paused": True}))
            return self._send(200, json.dumps({"pending": pending_voice(
                worker=qs.get("worker", [None])[0],
                min_age_min=qs.get("min_age", [0])[0],
                tier=qs.get("tier", [None])[0])}))

        if path == "/api/vehicles":
            try:
                sys.path.insert(0, os.path.join(ROOT, "analysis"))
                import vehicles as _v
                out = {"vehicles": _v.vehicle_status(DB, CONFIG)}
                for veh in out["vehicles"]:
                    for t in veh["tyres"]:
                        t["history"] = _v.pressure_history(
                            DB, t["sensor"],
                            hours=float(qs.get("hours", ["168"])[0]))
                return self._send(200, json.dumps(out))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/feeds":
            try:
                sys.path.insert(0, HERE)
                import feeds as _f
                return self._send(200, json.dumps(_f.status(ROOT)))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/captures":
            try:
                sys.path.insert(0, HERE)
                import panels
                return self._send(200, json.dumps(
                    {"captures": panels.captures(ROOT)}))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/health":
            try:
                sys.path.insert(0, HERE)
                import panels
                h = qs.get("hours", [None])[0]
                return self._send(200, json.dumps(
                    panels.health_trends(DB, hours=float(h) if h else 24)))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/transcription":
            try:
                sys.path.insert(0, HERE)
                import panels
                h = qs.get("hours", [None])[0]
                return self._send(200, json.dumps(
                    panels.transcription_status(DB, ROOT,
                                                hours=float(h) if h else 24)))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/lanes":
            try:
                sys.path.insert(0, HERE)
                import panels
                h = qs.get("hours", [None])[0]
                return self._send(200, json.dumps(
                    panels.lane_yield(DB, ROOT, hours=float(h) if h else None)))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path.startswith("/img/"):
            # serve a decoded satellite image out of var/. basename() alone is
            # not the guard: verify the resolved path really sits under var/
            rel = urllib.parse.unquote(path[len("/img/"):])
            base = os.path.realpath(VAR)
            cand = os.path.realpath(os.path.join(base, rel))
            if cand.startswith(base + os.sep) and os.path.isfile(cand):
                if qs.get("thumb"):
                    cand = thumbnail(cand) or cand
                try:
                    with open(cand, "rb") as fh:
                        data = fh.read()
                except OSError:
                    return self._send(404, json.dumps({"error": "unreadable"}))
                ct = ("image/jpeg" if cand.lower().endswith((".jpg", ".jpeg"))
                      else "image/png")
                return self._send(200, data, ct)
            return self._send(404, json.dumps({"error": "not found"}))

        if path == "/api/environment":
            try:
                sys.path.insert(0, os.path.join(ROOT, "analysis"))
                import readings as _r
                con = sqlite3.connect(DB, timeout=10)
                hours = float(qs.get("hours", ["24"])[0])
                devs = _r.device_summary(con, root=ROOT, hours=hours)
                # attach a trace per metric so a tile can show that a sensor
                # flatlined at a plausible-looking value, which a single
                # number cannot
                for d in devs:
                    for m in d["metrics"]:
                        m["series"] = _r.series(con, d["device_key"],
                                                m["metric"], hours=hours,
                                                max_points=90)
                owned, labels = _r.load_owned(CONFIG)
                con.close()
                return self._send(200, json.dumps({
                    "hours": hours, "devices": devs,
                    "nominated": sorted(owned),
                    "note": ("Ownership is declared in config/sensors.json, never "
                             "inferred. Anything not named there is reported "
                             "as third-party: receiving a sensor is not "
                             "owning it."),
                }))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/tracks":
            try:
                return self._send(200, json.dumps(views.tracks(
                    DB, since_h=float(qs.get("since", ["6"])[0]),
                    hex_filter=qs.get("hex", [None])[0])))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/patterns":
            try:
                kinds = qs.get("kind", [None])[0]
                out = views.patterns(
                    DB, days=int(qs.get("days", ["14"])[0]),
                    kinds=kinds.split(",") if kinds and kinds != "all" else None)
                out["voice"] = views.voice_patterns(
                    DB, days=int(qs.get("days", ["14"])[0]))["channels"]
                return self._send(200, json.dumps(out))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/entity":
            key = qs.get("key", [None])[0]
            if not key:
                return self._send(200, json.dumps({"results": views.search_entities(
                    DB, qs.get("q", [""])[0])}))
            try:
                r = views.entity(DB, key)
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            return self._send(404 if "error" in r else 200, json.dumps(r))

        if path == "/api/stations":
            st = read_stations()
            try:
                hours = float(qs.get("active_h", ["24"])[0])
                st = views.stations_with_activity(
                    st, views.station_activity(DB, hours=hours))
            except Exception as e:
                # the directory must still load if the activity join fails --
                # losing the badge is survivable, losing the tuner is not
                st["activity_error"] = str(e)
            return self._send(200, json.dumps(st))

        if path == "/listen.mp3":
            with _listen_lock:
                proc = _listen["proc"]
            if not proc or proc.poll() is not None:
                return self._send(409, json.dumps({"error": "not listening"}))
            with _buf_cv:
                epoch = _buf_epoch[0]
                snap = list(_buf)           # the pre-roll, oldest first
                backlog = [c for _, c in snap]
                # track a monotonic sequence, NOT a position: the deque evicts
                # from the left, so an index silently drifts once it is full
                last_seq = snap[-1][0] if snap else 0
            # A live stream has no length and no seekable range. Say so
            # explicitly and close on completion, so the browser reads until
            # EOF instead of waiting for a length that never comes.
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                # hand over the backlog first: this is the audio that happened
                # before this listener connected
                for chunk in backlog:
                    self.wfile.write(chunk)
                self.wfile.flush()
                while True:
                    with _buf_cv:
                        if _buf_epoch[0] != epoch:
                            break            # retuned; this stream is over
                        newer = [(q, c) for q, c in _buf if q > last_seq]
                        if not newer:
                            if not _buf_cv.wait(timeout=20):
                                break
                            continue
                        last_seq = newer[-1][0]
                        out = [c for _, c in newer]
                    for chunk in out:
                        self.wfile.write(chunk)
                    self.wfile.flush()   # do not let a buffer hold audio back
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path.startswith("/audio/"):
            name = os.path.basename(urllib.parse.unquote(path[len("/audio/"):]))
            # basename() alone is the traversal guard; verify the resolved path
            # really sits under VOICE_DIR before opening anything.
            for sub in os.listdir(VOICE_DIR) if os.path.isdir(VOICE_DIR) else []:
                cand = os.path.realpath(os.path.join(VOICE_DIR, sub, name))
                if not cand.startswith(os.path.realpath(VOICE_DIR)):
                    continue
                if os.path.isfile(cand):
                    # rtl_airband writes MPEG-2.5 at 8 kHz -- a valid mp3 that
                    # Chrome refuses to decode. Transcode to MPEG-1 44.1 kHz on
                    # the way out. The clips are a few KB, so this is cheap.
                    try:
                        out = subprocess.run(
                            [FFMPEG, "-loglevel", "error", "-i", cand,
                             "-ar", "44100", "-ac", "1", "-b:a", "64k",
                             "-f", "mp3", "pipe:1"],
                            capture_output=True, timeout=30)
                        data = out.stdout if out.returncode == 0 and out.stdout else None
                    except Exception:
                        data = None
                    if data is None:
                        with open(cand, "rb") as fh:
                            data = fh.read()      # fall back to the original
                    return self._send_media(data)
            return self._send(404, json.dumps({"error": "not found"}))

        return self._send(404, json.dumps({"error": "not found"}))


def ensure_store():
    """Create every table the console reads, if nothing has yet.

    The console is often the first thing a new install opens, before the
    collector has ever run. Without this, several panels return 500 against a
    database that exists but is empty, and the software looks broken when it
    is merely idle.

    Four owners, because the schema genuinely is spread across four modules:
    the collector owns the event store AND ITS MIGRATIONS (CREATE TABLE IF NOT
    EXISTS does not add columns to an existing table, so running the DDL alone
    leaves an older database missing columns the console queries), readings
    owns its own table, and the health and spectrum tables are created by the
    tools that write them. All of it is idempotent, so this is a no-op against
    a live station.
    """
    os.makedirs(VAR, exist_ok=True)
    for label, fn in (("event store", _init_events),
                      ("readings", _init_readings),
                      ("health", _init_health),
                      ("spectrum", _init_spectrum)):
        try:
            fn()
        except Exception as e:                  # noqa: BLE001
            # Never fatal, and never silent: a read-only or unusual store
            # should still serve the pages that do not need that table, but
            # the operator has to be able to see why a panel is empty.
            print("could not initialise %s: %s" % (label, e), flush=True)


def _init_events():
    sys.path.insert(0, os.path.join(ROOT, "broker"))
    import collector as _c
    _c.db_connect().close()          # runs SCHEMA *and* MIGRATIONS


def _init_readings():
    sys.path.insert(0, os.path.join(ROOT, "analysis"))
    import readings as _rd
    con = sqlite3.connect(DB)
    _rd.ensure_schema(con)
    con.commit()
    con.close()


def _init_health():
    sys.path.insert(0, os.path.join(ROOT, "bin"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_bw_health", os.path.join(ROOT, "bin", "bw-health.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    con = sqlite3.connect(DB)
    con.executescript(m.SCHEMA)
    con.commit()
    con.close()


def _init_spectrum():
    sys.path.insert(0, os.path.join(ROOT, "analysis"))
    import spectrum as _sp
    con = sqlite3.connect(DB)
    _sp._ensure_table(con)
    con.close()


if __name__ == "__main__":
    os.makedirs(VOICE_DIR, exist_ok=True)
    ensure_store()
    srv = Server(("0.0.0.0", PORT), Handler)
    print("bandwatch UI on http://0.0.0.0:%d" % PORT, flush=True)
    srv.serve_forever()
