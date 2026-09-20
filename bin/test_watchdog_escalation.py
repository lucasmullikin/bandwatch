"""Negative+positive control for the watchdog's consecutive-failed-repair logic.

Stubs repair() and notify() so nothing real happens, and drives the SAME main()
the supervisor calls. Uses a scratch STATE file so live state is untouched.
"""
import importlib.util, os, sys, tempfile, json

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "wd", os.path.join(HERE, "bw-watchdog.py"))
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

tmp = tempfile.mkdtemp()
wd.STATE = os.path.join(tmp, "watchdog.json")
wd.MUTE_LOG = os.path.join(tmp, "muted.log")
wd.LOG = os.path.join(tmp, "wd.log")

repairs, notifies = [], []
wd.repair = lambda reason: repairs.append(reason)
wd.notify = lambda text: notifies.append(text)
wd.heartbeat = lambda faults: None

FAULT = [("1", "lane adsb exits early on every run")]

def run(faults, label):
    wd.check = lambda: list(faults)
    before_r, before_n = len(repairs), len(notifies)
    wd.main()
    st = json.load(open(wd.STATE))
    rec = st.get("lane_faults", {}).get("dev1|adsb", {})
    print("%-34s repair=%s notify=%s count=%s muted=%s"
          % (label, len(repairs) - before_r, len(notifies) - before_n,
             rec.get("count", "-"), rec.get("muted", "-")))
    return len(repairs) - before_r, len(notifies) - before_n

r1, n1 = run(FAULT, "pass 1 (fault)")
r2, n2 = run(FAULT, "pass 2 (fault)")
r3, n3 = run(FAULT, "pass 3 (fault, hits limit)")
r4, n4 = run(FAULT, "pass 4 (fault, already muted)")
r5, n5 = run(FAULT, "pass 5 (fault, already muted)")
r6, n6 = run([],    "pass 6 (CLEAN - streak resets)")
r7, n7 = run(FAULT, "pass 7 (fault again after clean)")

print("---")
ok = True
def check(cond, msg):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    ok = ok and cond

check(r1 == 1 and r2 == 1, "passes 1-2 still attempt a repair")
check(n1 == 0, "pass 1 stays quiet (a transient self-heals silently)")
check(n2 == 1, "pass 2 pages via the pre-existing 60min repeat rule")
check(r3 == 0, "pass 3 does NOT restart the stack -- the loop is broken")
check(n3 == 1, "pass 3 pages exactly once")
check(r4 == 0 and r5 == 0, "passes 4-5 never restart again")
check(n4 == 0 and n5 == 0, "passes 4-5 do not re-page (no alert spam)")
check(r7 == 1, "after a clean pass the streak resets and repair resumes")
print("---")
print("ESCALATION LOGIC: " + ("CORRECT" if ok else "BROKEN"))
escalation_ok = ok


# ---------------------------------------------------------------------------
# A radio that produces nothing while the other one is fine.
#
# Every test below has its negative control, because the failure that matters
# here is a check that cries "dead radio" at a quiet band -- that would train
# the operator to ignore the one message that means the hardware is gone.
# ---------------------------------------------------------------------------

def _lanes(spec):
    """spec: {lane: (runs, events_total)}"""
    return {k: {"runs": r, "events_total": e} for k, (r, e) in spec.items()}


def _state(devs):
    return {"devices": {d: {"lanes": _lanes(sp)} for d, sp in devs.items()}}


_dead_checks = []


def _dead(label, state, expect_devs):
    got = sorted(d for d, _ in wd.dead_radios(state))
    ok = got == sorted(expect_devs)
    _dead_checks.append(ok)
    print("  %s  %-58s got=%s want=%s" % ("PASS" if ok else "FAIL", label, got, expect_devs))


print("\n--- dead radio detection ---")

# the real incident: dev1 silent across 4 lanes, dev0 producing
_dead("one radio silent, the other producing -> flagged",
      _state({"0": {"acars": (3, 116), "air_survey": (3, 40)},
              "1": {"adsb": (3, 0), "ism433": (3, 0),
                    "p25_survey": (3, 0), "survey_fm": (3, 0)}}),
      ["1"])

# NEGATIVE CONTROL: both radios quiet is a quiet night, not a dead radio
_dead("both radios silent -> NOT flagged (no contrast)",
      _state({"0": {"a": (3, 0), "b": (3, 0)},
              "1": {"c": (3, 0), "d": (3, 0)}}),
      [])

# NEGATIVE CONTROL: both producing
_dead("both producing -> NOT flagged",
      _state({"0": {"a": (3, 5), "b": (3, 1)},
              "1": {"c": (3, 2), "d": (3, 9)}}),
      [])

# NEGATIVE CONTROL: a lane that has not had a fair turn is not evidence
_dead("silent radio whose lanes barely ran -> NOT flagged",
      _state({"0": {"a": (3, 5), "b": (3, 1)},
              "1": {"c": (1, 0), "d": (1, 0)}}),
      [])

# NEGATIVE CONTROL: a single lane is not enough to condemn a radio
_dead("silent radio with only ONE judged lane -> NOT flagged",
      _state({"0": {"a": (3, 5), "b": (3, 1)},
              "1": {"c": (3, 0)}}),
      [])

# one producing lane is enough to clear the radio
_dead("radio with one producing lane among many -> NOT flagged",
      _state({"0": {"a": (3, 5), "b": (3, 1)},
              "1": {"c": (3, 0), "d": (3, 0), "e": (3, 2)}}),
      [])

# a single-radio station must never self-condemn: nothing to compare with
_dead("single radio only -> NOT flagged (nothing to compare)",
      _state({"0": {"a": (3, 0), "b": (3, 0)}}),
      [])

print("---")
dead_ok = all(_dead_checks)
print("DEAD RADIO DETECTION: " + ("CORRECT" if dead_ok else "FAILED"))

# One exit for the whole file, at the end. The escalation block used to exit
# here, so every check appended after it never ran while the file still
# reported success.
sys.exit(0 if (escalation_ok and dead_ok) else 1)
