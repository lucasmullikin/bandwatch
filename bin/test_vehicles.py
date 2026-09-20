"""Controls for the overnight vehicle watch. Scratch DB + scratch source, so
the real baseline is untouched and nothing is sent."""
import importlib.util, json, os, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("v", os.path.join(HERE, "bw-vehicles.py"))
v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)

tmp = tempfile.mkdtemp()
v.DB = os.path.join(tmp, "t.db"); v.STATE = os.path.join(tmp, "s.json")
v.LOG = os.path.join(tmp, "t.log"); v.SRC = os.path.join(tmp, "tpms.jsonl")
sent = []; v.send = lambda t: sent.append(t)
v.cfg = lambda: {"notify_enabled": True, "signal_recipient": "+1", "signal_rpc": "http://x"}

def row(proto, sid, dt, psi=34.0):
    return json.dumps({"time": dt.isoformat(timespec="seconds"), "model": proto,
                       "id": sid, "pressure_PSI": psi, "rssi": -9.0, "snr": 12.0})

base = datetime.now(timezone.utc) - timedelta(hours=3)
# a KNOWN resident car, seen on many earlier passes
known = [row("Toyota", "aa0001", base - timedelta(days=d)) for d in range(1, 6)]
open(v.SRC, "w").write("\n".join(known) + "\n")

con = sqlite3.connect(v.DB); v.ensure_schema(con); s = v.load_state()
v.ingest(con, s); s["baselined"] = True; v.save_state(s)
print("baseline sensors:", con.execute("select count(*) from vehicle_sensors").fetchone()[0])

# overnight: the resident again, PLUS one unknown 4-wheel Ford set arriving
# together, PLUS a single unknown Schrader wheel passing once
new = [row("Toyota", "aa0001", base)]
for i, sid in enumerate(["bb1000", "bb1002", "bb1004", "bb1006"]):
    new.append(row("Ford", sid, base + timedelta(seconds=10 + i * 20)))
new.append(row("Schrader-EG53MA4", "CC9999", base + timedelta(minutes=40)))
open(v.SRC, "a").write("\n".join(new) + "\n")

n, fresh = v.ingest(con, s); v.save_state(s)
print("ingested %d rows, %d new sensors" % (n, len(fresh)))
text, nclusters = v.digest(con, s)
print("-" * 60); print(text); print("-" * 60)

ok = True
def check(c, m):
    global ok; print(("  PASS  " if c else "  FAIL  ") + m); ok = ok and c

check(len(fresh) == 5, "5 new wheel sensors detected (4 Ford + 1 Schrader)")
check("aa0001" not in text, "the KNOWN resident car is not reported as new")
check(nclusters == 2, "5 sensors resolve to 2 vehicles, not 5")
check("4 wheels -- a full set" in text, "the Ford set is recognised as one full set")
check("Ford / Lincoln" in text, "make family reported from the protocol")
check("ids run consecutively" in text, "consecutive ids flagged as corroboration")
check("GM / Stellantis / Nissan" in text, "Schrader reported as multi-make, not guessed")
check("passing through, not parked" in text, "a once-heard wheel is called out as passing")
check("not a model" in text, "the report states the limit of the inference")
print("-" * 60)
print("VEHICLE WATCH LOGIC: " + ("CORRECT" if ok else "BROKEN"))
sys.exit(0 if ok else 1)
