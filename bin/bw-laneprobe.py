#!/usr/bin/env python3
"""Why is a lane silent? Sweep its frequency and find out.

Eight lanes consume roughly 730 minutes of radio a day and produce nothing,
with no recorded reason. "0 events" is consistent with three completely
different situations that require opposite responses:

  SIGNAL ABSENT      the band really is quiet here. Correct behaviour; the
                     lane should keep a short dwell or be marked expected.
  SIGNAL PRESENT     something transmits and we cannot read it. Either SNR
                     (like VDL2 at 10 dB short) or a broken decoder (like the
                     APRS filter that discarded every packet it decoded).
  NEVER RAN          not a reception question at all.

Guessing between them is what produced two wrong conclusions today, so this
measures instead: sweep a window around each lane's frequency and report the
strongest excursion above that band's own noise floor.

What it CANNOT do, stated because the distinction matters: a sweep is a
snapshot. A band that is busy only at 3pm looks dead at 3am, and an
intermittent transmitter can be missed entirely. So a "quiet" result here
bounds what was there DURING THE SWEEP -- it is evidence, not proof, and the
verdict says so.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
BIN = os.path.join(os.path.expanduser("~"), "homebrew", "bin")
STATE = os.path.join(VAR, "state.json")
PAUSE_FMT = "/tmp/sdr_pause_dev%s"

# lane -> (device key in the profile, centre MHz, span kHz, what it is)
LANES = {
    "aprs":             ("0", 144.390, 120, "APRS 1200 baud AFSK"),
    "vhf_fire":         ("0", 154.280, 200, "VHF fire dispatch"),
    "murs_voice":       ("0", 151.880, 200, "MURS channels 1-3"),
    "air_ground_voice": ("0", 121.700, 120, "BOI ground"),
    "air_survey":       ("0", 127.000, 4000, "airband sweep"),
    "pagers":           ("1", 929.600, 200, "POCSAG/FLEX pager"),
    "pager_survey":     ("1", 930.500, 3000, "929-932 pager band"),
    "lora915":          ("1", 915.000, 4000, "902-928 ISM / LoRa"),
}
_paused = []


def log(m):
    print("%s  %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), m),
          flush=True)


def cleanup(*_a):
    for d in list(_paused):
        try:
            os.unlink(PAUSE_FMT % d)
        except OSError:
            pass
        _paused.remove(d)


def phys_for(devkey):
    try:
        st = json.load(open(STATE))
        d = (st.get("devices") or {}).get(str(devkey)) or {}
        return str(d.get("physical_index", devkey))
    except Exception:
        return str(devkey)


def wait_release(phys, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = json.load(open(STATE))
            for dev in (st.get("devices") or {}).values():
                if (str(dev.get("physical_index")) == str(phys)
                        and dev.get("paused") and not dev.get("current_lane")):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def sweep(phys, centre_mhz, span_khz, seconds, gain=45):
    lo = centre_mhz - span_khz / 2000.0
    hi = centre_mhz + span_khz / 2000.0
    step = "2k" if span_khz <= 300 else "25k"
    out = "/tmp/laneprobe_%s.csv" % phys
    cmd = [os.path.join(BIN, "rtl_power"), "-d", str(phys),
           "-f", "%.4fM:%.4fM:%s" % (lo, hi, step),
           "-g", str(gain), "-i", "3", "-e", str(seconds), out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=seconds + 60,
                       stdin=subprocess.DEVNULL)
    except Exception as e:
        log("  rtl_power failed: %s" % e)
        return []
    peak = {}
    try:
        with open(out) as fh:
            for row in csv.reader(fh):
                if len(row) < 7:
                    continue
                try:
                    f0, st = float(row[2]), float(row[4])
                except ValueError:
                    continue
                for i, c in enumerate(row[6:]):
                    try:
                        v = float(c)
                    except ValueError:
                        continue
                    f = f0 + i * st
                    if f not in peak or v > peak[f]:
                        peak[f] = v
    except OSError:
        return []
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    return sorted(peak.items())


def median(xs):
    s = sorted(xs)
    return None if not s else (s[len(s) // 2] if len(s) % 2
                               else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2.0)


SAFETY_MARGIN_MIN = 12


def capture_imminent():
    """Minutes until an armed satellite capture needs a radio, or None.

    Reads the armed pass time out of the capture log rather than guessing:
    the capture prints its window when it arms.
    """
    try:
        r = subprocess.run(["pgrep", "-f", "[s]dr-capture-meteor"],
                           capture_output=True, timeout=6)
        if r.returncode != 0:
            return None
    except Exception:
        return None
    for name in ("meteorcapture2.log", "meteorcapture.log"):
        path = os.path.join(VAR, "logs", name)
        try:
            for line in open(path):
                if "pass " not in line or "->" not in line:
                    continue
                # everything BEFORE "pass " is the log's own timestamp. Taking
                # the first HH:MM:SS on the whole line picked that instead of
                # the pass, and reported 1217 minutes when the pass was 100
                # minutes away -- the probe would have judged itself safe and
                # taken a radio mid-pass.
                tail = line.split("pass ", 1)[1]
                for tok in tail.split():
                    if tok.count(":") == 2 and len(tok) == 8:
                        try:
                            hh, mm, ss = (int(x) for x in tok.split(":"))
                        except ValueError:
                            continue
                        now = datetime.now(timezone.utc)
                        tgt = now.replace(hour=hh, minute=mm, second=ss,
                                          microsecond=0)
                        mins = (tgt - now).total_seconds() / 60.0
                        if mins < -60:
                            mins += 1440          # the pass is tomorrow
                        if mins >= 0:
                            return mins
        except OSError:
            continue
    return None


def probe(lane, seconds=30, gain=45):
    devkey, centre, span, what = LANES[lane]
    phys = phys_for(devkey)
    open(PAUSE_FMT % phys, "w").write(str(os.getpid()))
    _paused.append(phys)
    if not wait_release(phys):
        log("  %s: broker never confirmed release" % lane)
    pts = sweep(phys, centre, span, seconds, gain)
    cleanup()
    if not pts:
        return {"lane": lane, "verdict": "no data", "peak_db": None}
    floor = median([d for _, d in pts])
    best_f, best_d = max(pts, key=lambda p: p[1])
    excess = best_d - floor
    if excess >= 10:
        verdict = "SIGNAL PRESENT -- decoder or SNR, not an empty band"
    elif excess >= 5:
        verdict = "marginal -- something is there, close to the floor"
    else:
        verdict = "quiet during this sweep"
    return {"lane": lane, "what": what, "centre_mhz": centre,
            "span_khz": span, "phys": phys, "floor_db": round(floor, 2),
            "peak_mhz": round(best_f / 1e6, 4), "peak_db": round(excess, 1),
            "verdict": verdict}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lanes", nargs="*", default=None)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--gain", type=int, default=45)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    import signal as _s
    _s.signal(_s.SIGINT, lambda *x: (cleanup(), sys.exit(1)))
    _s.signal(_s.SIGTERM, lambda *x: (cleanup(), sys.exit(1)))

    mins = capture_imminent()
    if mins is not None and mins < SAFETY_MARGIN_MIN:
        log("REFUSING TO RUN: a satellite capture needs a radio in %.0f min "
            "(margin %d min). A pass cannot be re-flown; this probe can be "
            "re-run any time." % (mins, SAFETY_MARGIN_MIN))
        return 2
    if mins is not None:
        log("capture armed in %.0f min -- safe to probe now, refused inside "
            "%d min" % (mins, SAFETY_MARGIN_MIN))

    want = a.lanes or list(LANES)
    out = []
    try:
        for ln in want:
            if ln not in LANES:
                log("unknown lane %s" % ln)
                continue
            log("probing %-17s %.3f MHz +/-%d kHz"
                % (ln, LANES[ln][1], LANES[ln][2] // 2))
            r = probe(ln, a.seconds, a.gain)
            out.append(r)
            log("  floor %-8s peak +%-5s dB at %-9s  %s"
                % (r.get("floor_db"), r.get("peak_db"),
                   r.get("peak_mhz"), r["verdict"]))
    finally:
        cleanup()

    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print()
        print("  %-17s %-9s %-8s %s" % ("lane", "peak dB", "at MHz", "verdict"))
        for r in out:
            print("  %-17s %-9s %-8s %s"
                  % (r["lane"], r.get("peak_db"), r.get("peak_mhz"),
                     r["verdict"]))
        print()
        print("  A sweep is a SNAPSHOT. A band busy only in daytime looks dead")
        print("  at night, so 'quiet' bounds what was there DURING the sweep --")
        print("  evidence, not proof.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
