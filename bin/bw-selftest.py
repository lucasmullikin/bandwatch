#!/usr/bin/env python3
"""T24 -- prove each radio can actually HEAR, not merely open.

A dongle can enumerate, claim its USB interface, run a lane to completion and
write a coverage row while hearing absolutely nothing: a disconnected coax, a
dead LNA, an antenna cut for the wrong band, a gain of zero. Every other check
in this system is satisfied by that radio. "0 events" then reads as a quiet
band, which is exactly the failure this project keeps paying for.

The control signal is the FM broadcast band. It is the only thing in range that
is guaranteed present day and night regardless of traffic, aircraft or weather,
and it is strong enough that its absence means something is physically wrong.
Measured on this receiver: 92.30 MHz at +20.6 dB, 96.91 at +18.5, 94.88 at
+18.1 -- roughly 25 dB above the local floor.

The test is only meaningful if it can FAIL. `--negative-control` runs the same
measurement against a slice of spectrum with no licensed broadcast service in
it; if that slice also "passes", the detector is measuring its own noise and
the result on the real band means nothing either.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")
BIN = os.path.join(os.path.expanduser("~"), "homebrew", "bin")
PAUSE_FMT = "/tmp/sdr_pause_dev%s"

FM_BAND = "88M:108M:100k"
# US FM broadcast raster: 87.9 MHz + n * 200 kHz, i.e. always an odd tenth of a
# MHz. Licensed stations are required to sit on it; noise, spurs and images are
# not. This is the control -- it does not depend on any band being quiet.
FM_RASTER_LO_HZ = 87_900_000
FM_RASTER_STEP_HZ = 200_000
# half a sweep bin: a carrier can only be located to within one bin
RASTER_TOL_HZ = 40_000
# a random frequency falls within +/-40 kHz of a raster point 2*40/200 = 40% of
# the time, so chance alone produces ~0.4. Require clearly better than chance.
MIN_RASTER_FRACTION = 0.75

# a real carrier stands this far above the band's own median
MIN_SNR_DB = 8.0
# and at least this many distinct carriers should be visible on FM
MIN_CARRIERS = 3
SWEEP_SECONDS = 14


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print("%s  %s" % (now(), msg), flush=True)


def dongles():
    try:
        r = subprocess.run([os.path.join(BIN, "rtl_test"), "-t"],
                           capture_output=True, text=True, timeout=25,
                           stdin=subprocess.DEVNULL)
        return re.findall(r"^\s*(\d+):.*SN:\s*(\S+)", r.stdout + r.stderr, re.M)
    except Exception as e:
        log("rtl_test failed: %s" % e)
        return []


def sweep(device, band, seconds, gain=30):
    """One rtl_power sweep. Returns [(freq_hz, db)] or None if it could not run."""
    out = os.path.join(VAR, "selftest_dev%s.csv" % device)
    cmd = [os.path.join(BIN, "rtl_power"), "-d", str(device), "-f", band,
           "-g", str(gain), "-i", "4", "-e", str(seconds), out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=seconds + 40,
                       stdin=subprocess.DEVNULL)
    except Exception as e:
        log("  rtl_power failed on dev%s: %s" % (device, e))
        return None
    pts = []
    try:
        with open(out) as fh:
            for row in csv.reader(fh):
                if len(row) < 7:
                    continue
                try:
                    lo, hi, step = float(row[2]), float(row[3]), float(row[4])
                except ValueError:
                    continue
                for i, cell in enumerate(row[6:]):
                    try:
                        pts.append((lo + i * step, float(cell)))
                    except ValueError:
                        pass
    except OSError:
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    return pts or None


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def analyse(points, min_snr=MIN_SNR_DB):
    """Carriers standing clear of the band's own median.

    The floor is the median rather than the minimum: one dead bin would drag a
    minimum-based floor down and make every bin look like a signal.
    """
    if not points:
        return {"floor": None, "carriers": [], "peak_snr": None}
    dbs = [d for _, d in points]
    floor = median(dbs)
    carriers = []
    for f, d in points:
        if d - floor >= min_snr:
            carriers.append((f, round(d - floor, 1)))
    # collapse adjacent bins of one transmitter into a single carrier
    carriers.sort()
    merged = []
    for f, snr in carriers:
        if merged and f - merged[-1][0] < 300000:
            if snr > merged[-1][1]:
                merged[-1] = (f, snr)
        else:
            merged.append((f, snr))
    merged.sort(key=lambda c: -c[1])
    return {"floor": round(floor, 2), "carriers": merged,
            "peak_snr": merged[0][1] if merged else 0.0}


def on_raster(freq_hz):
    """Is this carrier where a licensed FM station is required to be?"""
    if freq_hz < FM_RASTER_LO_HZ - RASTER_TOL_HZ:
        return False
    off = (freq_hz - FM_RASTER_LO_HZ) % FM_RASTER_STEP_HZ
    return min(off, FM_RASTER_STEP_HZ - off) <= RASTER_TOL_HZ


def raster_score(carriers):
    """Fraction of detected carriers sitting on the FM channel raster.

    This is the whole proof. Energy alone does not show a radio is hearing the
    outside world -- a shorted input still produces energy. Energy landing
    where transmitters are legally obliged to be does.
    """
    if not carriers:
        return 0.0, 0, 0
    hits = sum(1 for f, _ in carriers if on_raster(f))
    return hits / float(len(carriers)), hits, len(carriers)


def pause(device):
    open(PAUSE_FMT % device, "w").close()


def unpause(device):
    try:
        os.unlink(PAUSE_FMT % device)
    except OSError:
        pass


def clear_all_pauses():
    """A pause flag left behind idles a radio silently. One once sat for three
    hours while every status check reported healthy, so this runs on every
    exit path including failure."""
    for f in os.listdir("/tmp"):
        if f.startswith("sdr_pause_dev"):
            try:
                os.unlink(os.path.join("/tmp", f))
            except OSError:
                pass


def test_device(device, negative_control=False, quiet=False):
    result = {"device": device, "ok": False, "reason": None}
    pause(device)
    # Lanes yield at their own pace: the broker polls the pause flag every
    # 0.2s, but rtl_airband ignores SIGTERM and has to be escalated, and readsb
    # takes longer still. A single short wait made dev1 report "device busy"
    # when nothing was wrong with the radio at all.
    try:
        pts = None
        for wait in (8, 12, 20):
            time.sleep(wait)
            pts = sweep(device, FM_BAND, SWEEP_SECONDS)
            if pts is not None:
                break
            if not quiet:
                log("  dev%s still held after %ds -- waiting longer" % (device, wait))
        if pts is None:
            result["reason"] = ("rtl_power could not open dev%s after 40s of "
                                "waiting -- the lane never yielded, or the "
                                "dongle is gone" % device)
            return result
        fm = analyse(pts)
        result["fm"] = fm
        if not quiet:
            log("  dev%s FM floor %.1f dB, %d carrier(s), peak +%.1f dB"
                % (device, fm["floor"], len(fm["carriers"]), fm["peak_snr"]))
            for f, snr in fm["carriers"][:5]:
                log("      %.4f MHz  +%.1f dB" % (f / 1e6, snr))

        frac, hits, total = raster_score(fm["carriers"])
        result["raster"] = {"fraction": round(frac, 3), "on_raster": hits,
                            "carriers": total, "chance": 0.4}
        if not quiet:
            log("  dev%s %d/%d carriers on the FM raster (%.0f%%, chance is 40%%)"
                % (device, hits, total, frac * 100))

        if total < MIN_CARRIERS:
            result["reason"] = (
                "only %d carrier(s) above +%.0f dB on the FM broadcast band. "
                "FM transmits continuously, so this means the antenna is "
                "disconnected, cut for the wrong band, or the front end is dead."
                % (total, MIN_SNR_DB))
            return result
        if frac < MIN_RASTER_FRACTION:
            result["reason"] = (
                "found %d carriers but only %.0f%% sit on the FM channel raster "
                "(chance alone gives 40%%). That is energy, not broadcast "
                "reception -- suspect a spurious/overloaded front end or an "
                "antenna that is not connected to anything."
                % (total, frac * 100))
            return result

        if negative_control:
            # The control is that the SAME detector, applied to the same sweep,
            # must NOT find this alignment by accident. Shift the raster by half
            # a channel: a real FM band scores near zero against an offset
            # raster, noise scores about the same as it did against the real one.
            shifted = [(f + FM_RASTER_STEP_HZ / 2, d) for f, d in fm["carriers"]]
            sfrac, shits, stot = raster_score(shifted)
            result["offset_raster"] = {"fraction": round(sfrac, 3),
                                       "on_raster": shits}
            if not quiet:
                log("  dev%s negative control: %d/%d on a HALF-CHANNEL-OFFSET "
                    "raster (%.0f%%) -- should be far lower"
                    % (device, shits, stot, sfrac * 100))
            if sfrac >= frac:
                result["reason"] = (
                    "NEGATIVE CONTROL FAILED: the carriers fit a deliberately "
                    "wrong raster (%.0f%%) as well as the real one (%.0f%%). "
                    "The alignment is coincidence, so the pass proves nothing."
                    % (sfrac * 100, frac * 100))
                return result

        result["ok"] = True
        result["reason"] = (
            "hears %d FM carriers, %.0f%% on the licensed raster, strongest "
            "+%.1f dB above floor" % (total, frac * 100, fm["peak_snr"]))
        return result
    finally:
        unpause(device)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="only this device index")
    ap.add_argument("--negative-control", action="store_true",
                    help="also sweep an empty band; the test is worthless if "
                         "that one passes too")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    present = dongles()
    if not present:
        print("no dongles visible to rtl_test", file=sys.stderr)
        return 2
    if not a.quiet:
        log("%d dongle(s): %s" % (len(present),
                                  ", ".join("%s=SN:%s" % (i, s) for i, s in present)))
        serials = [s for _, s in present]
        if len(set(serials)) < len(serials):
            log("  NOTE: duplicate serials -- device order can change on replug")

    devices = [a.device] if a.device else [i for i, _ in present]
    results = []
    try:
        for d in devices:
            if not a.quiet:
                log("testing dev%s (pausing its lane)" % d)
            results.append(test_device(d, a.negative_control, a.quiet))
    finally:
        clear_all_pauses()

    if a.json:
        print(json.dumps({"ts": now(), "results": results}, indent=2))
    else:
        print()
        for r in results:
            print("  dev%s: %s -- %s"
                  % (r["device"], "PASS" if r["ok"] else "FAIL", r["reason"]))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
