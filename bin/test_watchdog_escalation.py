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
sys.exit(0 if ok else 1)
