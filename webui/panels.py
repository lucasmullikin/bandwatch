"""Imagery gallery and lane-productivity views for the console.

Two questions the console could not answer, both of which cost something today.

IMAGERY -- satellite captures land as files in var/lrpt and var/apt and there
was nowhere to see them. Worse, the NOAA capture produced eight large PNGs of
pure static and reported success; a gallery that shows the pictures makes that
obvious in one glance instead of requiring someone to think to check.

LANES -- the only question that matters for tuning is what a lane returns for
the radio time it is given, and nothing computed it. Running that by hand
today found APRS discarding every packet it decoded, and ADS-B (841 events per
minute, two orders of magnitude above anything else) receiving 3.7% of the
radio. Both had been true for days and every health check was green.

A lane producing nothing is NOT automatically a fault: guard_121 is aviation
distress and its silence is the desirable outcome. So this reports yield and
lets a human judge, rather than flagging low output as an error.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Lanes whose silence is the CORRECT result. Never present these as
# underperforming -- optimising them away would be removing the alarm because
# the building has not burned down.
EXPECTED_SILENT = {
    "guard_121": "aviation distress 121.5 -- silence is the desired state",
    "milair_guard": "military guard 243.0 -- silent almost always by design",
    "noaa_wx": "weather radio, surveyed for occupancy rather than decoded",
    # measured 2026-09-01: VDL2 arrives at +6..8 dB, D8PSK needs ~15-20, so
    # silence is an antenna limit rather than a dead band
    "vdl2": "arrives ~10 dB below what D8PSK needs -- antenna limited",
    # only tested at night so far, when little general aviation flies.
    # Silence means nothing here until a daylight test.
    "uat978": "unproven -- night test only, when little GA is flying",
    # --- measured by bin/bw-laneprobe.py, 2026-09-02 02:38-02:42Z ---
    # Signal IS present at 914.39 (+24.1 dB) -- the SCMplus gas meters. The
    # width test correctly rejects narrowband, so silence here is the lane
    # working, not failing.
    "lora915": "signal present but narrowband (gas meters) -- correctly not LoRa",
    # 2026-09-02: swept 153.8-155.0 MHz. The three national fire MUTUAL-AID
    # channels this lane watched (154.265/.280/.295) were SILENT, while six
    # adjacent channels were busy -- 154.2395 at +16 dB over floor. Mutual aid
    # only carries traffic during a multi-agency incident, so the lane was never
    # mis-hearing anything: it was tuned to channels quiet BY DESIGN, which is
    # why no antenna change would ever have fixed it. The six busy channels are
    # now monitored alongside. Mutual aid is KEPT -- like guard 121.5, silence
    # there is the signal, and dropping it to chase busier channels would be
    # optimising away the alarm. The retuned config is verified to parse and to
    # open the radio; whether it CAPTURES needs a daytime check, because a 60s
    # test at 03:00 local proves nothing about a bursty land-mobile band.
    "vhf_fire": "retuned 2026-09-02 -- mutual aid quiet by design; 6 measured-busy channels added; awaiting a daytime capture check",
    # +8.1 dB, the same marginal band VDL2 sits in
    "aprs": "+8.1 dB over floor -- marginal, same antenna deficit as VDL2",
    # +9.0 dB, and MURS is lightly used generally
    "murs_voice": "+9.0 dB over floor -- marginal, and MURS is lightly used",
    # right on 929.6 but only +8.7 dB
    "pagers": "+8.7 dB on frequency -- marginal, and pager traffic is sparse here",
    "air_ground_voice": "+7.0 dB over floor -- marginal",
    # a snapshot at 02:40 local night; daytime would be the real test
    "air_survey": "quiet during a 02:40Z sweep -- a night snapshot, not proof",
    "pager_survey": "quiet during a 02:40Z sweep -- a night snapshot, not proof",
}
IMAGE_DIRS = ("lrpt", "apt")


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ imagery

def captures(root, limit=40):
    """Every satellite capture on disk, newest first, with its images."""
    out = []
    for sub in IMAGE_DIRS:
        base = os.path.join(root, "var", sub)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base), reverse=True):
            path = os.path.join(base, name)
            if not os.path.isdir(path):
                continue
            imgs = []
            for r, dirs, files in os.walk(path):
                # never walk the thumbnail cache: its contents would appear as
                # extra captured images, doubling every gallery
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                for f in sorted(files):
                    if f.lower().endswith((".png", ".jpg")):
                        fp = os.path.join(r, f)
                        imgs.append({
                            "name": f,
                            "rel": os.path.relpath(fp, os.path.join(root, "var")),
                            "bytes": os.path.getsize(fp),
                        })
            if not imgs:
                continue
            # the raw/unsync products are diagnostics, not pictures
            imgs.sort(key=lambda i: (0 if ("composite" in i["name"].lower()
                                           or "rgb" in i["name"].lower()) else
                                     1 if "corrected" in i["name"].lower() else
                                     3 if i["name"].lower().startswith("raw") else 2,
                                     i["name"]))
            out.append({
                "id": name,
                "kind": "LRPT" if sub == "lrpt" else "APT",
                "when": datetime.fromtimestamp(os.path.getmtime(path),
                                               timezone.utc).isoformat(timespec="seconds"),
                "images": imgs,
                "count": len(imgs),
            })
    out.sort(key=lambda c: c["when"], reverse=True)
    return out[:limit]


# -------------------------------------------------------------- lane yield

def lane_yield(db, root, hours=None):
    """What each lane returned for the radio time it was given.

    Voice lanes write to `voice` keyed by CHANNEL, surveys write to `spectrum`
    keyed by BAND, and decoders write to `events` keyed by lane. Counting only
    `events` reports every voice and survey lane as producing nothing -- which
    is exactly the wrong answer, and the first version of this query made that
    mistake.
    """
    con = sqlite3.connect(db, timeout=10)
    try:
        where, args = "", []
        if hours:
            cut = (datetime.now(timezone.utc)
                   - timedelta(hours=float(hours))).isoformat(timespec="seconds")
            where, args = " WHERE ended_at>=?", [cut]

        spent, runs = {}, {}
        for lane, a, b in con.execute(
                "SELECT lane, started_at, ended_at FROM coverage" + where, args):
            s, e = _parse(a), _parse(b)
            if not s or not e or e <= s:
                continue
            spent[lane] = spent.get(lane, 0.0) + (e - s).total_seconds()
            runs[lane] = runs.get(lane, 0) + 1

        ev = dict(con.execute("SELECT lane, COUNT(*) FROM events "
                              "GROUP BY lane").fetchall())
        # map a lane to its voice channels via the frequencies it covers is
        # overkill; the recorder names the directory after the lane, so voice
        # rows are attributed by counting all clips whose channel appears in
        # that lane's config. Simpler and honest: total voice per lane dir.
        vdir = os.path.join(root, "var", "voice")
        voice = {}
        if os.path.isdir(vdir):
            for d in os.listdir(vdir):
                p = os.path.join(vdir, d)
                if os.path.isdir(p) and d != "rejected":
                    voice[d] = len([f for f in os.listdir(p)
                                    if f.lower().endswith((".mp3", ".wav"))])
        spec = dict(con.execute("SELECT band, COUNT(*) FROM spectrum "
                                "GROUP BY band").fetchall())

        rows = []
        for lane, secs in spent.items():
            mins = secs / 60.0
            n, kind = ev.get(lane, 0), "events"
            if not n:
                for d, c in voice.items():
                    if d and (d in lane or lane.replace("_voice", "") in d):
                        n, kind = c, "clips"
                        break
            if not n:
                key = lane.replace("_survey", "").replace("survey_", "")
                if key in spec:
                    n, kind = spec[key], "carriers"
            rows.append({
                "lane": lane, "minutes": round(mins, 1), "runs": runs.get(lane, 0),
                "output": n, "unit": kind,
                "per_min": round(n / mins, 2) if mins > 0 else 0.0,
                "expected_silent": lane in EXPECTED_SILENT,
                "note": EXPECTED_SILENT.get(lane),
            })
        rows.sort(key=lambda r: -r["minutes"])
        total = sum(r["minutes"] for r in rows) or 1
        for r in rows:
            r["share_pct"] = round(100.0 * r["minutes"] / total, 1)
        return {
            "lanes": rows,
            "total_minutes": round(total, 1),
            "caveat": ("Silence is not automatically a fault. guard_121 is "
                       "aviation distress -- zero output there is the desired "
                       "result, and cutting it would be removing the alarm "
                       "because nothing has gone wrong yet."),
        }
    finally:
        con.close()


def transcription_status(db, root, hours=24):
    """What the transcriber did, and -- more usefully -- what it did NOT do.

    Three different things all look like "no transcript" in the database and
    they need opposite responses:

      NOT SELECTED   the channel is not on the transcribe_channels allowlist.
                     Working as configured; airband is deliberately excluded.
      REJECTED       transcribed, then thrown away by the quality gate because
                     Whisper was fabricating. 59% of airband transcripts were
                     invented text before that gate existed, so these are a
                     SUCCESS, not a loss -- but they were invisible until now.
      PENDING/AGED   never processed. Clips older than transcribe_max_age_min
                     are abandoned on purpose rather than queued forever.

    Presenting them as one number is how a working filter and a broken
    transcriber look identical.
    """
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout = 5000")
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    try:
        cfg = json.load(open(os.path.join(root, "collector.json")))
    except Exception:
        cfg = {}
    allow = set(cfg.get("transcribe_channels") or [])
    max_age = float(cfg.get("transcribe_max_age_min", 0) or 0)

    rows = []
    try:
        q = con.execute(
            "SELECT COALESCE(channel,'?'), COUNT(*), "
            "SUM(CASE WHEN transcript IS NOT NULL AND transcript != '' "
            "         THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN rejected_because IS NOT NULL "
            "         AND rejected_because != '' THEN 1 ELSE 0 END) "
            "FROM voice WHERE ts >= ? GROUP BY channel "
            "ORDER BY COUNT(*) DESC", (cut,)).fetchall()
    except sqlite3.Error:
        q = []
    for ch, n, done, rej in q:
        # an empty allowlist means "everything", which is not the same as
        # "nothing" -- getting that backwards would report the whole system
        # as switched off
        selected = (ch in allow) if allow else True
        rows.append({
            "channel": ch, "recorded": n, "transcribed": done or 0,
            "rejected": rej or 0,
            "untranscribed": n - (done or 0) - (rej or 0),
            "selected": selected,
        })

    reasons = []
    try:
        for why, n in con.execute(
                "SELECT rejected_because, COUNT(*) FROM voice "
                "WHERE ts >= ? AND rejected_because IS NOT NULL "
                "AND rejected_because != '' GROUP BY rejected_because "
                "ORDER BY COUNT(*) DESC LIMIT 10", (cut,)).fetchall():
            reasons.append({"reason": why, "count": n})
    except sqlite3.Error:
        pass
    con.close()

    sel = [r for r in rows if r["selected"]]
    return {
        "hours": hours,
        "mode": cfg.get("transcribe_mode", "all"),
        "allowlist": sorted(allow),
        "allowlist_active": bool(allow),
        "max_age_min": max_age,
        "channels": rows,
        "reject_reasons": reasons,
        "totals": {
            "recorded": sum(r["recorded"] for r in rows),
            "selected_recorded": sum(r["recorded"] for r in sel),
            "transcribed": sum(r["transcribed"] for r in rows),
            "rejected": sum(r["rejected"] for r in rows),
            "not_selected": sum(r["recorded"] for r in rows
                                if not r["selected"]),
        },
        "caveat": ("Rejected clips are a SUCCESS: the gate caught Whisper "
                   "inventing text. Not-selected channels are recorded and "
                   "kept, just not sent to the GPU."),
    }


# Metrics worth a chart, and the QUESTION each one answers. A dashboard that
# plots everything recorded answers nothing -- bw-health.py stores 67 metrics
# a run and most of them are only interesting when something else is already
# wrong.
TREND_METRICS = [
    ("events_1h", "_total", "Events per hour", "",
     "Is the system hearing anything at all?"),
    ("age_min", "events", "Ingest age", "min",
     "Minutes since the last event stored. Climbing means ingest stalled."),
    ("age_min", "readings", "Sensor age", "min",
     "Minutes since the last decoded sensor reading."),
    ("age_min", "voice", "Voice age", "min",
     "Minutes since the last recording. Long gaps are normal overnight."),
    ("watchdog", "age_min", "Watchdog age", "min",
     "Minutes since the checker last ran. If this climbs, the thing that "
     "notices problems has itself stopped."),
    ("disk", "free_gb", "Disk free", "GB",
     "Headroom. Falling steadily is the signal, not the absolute number."),
    ("disk", "var_mb", "Data size", "MB",
     "How much the capture directory is holding."),
]


def _series(con, section, metric, since):
    rows = con.execute(
        "SELECT ts, value FROM health WHERE section=? AND metric=? AND ts>=? "
        "AND value IS NOT NULL ORDER BY ts", (section, metric, since)).fetchall()
    return [(t, float(v)) for t, v in rows]


def health_trends(db, hours=24):
    """Trends, so "is it better now?" is a query rather than another manual dig.

    Every tuning decision made on this system so far rested on a number someone
    measured by hand and then lost: ADS-B at 839 events/min against everything
    else's single digits, VDL2 arriving 10 dB short, a noise floor that moved
    0.56 dB in six hours. None of it was recorded, so the next question needed
    the same dig again.

    Reports AVAILABILITY from the process samples rather than just current
    state, because "is it up now?" and "has it been up all day?" are different
    questions and only the second one catches a crash loop that self-heals.
    """
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout = 5000")
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds")

    runs = [r[0] for r in con.execute(
        "SELECT DISTINCT ts FROM health WHERE ts>=? ORDER BY ts", (since,))]

    trends = []
    for section, metric, label, unit, question in TREND_METRICS:
        pts = _series(con, section, metric, since)
        if not pts:
            continue
        vals = [v for _, v in pts]
        trends.append({
            "label": label, "unit": unit, "question": question,
            "points": [round(v, 2) for v in vals],
            "current": round(vals[-1], 2),
            "min": round(min(vals), 2), "max": round(max(vals), 2),
            "first_ts": pts[0][0], "last_ts": pts[-1][0],
        })

    # Availability: a component that was down and came back looks identical to
    # one that never faltered if you only ever ask "is it up NOW?"
    procs = []
    for (name,) in con.execute(
            "SELECT DISTINCT metric FROM health WHERE section='process' "
            "AND ts>=? ORDER BY metric", (since,)):
        pts = _series(con, "process", name, since)
        if not pts:
            continue
        up = sum(1 for _, v in pts if v >= 1.0)
        procs.append({
            "name": name, "samples": len(pts), "up": up,
            "uptime_pct": round(100.0 * up / len(pts), 1),
            "now": bool(pts[-1][1] >= 1.0),
            "points": [1 if v >= 1.0 else 0 for _, v in pts],
        })

    # Lane productivity, only lanes that actually have a series
    lanes = []
    for (name,) in con.execute(
            "SELECT DISTINCT metric FROM health WHERE section='lane_per_min' "
            "AND ts>=? ORDER BY metric", (since,)):
        pts = _series(con, "lane_per_min", name, since)
        if len(pts) < 2:
            continue
        vals = [v for _, v in pts]
        lanes.append({"lane": name, "current": round(vals[-1], 2),
                      "max": round(max(vals), 2),
                      "points": [round(v, 3) for v in vals]})
    lanes.sort(key=lambda x: -x["max"])
    con.close()

    return {
        "hours": hours,
        "runs": len(runs),
        "first_run": runs[0] if runs else None,
        "last_run": runs[-1] if runs else None,
        "trends": trends,
        "processes": procs,
        "lanes": lanes[:12],
        "caveat": ("Each point is one snapshot roughly every 15 minutes, not a "
                   "continuous measurement: a gap shorter than the interval is "
                   "invisible here. Uptime is the FRACTION OF SAMPLES a process "
                   "was seen, so 100% means it was up every time we looked."),
    }
