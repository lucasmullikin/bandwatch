#!/usr/bin/env python3
"""T23 -- watch what the radios PRODUCE, not whether processes exist.

The supervisor already restarts anything that dies. That is not enough: on
2026-08-31 a stale /tmp/sdr_pause_dev1 left device 1 IDLE FOR THREE HOURS while
every process was alive and `bandwatch status` reported healthy. Liveness is not
health. A receiver can be running, holding its dongle, and hearing nothing.

Failure modes this actually checks:
  1. a dongle missing from rtl_test entirely (unplugged, USB fault)
  2. a pause flag set with NO live tuner behind it -- the exact 3-hour outage
  3. a device that has completed no lane in the window (wedged rotation)
  4. a lane exiting early on every run (broken command, device contention)

Policy, set by the operator: AUTO-REPAIR first, and Signal only if the SAME
device faults again inside the escalation window -- so transients self-heal
quietly and a fault that does not hold still reaches a human.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.environ.get("BANDWATCH_VAR") or os.path.join(ROOT, "var")
DB = os.path.join(VAR, "events.db")
STATE = os.path.join(VAR, "watchdog.json")
LOG = os.path.join(VAR, "logs", "watchdog.log")
CONFIG = os.path.join(
    os.environ.get("BANDWATCH_CONFIG") or os.path.join(ROOT, "config"),
    "bandwatch.json")
PAUSE_PREFIX = "sdr_pause_dev"
HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, "homebrew", "bin")

SILENT_MIN = 25            # no completed lane in this long == wedged
STARVE_CYCLES = 3          # a lane unrun for this many full cycles is starved
ESCALATE_WINDOW_MIN = 60   # a second fault inside this window pages a human
TUNER_PROCS = ("rtl_fm", "ffmpeg .*pipe:0")

# --- a repair that does not repair -------------------------------------------
# The escalation above only sees a REPEAT inside a 60-minute window. dev1's adsb
# and ism433 lanes fault every ~72 MINUTES -- just outside it -- so the watchdog
# bounced the WHOLE STACK twelve times a day, every day, and paged twice in
# eleven days. Every one of those restarts took both radios and every other lane
# down to "fix" one lane that dies again 24 seconds later.
#
# Detecting a fault forever is not fixing it. After LANE_REPAIR_LIMIT
# CONSECUTIVE failed repairs a lane is declared not self-healing: paged once,
# then MUTED -- it stops driving stack restarts.
#
# Muted, NOT disabled. The lane keeps its turn in the rotation, because a lane
# that fails most runs is not a lane that fails every run: adsb wedges on ~70%
# of starts and still delivered 2500 events in the hour it survived. Disabling
# it would throw that away to quiet an alarm. The restart is the harm here, not
# the lane. A fault in a lane that produces NOTHING is a separate judgement and
# is left to a human.
#
# A pass in which the lane does not fault clears its streak, so only consecutive
# failures count, and a lane that comes back unmutes itself.
LANE_REPAIR_LIMIT = 3
LANE_FAULT_RE = re.compile(r"^lane (\S+) exits early on every run$")
MUTE_LOG = os.path.join(VAR, "logs", "muted-lanes.log")


def now():
    return datetime.now(timezone.utc)


def log(msg):
    line = "%s  %s" % (now().isoformat(timespec="seconds"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def cfg():
    try:
        return json.load(open(CONFIG))
    except Exception:
        return {}


def load_state():
    try:
        s = json.load(open(STATE))
        s.setdefault("repairs", [])
        s.setdefault("lane_faults", {})
        return s
    except Exception:
        return {"repairs": [], "lane_faults": {}}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def dongles_present():
    try:
        r = subprocess.run([os.path.join(BIN, "rtl_test"), "-t"],
                           capture_output=True, text=True, timeout=25,
                           stdin=subprocess.DEVNULL)
        return re.findall(r"^\s*(\d+):.*SN:\s*(\S+)", r.stdout + r.stderr, re.M)
    except Exception:
        return []


def proc_running(pattern):
    try:
        return subprocess.run(["pgrep", "-f", pattern],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def all_pause_flags():
    try:
        return [p for p in os.listdir("/tmp") if p.startswith(PAUSE_PREFIX)]
    except Exception:
        return []


def flag_owner_alive(name):
    """Is the process that set this flag still running?

    The flag carries its owner's PID. A live owner means the radio is being
    held on purpose -- a satellite pass, a live tune -- and is not a fault.
    """
    try:
        with open(os.path.join("/tmp", name)) as fh:
            pid = int((fh.read() or "0").strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def held_devices():
    """Physical/device ids currently held by a LIVE process."""
    out = set()
    for f in all_pause_flags():
        if flag_owner_alive(f):
            out.add(f[len(PAUSE_PREFIX):])
    return out


def stale_pause_flags():
    """Only flags whose owner is gone. A live hold is deliberate."""
    return [f for f in all_pause_flags() if not flag_owner_alive(f)]


def broker_state():
    try:
        return json.load(open(os.path.join(VAR, "state.json")))
    except Exception:
        return {}


def recent_coverage(dev, minutes):
    """Coverage rows are written only when a lane RAN TO COMPLETION, which
    makes them the honest signal that a radio is actually working."""
    if not os.path.exists(DB):
        return None
    cut = (now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    try:
        con = sqlite3.connect(DB, timeout=5)
        n = con.execute(
            "SELECT COUNT(*) FROM coverage WHERE device=? AND ended_at>=?",
            (str(dev), cut)).fetchone()[0]
        con.close()
        return n
    except Exception:
        return None


def profile_cycles():
    """Seconds per full rotation for each device, from the live profile."""
    st = broker_state()
    name = st.get("profile")
    path = os.path.join(ROOT, "profiles", "%s.json" % name) if name else None
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        prof = json.load(open(path))
    except Exception:
        return out
    for dev, spec in (prof.get("devices") or {}).items():
        lanes = [l for l in spec.get("lanes", []) if l.get("enabled", True)]
        out[str(dev)] = (sum(int(l.get("seconds") or 0) for l in lanes),
                         [l["id"] for l in lanes])
    return out


def starved_lanes():
    """Lanes that have not RUN recently enough, as opposed to lanes that ran
    and heard nothing. The two look identical downstream and are completely
    different faults: one is a quiet band, the other is a radio that was never
    pointed at it."""
    st = broker_state()
    cycles = profile_cycles()
    faults = []
    now_ts = now()
    for dev, (cycle_s, lane_ids) in cycles.items():
        if not cycle_s or not lane_ids:
            continue
        limit = timedelta(seconds=cycle_s * STARVE_CYCLES)
        # a device that has not completed one full cycle since boot cannot be
        # judged yet -- do not report a lane as starved before its first turn
        # was even due
        lanes = ((st.get("devices") or {}).get(dev) or {}).get("lanes") or {}
        # started_at is TOP-LEVEL in state.json, not per-device. Reading it
        # from the device dict returned None, which disabled this guard
        # entirely and would have flagged every lane as "never run" the
        # instant the broker started.
        s0 = parse_iso(st.get("started_at"))
        if s0 and (now_ts - s0) < limit:
            continue
        for lid in lane_ids:
            last = parse_iso((lanes.get(lid) or {}).get("last_run_at"))
            if last is None:
                faults.append((dev, "lane %s has NEVER run (cycle is %dm)"
                               % (lid, cycle_s // 60)))
            elif now_ts - last > limit:
                mins = int((now_ts - last).total_seconds() // 60)
                faults.append((dev, "lane %s last ran %dm ago, cycle is only %dm"
                               % (lid, mins, cycle_s // 60)))
    return faults


def parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def check():
    """Return [(device, reason)] for every fault found."""
    faults = []
    st = broker_state()
    slots = sorted((st.get("devices") or {}).keys())
    if not slots:
        return [("-", "no broker state; the broker has never run")]

    present = dongles_present()
    if len(present) < len(slots):
        faults.append(("-", "only %d dongle(s) visible, profile expects %d"
                       % (len(present), len(slots))))

    # A radio deliberately held by a live process is BUSY, not broken. Judging
    # it by "did a lane complete" would fault every satellite pass and every
    # long listen, then restart the stack in the middle of the measurement.
    held = held_devices()
    if held:
        log("radios held by a live process (not faults): %s" % ", ".join(sorted(held)))

    tuner_live = any(proc_running(p) for p in TUNER_PROCS)
    flags = set(stale_pause_flags())

    for dev in slots:
        d = st["devices"].get(dev, {})
        phys = str(d.get("physical_index", dev))
        if dev in held or phys in held:
            continue          # deliberately held; skip every liveness test

        # The broker probed this radio and it would not open. That is a DIRECT
        # observation, not an inference from silence, so it is reported at once
        # rather than waiting for dead_radios() to accumulate the two runs and
        # the producing-radio contrast it needs. It is also the one fault a
        # stack restart cannot touch: only removing the dongle's power clears
        # it, so say that instead of repairing around it.
        if d.get("wedged"):
            faults.append((dev, "WEDGED, NEEDS A PHYSICAL REPLUG: %s (since %s)"
                           % (d["wedged"], d.get("wedged_since", "?"))))
            continue
        if (PAUSE_PREFIX + dev) in flags and not tuner_live:
            faults.append((dev, "pause flag set with no live tuner (stale)"))
            continue
        if d.get("paused") and not tuner_live:
            faults.append((dev, "reports paused with no live tuner"))
            continue
        n = recent_coverage(dev, SILENT_MIN)
        if n == 0:
            faults.append((dev, "no lane completed in %d min" % SILENT_MIN))
        for lid, ls in (d.get("lanes") or {}).items():
            runs = ls.get("runs", 0)
            if runs >= 3 and ls.get("early_exits", 0) >= runs:
                faults.append((dev, "lane %s exits early on every run" % lid))
    # a lane that never gets a turn is invisible to every check above
    faults.extend(starved_lanes())
    faults.extend(dead_radios(st))
    return faults


DEAD_RADIO_MIN_LANES = 2      # below this, "all its lanes are quiet" means little
DEAD_RADIO_MIN_RUNS = 2       # a lane needs this many turns before it counts


def dead_radios(st):
    """A radio where EVERY lane produced nothing, while another radio is fine.

    Per-lane faults cannot see this. Each lane looks like its own problem, the
    watchdog repairs each one by restarting the stack, and the restart cannot
    fix the actual cause -- so it repeats forever. Measured 2026-09-20: one
    dongle's USB interface was stuck claimed for two days. Every open failed,
    the watchdog kept restarting, and nothing improved because the fault was
    below the software entirely.

    The signal is COMPARATIVE and that is what makes it trustworthy: one radio
    silent proves nothing (bands go quiet, antennas are bad), but one radio
    silent across every one of its lanes WHILE ANOTHER RADIO IS PRODUCING makes
    the radio itself the only variable that differs.
    """
    devs = st.get("devices") or {}
    if len(devs) < 2:
        return []                     # nothing to compare against
    produced, silent = {}, {}
    for dev, d in devs.items():
        judged = total = 0
        for ls in (d.get("lanes") or {}).values():
            if ls.get("runs", 0) < DEAD_RADIO_MIN_RUNS:
                continue              # not had a fair turn yet
            judged += 1
            total += ls.get("events_total", 0) or 0
        if judged >= DEAD_RADIO_MIN_LANES:
            (produced if total > 0 else silent)[dev] = judged
    if not produced or not silent:
        return []                     # no contrast -- say nothing
    out = []
    for dev, judged in sorted(silent.items()):
        out.append((dev, (
            "radio produced NOTHING across %d lanes while dev%s is producing "
            "-- the radio is the common factor, not the lanes. A stack restart "
            "will not fix this. Check the dongle: a stuck USB claim clears only "
            "on a replug, and 'rtl_test -d %s' will say so directly."
            % (judged, ", dev".join(sorted(produced)), dev))))
    return out


def lane_of(reason):
    """The lane id, but ONLY for the repeat-restart fault. Starvation reasons
    also name a lane and must not be muted -- a starved lane is one that never
    got a turn, and it is already handled by the starve_only branch below."""
    m = LANE_FAULT_RE.match(reason)
    return m.group(1) if m else None


def record_mute(dev, lane, reason, fails):
    """Durable, next to the data -- watchdog.json is state and gets rewritten."""
    try:
        with open(MUTE_LOG, "a") as f:
            f.write("%s  dev%s lane %s MUTED after %d consecutive failed repairs "
                    "-- %s -- lane still runs; it no longer triggers a stack restart\n"
                    % (now().isoformat(timespec="seconds"), dev, lane, fails, reason))
    except OSError:
        pass


RESET_COOLDOWN_MIN = 60       # at most one USB reset per radio per hour
RESET_TOOL = os.path.join(ROOT, "bin", "bw-radio-reset.py")


def try_radio_reset(state, dev):
    """Attempt a USB device reset for one logical radio. Returns what happened.

    Deliberately reports rather than claims. The tool re-probes the device
    AFTER resetting it, so the answer is measured, not assumed -- and when a
    reset cannot help, saying so plainly is the useful output, because the
    remaining fix is physical and no amount of retrying substitutes for it.
    """
    last = (state.setdefault("radio_resets", {}) or {}).get(str(dev))
    if last:
        try:
            age = (now() - parse_iso(last)).total_seconds() / 60.0
            if age < RESET_COOLDOWN_MIN:
                return ("reset skipped, last attempt %.0f min ago (cooldown %d min) "
                        "-- if it is still dead the fix is physical"
                        % (age, RESET_COOLDOWN_MIN))
        except Exception:
            pass
    if not os.path.exists(RESET_TOOL):
        return "reset tool missing at %s" % RESET_TOOL
    state["radio_resets"][str(dev)] = now().isoformat(timespec="seconds")
    try:
        out = subprocess.run([sys.executable, RESET_TOOL, "--slot", str(dev)],
                             capture_output=True, text=True, timeout=120)
    except Exception as e:                        # noqa: BLE001
        return "reset failed to run: %s" % e
    tail = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()][-3:]
    if out.returncode == 0:
        return "USB reset ran and the radio is claimable again | %s" % " ".join(tail)
    return ("USB reset ran and the radio is STILL unusable -- unplug it and plug "
            "it back in | %s" % " ".join(tail))


def repair(reason):
    log("REPAIR: %s" % reason)
    for p in stale_pause_flags():
        try:
            os.unlink(os.path.join("/tmp", p))
            log("  cleared stale flag /tmp/%s" % p)
        except OSError:
            pass
    subprocess.run(["pkill", "-f", "ffmpeg .*pipe:0"], capture_output=True)
    subprocess.run(["pkill", "-f", "rtl_fm "], capture_output=True)
    ctl = os.path.join(ROOT, "bin", "bandwatch")
    subprocess.run(["/bin/bash", ctl, "stop"], capture_output=True, timeout=150)
    time.sleep(3)
    subprocess.run(["/bin/bash", ctl, "start"], capture_output=True, timeout=200)
    log("  stack restarted")


def notify(text):
    c = cfg()
    if not c.get("notify_enabled"):
        log("NOTIFY suppressed (notify_enabled=false): %s" % text[:90])
        return
    payload = json.dumps({
        "jsonrpc": "2.0", "method": "send",
        "params": {"recipient": [c.get("signal_recipient")], "message": text},
        "id": "wd-%d" % int(time.time())})
    subprocess.run(["curl", "-s", "--max-time", "15", "-X", "POST",
                    c.get("signal_rpc", ""), "-H", "Content-Type: application/json",
                    "-d", payload], capture_output=True)
    log("NOTIFY sent: %s" % text.replace("\n", " ")[:110])


def heartbeat(faults):
    """Record that the watchdog RAN, separately from what it found.

    Under --quiet a healthy pass prints nothing, so the log cannot distinguish
    "checked, all well" from "never ran". That is precisely how a dead checker
    keeps claiming health: its last line stays on the screen forever. The
    supervisor bug that killed this watchdog for two days was invisible for
    exactly this reason."""
    try:
        s = load_state()
        s["last_check"] = now().isoformat(timespec="seconds")
        s["last_fault_count"] = len(faults)
        s["last_faults"] = ["dev%s: %s" % (d, r) for d, r in faults]
        save_state(s)
    except Exception:
        pass


def main():
    argv = sys.argv[1:]
    faults = check()
    heartbeat(faults)
    if not faults:
        if "--quiet" not in argv:
            log("all devices producing")
        # Nothing faulted, so every outstanding repair held. Clear the streaks
        # here too: this path returns before the per-lane bookkeeping below, and
        # without this a muted lane could never unmute.
        s = load_state()
        if s.get("lane_faults"):
            log("  clearing %d lane fault streak(s) -- the repairs held"
                % len(s["lane_faults"]))
            s["lane_faults"] = {}
            save_state(s)
        return 0
    for dev, reason in faults:
        log("FAULT dev%s: %s" % (dev, reason))
    if "--check" in argv:
        return 1

    s = load_state()

    # --- consecutive failed repairs, per (device, lane) ----------------------
    lf = s["lane_faults"]
    seen_now, kept = set(), []
    for dev, reason in faults:
        lane = lane_of(reason)
        if lane is None:
            kept.append((dev, reason))
            continue
        key = "dev%s|%s" % (dev, lane)
        seen_now.add(key)
        rec = lf.setdefault(key, {"count": 0, "muted": False,
                                  "first_ts": now().isoformat(timespec="seconds")})
        rec["count"] = rec.get("count", 0) + 1
        rec["last_ts"] = now().isoformat(timespec="seconds")
        rec["reason"] = reason

        if rec["count"] < LANE_REPAIR_LIMIT:
            kept.append((dev, reason))          # still worth a repair attempt
            continue

        if not rec.get("muted"):
            rec["muted"] = True
            rec["muted_ts"] = now().isoformat(timespec="seconds")
            record_mute(dev, lane, reason, rec["count"])
            log("MUTED dev%s lane %s after %d consecutive failed repairs"
                % (dev, lane, rec["count"]))
            notify("SDR watchdog: dev%s lane %s is NOT SELF-HEALING.\n%s\n"
                   "%d auto-repairs in a row failed, so the stack will stop "
                   "restarting for it. The lane still takes its turn; it just no "
                   "longer takes both radios down with it.\nNeeds hands."
                   % (dev, lane, reason, rec["count"]))
        else:
            log("  dev%s lane %s still faulting (%d) -- muted, not repairing"
                % (dev, lane, rec["count"]))

    # A lane that did NOT fault this pass was genuinely repaired: drop its streak
    # so it can be repaired again next time, and so a recovered lane unmutes.
    for key in list(lf):
        if key not in seen_now:
            del lf[key]

    faults = kept
    if not faults:
        save_state(s)
        return 1

    cut = (now() - timedelta(minutes=ESCALATE_WINDOW_MIN)).isoformat(timespec="seconds")
    devs = sorted({d for d, _ in faults})
    # a repeat fault on the same device means the previous repair did not hold
    repeats = [d for d in devs
               if any(r.get("device") == d and r.get("ts", "") >= cut
                      for r in s["repairs"])]

    # A dead radio is not repairable by restarting either, and restarting on it
    # churns the whole stack every cycle for a fault that is below the software.
    dead_only = all("the radio is the common factor" in r for _, r in faults)
    if dead_only:
        log("dead radio -- NOT restarting (the fault is below the software)")
        # Try the one recovery that can actually work here: a USB device reset.
        # Restarting the stack cannot clear a stuck interface claim; a reset
        # can. Bounded to one attempt per device per RESET_COOLDOWN_MIN so a
        # radio that is physically gone does not get reset every cycle forever.
        outcomes = []
        for dev, _r in faults:
            outcomes.append("dev%s: %s" % (dev, try_radio_reset(s, dev)))
        notify("SDR watchdog: a radio has stopped producing entirely.\n%s\n\n%s"
               % ("\n".join("dev%s: %s" % (d, r) for d, r in faults),
                  "\n".join(outcomes)))
        s["repairs"] = [r for r in s["repairs"] if r.get("ts", "") >= cut]
        save_state(s)
        return 1

    # A dongle whose USB interface has hung is the one fault a restart provably
    # cannot touch: the reset tool re-enumerates it and it still refuses to
    # open. Restarting the stack anyway would churn the OTHER radio for nothing
    # and bury the one message that says what to actually do.
    wedged_only = all("WEDGED" in r for _, r in faults)
    if wedged_only:
        log("wedged radio(s) only -- NOT restarting (only a replug clears this)")
        notify("SDR watchdog: a radio needs a PHYSICAL REPLUG.\n%s\n"
               "It enumerates but will not open. A USB reset cannot clear a "
               "dongle whose firmware has hung -- unplug it and plug it back "
               "in. The other radio is still running."
               % "\n".join("dev%s: %s" % (d, r) for d, r in faults))
        s["repairs"] = [r for r in s["repairs"] if r.get("ts", "") >= cut]
        save_state(s)
        return 1

    starve_only = all("lane " in r and ("starved" in r or "never run" in r.lower()
                                        or "last ran" in r) for _, r in faults)
    if starve_only:
        # restarting is what STARVES late lanes -- doing it again would make the
        # fault worse, so this one reports rather than repairs
        log("starvation only -- NOT restarting (a restart is what causes this)")
        notify("SDR watchdog: lane starvation.\n%s\nRotation should now resume "
               "across restarts; if this repeats, the cycle is too long."
               % "\n".join("dev%s: %s" % (d, r) for d, r in faults))
        s["repairs"] = [r for r in s["repairs"] if r.get("ts", "") >= cut]
        save_state(s)
        return 1

    repair("; ".join("dev%s: %s" % (d, r) for d, r in faults))
    for d in devs:
        s["repairs"].append({
            "device": d, "ts": now().isoformat(timespec="seconds"),
            "reason": next(r for dd, r in faults if dd == d)})
    s["repairs"] = [r for r in s["repairs"] if r.get("ts", "") >= cut]
    save_state(s)

    if repeats:
        notify("SDR watchdog: device(s) %s faulted AGAIN within %d min of a repair.\n%s\n"
               "Auto-repair ran but did not hold." %
               (", ".join(repeats), ESCALATE_WINDOW_MIN,
                "\n".join("dev%s: %s" % (d, r) for d, r in faults)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
