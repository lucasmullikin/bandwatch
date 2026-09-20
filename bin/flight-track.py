#!/usr/bin/env python3
"""Track ONE aircraft into or out of BOI on the home receiver, and push Signal.

  flight-track.py --arrive 22:50 --from PHX
  flight-track.py --callsign SWA1601 --arrive 22:50
  flight-track.py --depart 07:15 --to SEA
  flight-track.py --arrive 23:10 --from LAS --hold 3

Runs ON THE MINI: readsb's BaseStation feed (127.0.0.1:30003) and signal-cli
(127.0.0.1:8085) are both loopback-bound.

By default it takes a bounded hold on the radio first (profile event-track,
ADS-B pinned alone on the high-band dongle) and RELEASES it on the way out,
whatever happens. Without that the supervisor rotates 1090 away mid-approach:
on 2026-09-11 it SIGTERM'd a hand-started readsb after 31 minutes.

Two failures from that night are designed against here:
  * the feed died for 30 min and nothing said so -- a watcher that only looks
    for what it wants cannot report silence, so feed health is alerted on
    explicitly and separately;
  * a DEPARTURE was locked as the arrival because "alt < 24000" was used as a
    proxy for "on approach" -- a climbing aircraft is now disqualified outright.
"""
import argparse
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bandwatch_config as C  # noqa: E402

# The airport this tracker measures approaches against. No default: a tracker
# pointed at the wrong airport still produces confident distances and altitudes,
# and nothing about the output looks wrong.
_cfg = C.load("bandwatch")
SITE_LAT = C.require(_cfg, "adsb_alert_airport_lat",
                     "flight-track measures every approach against this airport.")
SITE_LON = C.require(_cfg, "adsb_alert_airport_lon",
                     "flight-track measures every approach against this airport.")
SITE_ELEV = float((_cfg.get("station") or {}).get("alt_m") or 0.0) * 3.28084
SBS = ("127.0.0.1", 30003)
HOLD = os.path.join(ROOT, "bin", "bw-hold")
LOG = os.path.join(ROOT, "var", "logs", "flight-track.log")

AIRLINE = re.compile(r"^[A-Z]{3}\d{1,4}$")
REPORT_EVERY = 120
STALE_AFTER = 90
FEED_SILENT_ALERT = 120       # no SBS bytes this long => tell the operator
SECTOR_HALF = 75.0            # accept origins within this many deg of bearing

AIRPORTS = {
    "PHX": (33.4342, -112.0116), "LAS": (36.0840, -75.1537),
    "SEA": (47.4502, -122.3088), "PDX": (45.5887, -122.5975),
    "DEN": (39.8561, -104.6737), "SLC": (40.7899, -111.9791),
    "LAX": (33.9416, -118.4085), "SFO": (37.6213, -122.3790),
    "OAK": (37.7213, -122.2207), "SAN": (32.7338, -117.1933),
    "MSP": (44.8848, -93.2223),  "DFW": (32.8998, -97.0403),
    "ORD": (41.9742, -87.9073),  "SJC": (37.3639, -121.9289),
    "SITE": (SITE_LAT, SITE_LON),
}


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def signal_send(text):
    """Send through the configured notifier.

    Named for history; it is no longer Signal-specific. See notify/README.md.
    """
    sys.path.insert(0, os.path.join(ROOT, "notify"))
    import notifiers
    ok, detail = notifiers.send(_cfg, text)
    if not ok:
        log("NOTIFY ERROR: %s" % (detail or "no detail")[:200])
        return False
    log("notify: %s" % text.replace("\n", " | ")[:150])
    return True


def geo(lat, lon):
    dx = (lon - SITE_LON) * math.cos(math.radians(SITE_LAT)) * 60.0
    dy = (lat - SITE_LAT) * 60.0
    return math.hypot(dx, dy), (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def bearing_to(code):
    if code not in AIRPORTS:
        return None
    return geo(*AIRPORTS[code])[1]


def angdiff(a, b):
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def compass(b):
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((b + 11.25) % 360 / 22.5)]


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Plane:
    __slots__ = ("hex", "callsign", "alt", "gs", "trk", "vr", "ground", "seen",
                 "first", "dist", "brg", "prev", "min_dist", "max_alt",
                 "entry_brg", "entry_dist")

    def __init__(self, h):
        self.hex = h
        self.callsign = ""
        self.alt = self.gs = self.trk = self.vr = None
        self.ground = False
        self.seen = self.first = time.time()
        self.dist = self.brg = self.prev = None
        self.min_dist = 9999.0
        self.max_alt = 0.0
        self.entry_brg = self.entry_dist = None


def parse(line, fleet):
    f = line.rstrip().split(",")
    if len(f) < 22 or f[0] != "MSG":
        return
    h = f[4].strip()
    if not h:
        return
    p = fleet.get(h) or fleet.setdefault(h, Plane(h))
    p.seen = time.time()
    if f[10].strip():
        p.callsign = f[10].strip()
    for i, a in ((11, "alt"), (12, "gs"), (13, "trk"), (16, "vr")):
        v = fnum(f[i])
        if v is not None:
            setattr(p, a, v)
    lat, lon = fnum(f[14]), fnum(f[15])
    if lat is not None and lon is not None and (lat or lon):
        d, b = geo(lat, lon)
        if p.entry_dist is None:
            p.entry_dist, p.entry_brg = d, b
        p.prev, p.dist, p.brg = p.dist, d, b
        p.min_dist = min(p.min_dist, d)
    if p.alt is not None:
        p.max_alt = max(p.max_alt, p.alt)
    g = f[21].strip()
    if g in ("-1", "1"):
        p.ground = True
    elif g == "0":
        p.ground = False


def score_arrival(p, origin_brg, target_epoch):
    """None = not the arrival. Origin bearing and descent carry the weight;
    proximity is deliberately secondary -- weighting it first made a cruising
    overflight 10nm overhead outscore a genuine arrival 150nm out."""
    if not p.callsign or not AIRLINE.match(p.callsign):
        return None
    if p.dist is None or time.time() - p.first < 15 or p.dist > 260:
        return None
    if p.dist > p.min_dist + 8:
        return None                       # receding: a departure or already past
    if p.vr is not None and p.vr > 400:
        return None                       # climbing is never an arrival
    if p.alt is not None and (p.alt > 42000 or (p.dist < 30 and p.alt > 16000)):
        return None                       # cruise this close = overflight
    ebrg = p.entry_brg if (p.entry_dist or 0) > 60 else p.brg
    off = angdiff(ebrg, origin_brg) if origin_brg is not None else 0.0
    if origin_brg is not None and off > SECTOR_HALF:
        return None
    descending = (p.vr is not None and p.vr < -250) or \
                 (p.vr is None and p.alt is not None and p.alt < 20000)
    if not descending and p.dist < 120:
        return None
    s = 100.0 * max(0.0, 1 - off / SECTOR_HALF)
    s += 60.0 if descending else 0.0
    s += 40.0 * max(0.0, 1 - p.dist / 260.0)
    if (p.entry_dist or 0) > 120:
        s += 40.0
    if p.max_alt >= 28000:
        s += 30.0
    if p.trk is not None and origin_brg is not None:
        s += 40.0 * max(0.0, 1 - angdiff(p.trk, origin_brg + 180) / 90.0)
    if target_epoch and p.gs and p.gs > 60:
        slip = abs(time.time() + p.dist / p.gs * 3600.0 - target_epoch) / 60.0
        s += 70.0 * max(0.0, 1 - slip / 35.0)
    return s


def score_departure(p, dest_brg, target_epoch):
    """A departure is the mirror image: close, climbing, getting further away."""
    if not p.callsign or not AIRLINE.match(p.callsign):
        return None
    if p.dist is None or time.time() - p.first < 15:
        return None
    if p.min_dist > 25:
        return None                       # never near the field: not from here
    if p.dist > 150:
        return None
    climbing = (p.vr is not None and p.vr > 300) or \
               (p.dist > p.min_dist + 3 and (p.alt or 0) > SITE_ELEV + 1500)
    if not climbing:
        return None
    s = 100.0 * max(0.0, 1 - p.dist / 150.0)
    s += 60.0
    if dest_brg is not None and p.trk is not None:
        s += 60.0 * max(0.0, 1 - angdiff(p.trk, dest_brg) / 90.0)
    if target_epoch:
        slip = abs(time.time() - target_epoch) / 60.0
        s += 50.0 * max(0.0, 1 - slip / 40.0)
    return s


def stats(p, tag, mode):
    out = ["%s  %s" % (tag, p.callsign)]
    if p.dist is not None:
        out.append("%.0f nm %s of BOI" % (p.dist, compass(p.brg)))
    alt = "?" if p.alt is None else "{:,} ft".format(int(p.alt))
    if p.vr is not None and abs(p.vr) > 100:
        alt += "  %s%d fpm" % ("↓" if p.vr < 0 else "↑", abs(int(p.vr)))
    out.append(alt)
    if p.gs is not None:
        out.append("%d kt%s" % (int(p.gs),
                                "  hdg %03d" % int(p.trk) if p.trk is not None else ""))
    if mode == "arrive" and p.dist is not None and p.gs and p.gs > 60:
        out.append("touchdown in ~%d min" % max(1, round(p.dist / p.gs * 60.0)))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--callsign", help="lock this callsign exactly, skip detection")
    ap.add_argument("--arrive", help="expected landing HH:MM local")
    ap.add_argument("--depart", help="expected takeoff HH:MM local")
    ap.add_argument("--from", dest="origin", help="origin airport, e.g. PHX")
    ap.add_argument("--to", dest="dest", help="destination airport, e.g. SEA")
    ap.add_argument("--hold", type=float, default=2.0, help="hours to hold the radio")
    ap.add_argument("--no-hold", action="store_true", help="do not touch the profile")
    ap.add_argument("--quiet", action="store_true", help="no Signal, log only")
    a = ap.parse_args()

    mode = "depart" if a.depart else "arrive"
    want = (a.callsign or "").upper().strip()
    code = (a.dest if mode == "depart" else a.origin)
    code = (code or "").upper().strip() or None
    ref_brg = bearing_to(code) if code else None
    if code and ref_brg is None:
        sys.exit("unknown airport %s -- add it to AIRPORTS" % code)

    target_epoch = None
    hhmm = a.depart or a.arrive
    if hhmm:
        h, m = (int(x) for x in hhmm.split(":"))
        t = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        if t < datetime.now() - timedelta(hours=6):
            t += timedelta(days=1)
        target_epoch = t.timestamp()

    def emit(text):
        if not a.quiet:
            signal_send(text)
        else:
            log("(quiet) %s" % text.replace("\n", " | "))

    held = False
    if not a.no_hold:
        r = subprocess.run([HOLD, "hold", "event-track", str(a.hold)],
                           capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        held = r.returncode == 0
        log("radio hold %s" % ("taken" if held else "FAILED -- continuing unpinned"))
        if held:
            time.sleep(12)          # let the broker bring the adsb lane up

    try:
        run(mode, want, ref_brg, code, target_epoch, emit)
    finally:
        if held:
            r = subprocess.run([HOLD, "release"], capture_output=True, text=True)
            sys.stdout.write(r.stdout)
            log("radio hold released")


def run(mode, want, ref_brg, code, target_epoch, emit):
    verb = "departure" if mode == "depart" else "arrival"
    emit("🛰 Tracking the %s%s%s.\nADS-B pinned on the home receiver; scheduled "
         "scanning paused. I'll ping you when it shows on 1090 MHz, then every "
         "2 min." % (verb,
                     " of %s" % want if want else "",
                     " from %s" % code if code and mode == "arrive"
                     else (" to %s" % code if code else "")))
    log("mode=%s want=%s ref_brg=%s target=%s"
        % (mode, want or "(auto)", ref_brg, target_epoch))

    fleet, locked = {}, None
    sock, buf = None, ""
    last_report = last_note = 0.0
    last_bytes = time.time()
    feed_alerted = False
    landed_at = None
    started = time.time()
    scorer = score_departure if mode == "depart" else score_arrival

    while time.time() - started < 4 * 3600:
        if sock is None:
            try:
                sock = socket.create_connection(SBS, timeout=10)
                sock.settimeout(5)
                log("connected to SBS feed")
                if feed_alerted:
                    emit("📡 Receiver feed is back. Still tracking.")
                    feed_alerted = False
                last_bytes = time.time()
            except OSError as e:
                if not feed_alerted and time.time() - last_bytes > FEED_SILENT_ALERT:
                    feed_alerted = True
                    emit("⚠️ The 1090 feed is DOWN (%s). Nothing is being received "
                         "— this is NOT 'no aircraft yet'. Trying to reconnect." % e)
                time.sleep(5)
                continue
        try:
            chunk = sock.recv(65536).decode("ascii", "replace")
            if not chunk:
                raise OSError("feed closed")
            buf += chunk
            last_bytes = time.time()
        except socket.timeout:
            pass
        except OSError as e:
            log("feed error %s" % e)
            try:
                sock.close()
            except OSError:
                pass
            sock = None
            continue

        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            parse(line, fleet)

        now = time.time()
        # Silence is a result in its own right and must be reported as one.
        if now - last_bytes > FEED_SILENT_ALERT and not feed_alerted:
            feed_alerted = True
            emit("⚠️ No ADS-B data for %d s. The receiver is connected but "
                 "silent — treat this as a fault, not as 'nothing inbound'."
                 % int(now - last_bytes))

        for h in [h for h, p in fleet.items()
                  if now - p.seen > STALE_AFTER and p is not locked]:
            del fleet[h]

        if locked is None:
            if want:
                cand = [p for p in fleet.values() if p.callsign == want]
                locked = cand[0] if cand else None
                best = 999 if locked else None
            else:
                ranked = sorted(((scorer(p, ref_brg, target_epoch), p)
                                 for p in fleet.values()),
                                key=lambda t: (t[0] is None, -(t[0] or 0)))
                top = ranked[0] if ranked and ranked[0][0] is not None else None
                locked, best = (top[1], top[0]) if top else (None, None)
                others = [r[1].callsign for r in ranked[1:4] if r[0] is not None] \
                    if top else []
            if locked is not None:
                log("LOCKED %s score=%s dist=%s alt=%s vr=%s"
                    % (locked.callsign, best, locked.dist, locked.alt, locked.vr))
                msg = stats(locked, "✈️ On the SDR —", mode)
                if not want:
                    msg += "\n\nBest match. Also inbound: %s" % (
                        ", ".join(others) if others else "nothing else")
                    msg += "\nTell me if it's the wrong one."
                emit(msg)
                last_report = now
            elif now - last_note > 300:
                last_note = now
                log("no candidate; %d aircraft tracked" % len(fleet))
            continue

        p, stale = locked, now - locked.seen

        if mode == "arrive":
            if landed_at is None and (p.ground or
                    (p.alt is not None and p.alt <= SITE_ELEV + 400
                     and (p.gs or 0) < 140 and (p.dist or 99) < 6)):
                landed_at = now
                emit("🛬 %s is down at BOI.\nOn the rollout — I'll say when it "
                     "stops at the gate." % p.callsign)
                last_report = now
                continue
            if landed_at is not None:
                parked = p.ground and (p.gs is not None and p.gs < 3)
                # Crews kill the transponder at the gate, so a clean signal
                # loss after touchdown is the EXPECTED tell, not a fault.
                if (parked and now - landed_at > 60) or stale > 150:
                    emit("🅿️ %s is at the gate.\n%.0f min from touchdown to stop."
                         % (p.callsign, (now - landed_at) / 60.0))
                    log("=== done ===")
                    return
        else:
            if (p.dist or 0) > 120 or stale > 150 or (p.alt or 0) > 28000:
                emit("🛫 %s is away — %.0f nm out at %s ft. Out of receiver "
                     "range now."
                     % (p.callsign, p.dist or 0, "{:,}".format(int(p.alt or 0))))
                log("=== done ===")
                return

        if now - last_report >= REPORT_EVERY:
            last_report = now
            if stale > 60 and landed_at is None:
                emit("📡 Lost %s briefly — dropout or out of range. Still "
                     "listening." % p.callsign)
            else:
                tag = "🛬 On the ground —" if landed_at else \
                      ("🛫" if mode == "depart" else "✈️")
                emit(stats(p, tag, mode))

    emit("⏹ Tracking timed out after 4 h. Stopping and releasing the radio.")


if __name__ == "__main__":
    sys.exit(main())
