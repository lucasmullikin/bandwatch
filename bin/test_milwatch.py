"""Controls for the military watch's decide-and-record logic.

Runs against a scratch DB and a stubbed feed, so nothing pages and the live
record is untouched. Proves the three properties that matter:
  * a close aircraft PAGES, a distant one is LOGGED ONLY, a far one is neither
  * the same airframe does not page twice inside ALERT_REPEAT_MIN
  * a failed fetch writes a health row saying so -- it NEVER looks like "no
    military aircraft"
"""
import importlib.util, os, sqlite3, sys, tempfile, json

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, os.path.dirname(HERE))
spec = importlib.util.spec_from_file_location("mw", os.path.join(HERE, "bw-milwatch.py"))
mw = importlib.util.module_from_spec(spec); spec.loader.exec_module(mw)

tmp = tempfile.mkdtemp()
mw.DB = os.path.join(tmp, "t.db")
mw.STATE = os.path.join(tmp, "s.json")
mw.LOG = os.path.join(tmp, "t.log")
# Pin the station position: home() reads config, and a unit test must not need
# a configured station. The coordinates below are what the distances in this
# file are computed against.
mw.home = lambda: (40.65, -76.25)
pages = []
mw.notify = lambda text, s: pages.append(text)
mw.cfg = lambda: {"notify_enabled": True, "signal_recipient": "+1", "signal_rpc": "http://x",
                  "quiet_start": "22:00", "quiet_end": "07:00",
                  "max_per_rule_per_hour": 10, "max_alerts_per_hour": 30}

con = sqlite3.connect(mw.DB); mw.ensure_schema(con)
s = mw.load_state()

# The reference site is 40.65,-76.25. 1 deg lat ~= 60 nm.
CLOSE = {"hex": "ae1111", "flight": "REACH01 ", "t": "C17", "r": "00-0185",
         "lat": 40.9, "lon": -76.25, "alt_baro": 24000, "gs": 410}
MID   = {"hex": "ae2222", "flight": "GRIZZLY2", "t": "KC135", "r": "58-0118",
         "lat": 43.3, "lon": -76.25, "alt_baro": 31000, "gs": 440}   # ~160 nm
FAR   = {"hex": "ae3333", "flight": "FARAWAY3", "t": "C130", "r": "08-5679",
         "lat": 49.0, "lon": -76.25, "alt_baro": 25000, "gs": 300}   # ~500 nm

def run(aclist, label, fail=False):
    if fail:
        mw.fetch = lambda: (_ for _ in ()).throw(OSError("simulated feed outage"))
    else:
        mw.fetch = lambda: {"ac": list(aclist)}
    before = len(pages)
    mw.poll_once(con, s)
    n_s = con.execute("select count(*) from mil_sightings").fetchone()[0]
    h = con.execute("select ok, n_in_range, error from mil_watch_health "
                    "order by ts desc limit 1").fetchone()
    print("%-38s pages=%d sightings=%d health=%s" % (label, len(pages)-before, n_s, h))
    return len(pages) - before, n_s, h

ok = True
def check(c, m):
    global ok; print(("  PASS  " if c else "  FAIL  ") + m); ok = ok and c

p1, n1, h1 = run([CLOSE, MID, FAR], "1: close + mid + far")
check(p1 == 1, "one page raised (the close aircraft), not three")
check("REACH01" in pages[-1], "the page names the close aircraft")
check("FARAWAY3" not in pages[-1], "the far aircraft is not paged")
check(n1 == 2, "close+mid LOGGED (<=250nm); far one is outside the log radius")
check(h1[0] == 1 and h1[1] == 2, "health row records 2 in range")

s["logged"] = {}   # defeat only the WRITE throttle, so the alert dedupe is what we test
p2, n2, h2 = run([CLOSE, MID, FAR], "2: same aircraft again")
check(p2 == 0, "same airframe does NOT page twice inside ALERT_REPEAT_MIN")

p3, n3, h3 = run([], "3: feed outage", fail=True)
check(p3 == 0, "an outage does not page a sighting")
check(h3[0] == 0 and h3[2] is not None, "outage writes ok=0 WITH the error text")
check(h3[1] is None, "outage records n_in_range=NULL, never 0 -- a gap is not a quiet sky")

print("---")
print("MIL WATCH LOGIC: " + ("CORRECT" if ok else "BROKEN"))
sys.exit(0 if ok else 1)
