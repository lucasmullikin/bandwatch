#!/usr/bin/env python3
"""Tuner broker -- owns every SDR device and grants each to one lane at a time.

An RTL-SDR is an exclusive resource: two processes cannot open the same dongle.
Each device gets its own thread, its own flock, and its own independent lane
rotation, so a slow lane on one radio never stalls the other.

Voice lanes get long dwells because a transmission is short, unpredictable and
never repeats -- unlike sensors (repeat every 30-60s) and ADS-B (several times
per second), which tolerate slicing.
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import sqlite3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
LOGS = os.path.join(VAR, "logs")
STATE = os.path.join(VAR, "state.json")
LOCK_FMT = "/tmp/sdr_tuner_%s.lock"
# The web UI creates this to borrow a radio for live listening. The broker
# releases the device and idles rather than fighting for it -- two processes
# cannot open the same dongle, and the loser gets a dead handle, not an error.
sys.path.insert(0, ROOT)
from bandwatch_config import PAUSE_FMT  # noqa: E402  one definition, see there
OPEN_RETRY_S = 6        # a lane dying faster than this never opened the radio
OPEN_RETRY_WAIT_S = 4   # give the previous holder time to release USB

DEVICE_HOLDERS = ("rtl_433", "rtl_power", "rtl_fm", "rtl_tcp", "rtl_airband",
                  "dump1090", "readsb", "acarsdec", "direwolf")

_stop = threading.Event()
_state_lock = threading.Lock()
_state = {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    line = "%s  %s" % (now(), msg)
    print(line, flush=True)
    try:
        with open(os.path.join(LOGS, "broker.log"), "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def on_signal(signum, frame):
    _stop.set()
    log("signal %d received -- stopping after current lanes" % signum)


def write_state():
    with _state_lock:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_state, fh, indent=2)
        os.replace(tmp, STATE)


def record_coverage(dev, lane_id, started, ended, events):
    """T13: which lane was on which radio, and when.

    The coverage standard leans on this: "was the receiver listening to 121.5
    at 3am?" must be answerable from a query, not from a rotating text log.
    Best-effort -- a locked database must never stall a lane.
    """
    db = os.path.join(VAR, "events.db")
    if not os.path.exists(db):
        return
    try:
        con = sqlite3.connect(db, timeout=5)
        con.execute("INSERT INTO coverage(device,lane,started_at,ended_at,events)"
                    " VALUES (?,?,?,?,?)",
                    (str(dev), lane_id, started, ended, int(events)))
        con.commit()
        con.close()
    except Exception as e:
        # best-effort, but SAY SO. A bare except here hid the reason coverage
        # rows were never appearing.
        log("coverage write failed for dev%s/%s: %s" % (dev, lane_id, e))


def count_events(lane):
    """Progress counter for a lane.

    Default is line count, which assumes the producer APPENDS. rtl_power
    TRUNCATES its output file every run, so a line-count delta reads zero and
    a perfectly working survey lane would flag itself FAILED. Those lanes
    declare count_mode="mtime": the file's modification time is the counter,
    so any rewrite registers as progress.

    BUT AN EMPTY FILE IS NOT PROGRESS. Measured 2026-09-20: one radio had been
    unusable for two days -- its USB interface stuck claimed, every open
    failing -- and rtl_power still created and truncated its CSV on every run.
    The mtime advanced, the size stayed at 0, and all four sweep lanes on that
    radio reported themselves healthy the entire time while the event store
    recorded nothing. A touched file and a written file were the same signal.

    So mtime mode now requires BOTH: the file was rewritten AND it has bytes
    in it. An empty sink reads as 0, which makes the delta against any earlier
    non-empty reading negative, and the caller floors that at no progress.
    """
    sink = lane.get("event_file")
    if not sink or not os.path.exists(sink):
        return 0
    try:
        if lane.get("count_mode") == "mtime":
            st = os.stat(sink)
            if st.st_size <= 0:
                return 0                    # touched, but nothing was written
            return int(st.st_mtime)
        with open(sink, "rb") as fh:
            return sum(1 for _ in fh)
    except Exception:
        return 0


def kill_group(proc, tag, term_wait=10):
    """Kill the lane's whole process group.

    Lanes run with start_new_session=True, so they lead their own group.
    Killing only the leader can orphan children that keep the dongle open --
    which makes the NEXT lane deaf while status still reads green.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            log("%s ignored %s, escalating" % (tag, sig.name))


def enumerate_dongles():
    """Map serial -> current index by asking librtlsdr, via rtl_test.

    USB enumeration order is NOT stable across a replug or reboot. If the two
    dongles swap indices, every lane silently uses the WRONG ANTENNA -- radio 0
    carries the low-band whip and radio 1 the high-band one -- and the symptom
    looks like bad reception rather than a swapped index. Resolving by serial
    removes the failure mode entirely.
    """
    pairs = []
    try:
        r = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True,
                           timeout=20, stdin=subprocess.DEVNULL)
        for line in (r.stdout + r.stderr).splitlines():
            m = re.match(r"\s*(\d+):.*SN:\s*(\S+)", line)
            if m:
                pairs.append((m.group(2), m.group(1)))
    except Exception:
        pass
    # return the RAW pairs: collapsing to a dict here would hide duplicate
    # serials, which is precisely the condition the caller must detect
    return pairs


def resolve_device(dev_key, spec):
    """Return the index to hand the lane for this device slot.

    Falls back to the configured slot key when the serial is missing or
    ambiguous (both dongles ship as 00000001 until one is re-serialised), and
    says so, rather than silently picking the wrong radio.
    """
    want = spec.get("serial")
    if not want:
        return dev_key, None
    pairs = enumerate_dongles()
    matches = [idx for sn, idx in pairs if sn == want]
    if len(matches) > 1:
        return dev_key, ("serial %s matches %d dongles (re-serialise one)"
                         % (want, len(matches)))
    if not matches:
        return dev_key, "serial %s not present" % want
    return matches[0], None


def device_busy(dev):
    """Is anything still holding THIS device? Matches '-d <dev>'/'--device=<dev>'."""
    try:
        # macOS pgrep has NO -a (that is Linux); -l is the long form here.
        # With -af the output is PIDs only, every match test fails, and the
        # check silently reports "not busy" forever.
        out = subprocess.run(["pgrep", "-fl", "|".join(DEVICE_HOLDERS)],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    if out.returncode != 0:
        return False
    needles = ("-d %s" % dev, "--device=%s" % dev, "-d%s" % dev)
    return any(any(n in line for n in needles) for line in out.stdout.splitlines())


def run_lane(dev, lane, dstate, slot=None):
    """Run one lane.

    `dev` is the PHYSICAL librtlsdr index -- the number the decoder must be
    given to open the right hardware. `slot` is the LOGICAL device from the
    profile (dev0 = low band, dev1 = high band).

    They are usually the same and were assumed to be for a long time. They stop
    being the same the moment the dongles are replugged into different ports:
    librtlsdr's ordering is not stable, which is exactly why lanes are pinned
    by serial. Measured 2026-09-20 after a replug -- physical 0 became the
    high-band radio and physical 1 the low-band one.

    Labels must follow the LOGICAL slot, commands must follow the PHYSICAL
    index. Using the physical index for both meant every log line and every log
    FILENAME named the wrong radio after a replug, which points anyone
    diagnosing a fault at the wrong dongle. That cost real time here.
    """
    lane_id = lane["id"]
    if slot is None:
        slot = dev
    tag = "dev%s/%s" % (slot, lane_id)
    seconds = int(lane.get("seconds", 300))
    cmd = [c.replace("{DEV}", str(dev)) for c in lane["cmd"]]
    logfile = os.path.join(LOGS, "lane-%s-%s.log" % (slot, lane_id))

    before = count_events(lane)
    started = time.time()
    log("%s -> start (%ds)" % (tag, seconds))
    dstate["_lane_started"] = now()
    dstate["current_lane"] = {"id": lane_id, "started_at": now(),
                              "dwell_s": seconds, "note": lane.get("note", "")}
    write_state()

    rc = None
    yielding = False
    with open(logfile, "a") as lf:
        lf.write("\n--- %s start ---\n" % now())
        lf.flush()
        # The first lane after a broker restart reliably lost the race against
        # the previous holder releasing the dongle: rtl_airband died in ~1s with
        # "usb_claim_interface error -3 ... claimed by second instance". The
        # slot was then burnt and the lane skipped its whole turn. Retry a fast
        # non-zero exit once -- a lane that cannot open its radio is worth one
        # more try, and a lane that is genuinely broken still fails twice.
        proc = None
        for attempt in (1, 2):
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
            if attempt == 2:
                break
            quick = time.time() + OPEN_RETRY_S
            while time.time() < quick:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            if proc.poll() is None or proc.returncode == 0:
                break
            log("%s could not open the radio (rc=%s in <%ds) -- retrying once"
                % (tag, proc.returncode, OPEN_RETRY_S))
            lf.write("--- retry after failed open ---\n")
            lf.flush()
            time.sleep(OPEN_RETRY_WAIT_S)
            started = time.time()          # do not charge the retry to the lane
        deadline = started + seconds
        while time.time() < deadline and not _stop.is_set():
            # a pause must interrupt the lane, not wait for it. Lanes run
            # 240-720s; checking only between them made "tune in" take minutes.
            if os.path.exists(PAUSE_FMT % dev):
                log("%s -> yielding, pause requested" % tag)
                yielding = True
                break
            rc = proc.poll()
            if rc is not None:
                log("%s EXITED EARLY rc=%s after %.0fs" % (tag, rc, time.time() - started))
                break
            # 0.2s, not 1s: this interval is dead time between a tune request
            # and audio, and you lose the start of whatever you tuned for.
            time.sleep(0.2)
        if proc.poll() is None:
            # a lane being displaced by a live tune gets a short grace period;
            # the normal 10s SIGTERM wait is pure latency for the listener
            # rtl_airband ignores SIGTERM (observed: "ignored SIGTERM,
            # escalating" every yield), so waiting on it is pure dead air for
            # someone who just clicked a station. Displaced lanes have nothing
            # to flush -- recordings are written per transmission -- so give a
            # token 0.3s then SIGKILL.
            kill_group(proc, tag, term_wait=0.3 if yielding else 10)

    # prove the device came back before handing it to the next lane
    for _ in range(30 if not yielding else 10):
        if not device_busy(dev):
            break
        time.sleep(0.5 if yielding else 1)
    else:
        log("%s: DEVICE STILL HELD after teardown -- next lane would be deaf" % tag)

    elapsed = time.time() - started
    gained = count_events(lane) - before
    # count_mode=mtime returns a unix timestamp, so the delta is SECONDS, not
    # events. Reporting it as an event count showed "3117 events" for a lane
    # that produced one file. Collapse it to a 1/0 freshness signal.
    if lane.get("count_mode") == "mtime":
        gained = 1 if gained > 0 else 0

    ls = dstate["lanes"].setdefault(lane_id, {
        "runs": 0, "events_total": 0, "last_event_at": None,
        "early_exits": 0, "last_run_at": None, "last_run_events": 0})
    ls["runs"] += 1
    ls["events_total"] += gained
    ls["last_run_at"] = now()
    ls["last_run_events"] = gained
    ls["last_run_seconds"] = round(elapsed, 1)
    if gained > 0:
        ls["last_event_at"] = now()
    if rc is not None and not _stop.is_set() and not lane.get("self_terminating"):
        ls["early_exits"] += 1

    record_coverage(dev, lane_id, dstate.get("_lane_started") or now(), now(), gained)
    dstate["current_lane"] = None
    log("%s -> done %.0fs +%d events (total %d)" % (tag, elapsed, gained, ls["events_total"]))
    write_state()


def lane_index(lanes, lane_id):
    """Where to resume rotation.

    Keyed on lane ID, not position: editing a profile reorders the list, and an
    index saved against the old order would silently resume on a different
    lane. An unknown ID means the profile changed -- start from the top rather
    than guess.
    """
    if not lane_id:
        return 0
    for i, l in enumerate(lanes):
        if l.get("id") == lane_id:
            return i
    return 0


def device_thread(dev, spec):
    """One independent rotation per radio, each holding its own lock."""
    lanes = [l for l in spec["lanes"] if l.get("enabled", True)]
    dstate = _state["devices"][str(dev)]
    phys, why = resolve_device(dev, spec)
    if why:
        log("dev%s: %s -- falling back to index %s" % (dev, why, dev))
    elif phys != dev:
        log("dev%s resolved to physical index %s by serial %s"
            % (dev, phys, spec.get("serial")))
    dstate["physical_index"] = phys
    if not lanes:
        log("dev%s has no enabled lanes" % dev)
        return

    lockfh = open(LOCK_FMT % phys, "w")
    try:
        fcntl.flock(lockfh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("dev%s: another broker holds the lock -- thread exiting" % dev)
        return
    lockfh.write("%d\n" % os.getpid())
    lockfh.flush()

    log("dev%s (%s) up: %s" % (dev, spec.get("label", "?"),
                               ",".join(l["id"] for l in lanes)))
    try:
        while not _stop.is_set():
            if os.path.exists(PAUSE_FMT % phys) or os.path.exists(PAUSE_FMT % dev):
                if not dstate.get("paused"):
                    log("dev%s PAUSED -- device handed to the live tuner" % dev)
                    dstate["paused"] = True
                    dstate["current_lane"] = None
                    write_state()
                time.sleep(2)
                continue
            if dstate.get("paused"):
                log("dev%s resumed" % dev)
                dstate["paused"] = False
                write_state()
            # Resume where the last run left off. Starting at index 0 every
            # time meant a device whose cycle is longer than the interval
            # between restarts could never reach its later lanes at all.
            start = lane_index(lanes, dstate.get("next_lane_id"))
            for step in range(len(lanes)):
                if _stop.is_set() or os.path.exists(PAUSE_FMT % dev):
                    break
                idx = (start + step) % len(lanes)
                lane = lanes[idx]
                # record the NEXT lane before running this one, so a crash
                # mid-lane advances rather than repeating the same lane forever
                dstate["next_lane_id"] = lanes[(idx + 1) % len(lanes)]["id"]
                write_state()
                run_lane(phys, lane, dstate, slot=dev)
            dstate["cycles"] += 1
            write_state()
    finally:
        fcntl.flock(lockfh, fcntl.LOCK_UN)
        lockfh.close()
        log("dev%s down after %d cycles" % (dev, dstate["cycles"]))


def main():
    if len(sys.argv) < 2:
        print("usage: broker.py <profile>", file=sys.stderr)
        return 2
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(os.path.join(VAR, "events"), exist_ok=True)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    profile_name = sys.argv[1]
    with open(os.path.join(
            os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
            "profiles", profile_name + ".json")) as fh:
        profile = json.load(fh)

    # Carry the rotation cursor across restarts. Without this the persisted
    # next_lane_id was written every lane and then thrown away on the next
    # start, so rotation always resumed at lane 1 and the later lanes on a
    # long cycle were never reached.
    prior = {}
    try:
        with open(STATE) as fh:
            prior = (json.load(fh).get("devices") or {})
    except Exception:
        prior = {}

    _state.update({"profile": profile_name, "pid": os.getpid(),
                   "started_at": now(), "devices": {}})
    for dev in profile["devices"]:
        carried = (prior.get(str(dev)) or {}).get("next_lane_id")
        _state["devices"][str(dev)] = {
            "label": profile["devices"][dev].get("label", ""),
            "cycles": 0, "current_lane": None, "lanes": {}, "paused": False,
            "next_lane_id": carried}
        if carried:
            log("dev%s resuming rotation at %s" % (dev, carried))
    write_state()

    threads = []
    for dev, spec in profile["devices"].items():
        t = threading.Thread(target=device_thread, args=(dev, spec),
                             name="dev%s" % dev, daemon=True)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        _stop.set()
    for t in threads:
        t.join(timeout=60)

    _state["pid"] = None
    _state["stopped_at"] = now()
    write_state()
    log("broker down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
