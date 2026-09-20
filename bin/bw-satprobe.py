#!/usr/bin/env python3
"""Is a satellite carrier actually ARRIVING? Measure, do not infer.

    bw-satprobe.py --floor        is the antenna even coupling at 137 MHz?
    bw-satprobe.py --pass <ISO>   watch 137.9 across a pass for a carrier

Two failed captures in one day taught the same lesson twice: a decoder that
reports NOSYNC cannot tell you WHY. NOAA-18 gave noise because the satellite
was switched off. Meteor gave noise for some other reason, and satdump's
"Peak SNR 2.1 dB, BER 0.42" is equally consistent with a dead transmitter, a
disconnected antenna, wrong gain, or a pointing problem.

This separates those, using measurements a decoder never makes.

TEST 1 -- NOISE FLOOR (--floor). A connected antenna picks up atmospheric and
man-made noise, lifting the floor well above the receiver's own thermal noise.
An antenna that is disconnected, badly mismatched, or shielded shows a floor
near the receiver's internal noise. So comparing the 137 MHz floor against a
band the same antenna demonstrably hears (FM broadcast, 88-108) says whether
the antenna is coupling AT 137 -- which SWR alone would not tell you and no
decoder reports.

TEST 2 -- DOPPLER SIGNATURE (--pass). During a pass a LEO satellite's carrier
sweeps roughly +/-3 kHz at 137 MHz, high to low, crossing the nominal
frequency at closest approach. That drift is the signature: noise does not
drift, and a local interferer does not drift either. If a carrier appears,
sweeps down through 137.9, and vanishes, the satellite is transmitting and any
failure is ours. If nothing appears across a 57-degree pass, it is not.

Neither test decodes anything. That is the point -- they are independent of
the decoder that has already told us "no" twice without saying why.
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(os.path.expanduser("~"), "homebrew", "bin")
STATE = os.path.join(ROOT, "var", "state.json")
PAUSE_FMT = "/tmp/sdr_pause_dev%s"
LRPT_MHZ = 137.9

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


def low_band_phys():
    try:
        st = json.load(open(STATE))
        for k, dev in (st.get("devices") or {}).items():
            if "LOW BAND" in (dev.get("label") or "").upper():
                return str(dev.get("physical_index", k))
    except Exception:
        pass
    return "0"


def wait_release(phys, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st = json.load(open(STATE))
            for dev in (st.get("devices") or {}).values():
                if str(dev.get("physical_index")) == str(phys) \
                        and dev.get("paused") and not dev.get("current_lane"):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def grab(phys):
    open(PAUSE_FMT % phys, "w").write(str(os.getpid()))
    _paused.append(phys)
    return wait_release(phys)


def sweep(dev, band, seconds, gain, bins="2k"):
    """One rtl_power sweep -> [(hz, db)] using peak hold per bin."""
    out = "/tmp/satprobe_%s.csv" % dev
    cmd = [os.path.join(BIN, "rtl_power"), "-d", str(dev), "-f", band,
           "-g", str(gain), "-i", "2", "-e", str(seconds), out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=seconds + 60,
                       stdin=subprocess.DEVNULL)
    except Exception as e:
        log("rtl_power failed: %s" % e)
        return []
    peak = {}
    try:
        with open(out) as fh:
            for row in csv.reader(fh):
                if len(row) < 7:
                    continue
                try:
                    lo, step = float(row[2]), float(row[4])
                except ValueError:
                    continue
                for i, c in enumerate(row[6:]):
                    try:
                        v = float(c)
                    except ValueError:
                        continue
                    f = lo + i * step
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
    n = len(s)
    return None if not n else (s[n // 2] if n % 2
                               else (s[n // 2 - 1] + s[n // 2]) / 2.0)


def floor_test(gain=48):
    """Compare the 137 MHz noise floor against a band the antenna demonstrably
    hears. A floor that does not lift means the antenna is not coupling."""
    phys = low_band_phys()
    log("using physical radio %s" % phys)
    if not grab(phys):
        log("WARNING: broker never confirmed release; results may be wrong")
    bands = [("FM broadcast 88-108", "88M:108M:100k", 12),
             ("air 118-137", "118M:137M:100k", 12),
             ("SAT 136-138", "136M:138M:25k", 14),
             ("quiet 240-250", "240M:250M:100k", 12)]
    res = []
    for name, b, secs in bands:
        pts = sweep(phys, b, secs, gain)
        if not pts:
            log("  %-22s NO DATA" % name)
            continue
        dbs = [d for _, d in pts]
        fl, pk = median(dbs), max(dbs)
        res.append((name, fl, pk, pk - fl))
        log("  %-22s floor %7.2f dB   peak %7.2f dB   peak-floor %5.1f dB"
            % (name, fl, pk, pk - fl))
    cleanup()
    if len(res) >= 3:
        fm = next((r for r in res if r[0].startswith("FM")), None)
        sat = next((r for r in res if r[0].startswith("SAT")), None)
        quiet = next((r for r in res if r[0].startswith("quiet")), None)
        print()
        if fm and sat and quiet:
            lift = sat[1] - quiet[1]
            log("137 MHz floor sits %.1f dB above the 240-250 MHz reference."
                % lift)
            if lift < 1.5:
                log("  READS AS NOT COUPLING: the satellite band shows no more "
                    "external noise than a band we expect to be dead. An "
                    "antenna that hears the sky lifts its own noise floor.")
            else:
                log("  The antenna IS picking up external noise at 137 MHz, so "
                    "it is connected and coupling. A failed decode is then "
                    "about signal strength or pointing, not a dead feedline.")
    return 0


def band_power(pts, lo_hz, hi_hz):
    """Mean power across a block of spectrum, in dB."""
    vals = [d for f, d in pts if lo_hz <= f <= hi_hz]
    return (sum(vals) / len(vals)) if vals else None


def pass_test(start_iso, minutes=18, gain=48, span_khz=400):
    """Watch for a WIDEBAND rise around 137.9 -- LRPT has no carrier.

    LRPT is 72 kbaud QPSK, a suppressed-carrier modulation about 150 kHz wide.
    There is no spectral line to find: a perfect signal appears as a raised
    block of noise floor, not a peak. Searching for a peak is the right test
    for analog APT and the wrong test here, and running it produced a
    confident "no carrier" verdict about a satellite other people receive.

    So: measure total power in the 150 kHz the signal occupies, and compare it
    against the SAME measurement in guard bands either side. A wideband
    transmission raises the middle relative to the edges; noise raises both
    equally. That ratio is the signature, and unlike a peak it cannot be faked
    by a static receiver artefact -- which is what the constant 137.8728 MHz
    "peak" turned out to be.
    """
    phys = low_band_phys()
    start = datetime.fromisoformat(start_iso)
    wait = (start - datetime.now(timezone.utc)).total_seconds() - 60
    if wait > 0:
        log("waiting %.0f s for the pass" % wait)
        time.sleep(wait)
    if not grab(phys):
        log("WARNING: broker never confirmed release")
    lo = LRPT_MHZ - span_khz / 2000.0
    hi = LRPT_MHZ + span_khz / 2000.0
    band = "%.4fM:%.4fM:5k" % (lo, hi)
    # the signal occupies ~150 kHz centred on 137.9; the guard blocks sit
    # outside it and carry only noise
    sig_lo, sig_hi = (LRPT_MHZ - 0.080) * 1e6, (LRPT_MHZ + 0.080) * 1e6
    g1_lo, g1_hi = lo * 1e6, (LRPT_MHZ - 0.120) * 1e6
    g2_lo, g2_hi = (LRPT_MHZ + 0.120) * 1e6, hi * 1e6
    log("watching %s for %d min on radio %s" % (band, minutes, phys))
    log("  signal block 137.820-137.980 MHz vs guard blocks either side")
    track = []
    end = time.time() + minutes * 60
    while time.time() < end:
        pts = sweep(phys, band, 20, gain)
        if not pts:
            continue
        sig = band_power(pts, sig_lo, sig_hi)
        g = [x for x in (band_power(pts, g1_lo, g1_hi),
                         band_power(pts, g2_lo, g2_hi)) if x is not None]
        if sig is None or not g:
            continue
        guard = sum(g) / len(g)
        excess = sig - guard
        track.append((datetime.now(timezone.utc), excess))
        log("  signal block %+.2f dB vs guard bands%s"
            % (excess, "   <-- WIDEBAND SIGNAL" if excess > 2.0 else ""))
    cleanup()
    print()
    if not track:
        log("VERDICT: no usable sweeps taken.")
        return 1
    peak = max(t[1] for t in track)
    hits = [t for t in track if t[1] > 2.0]
    log("peak excess over guard bands: %+.2f dB across %d sweeps" % (peak, len(track)))
    if hits:
        log("VERDICT: a wideband signal WAS present (%d sweeps above +2 dB, "
            "peak %+.2f dB). The satellite is transmitting and the failure is "
            "at our end -- antenna gain, geometry or pointing." % (len(hits), peak))
    else:
        log("VERDICT: the 150 kHz LRPT block never rose more than %+.2f dB "
            "above the guard bands either side of it. No wideband "
            "transmission this antenna could hear." % peak)
        log("  NOTE: this bounds what the ANTENNA received. It cannot "
            "distinguish a silent satellite from one too weak for an indoor "
            "antenna -- only an outdoor antenna, or a report from another "
            "receiver on the same pass, separates those two.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--floor", action="store_true")
    ap.add_argument("--pass", dest="pass_at")
    ap.add_argument("--gain", type=int, default=48)
    ap.add_argument("--minutes", type=int, default=18)
    a = ap.parse_args()
    import signal as _s
    _s.signal(_s.SIGINT, lambda *x: (cleanup(), sys.exit(1)))
    _s.signal(_s.SIGTERM, lambda *x: (cleanup(), sys.exit(1)))
    try:
        if a.pass_at:
            return pass_test(a.pass_at, a.minutes, a.gain)
        return floor_test(a.gain)
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
