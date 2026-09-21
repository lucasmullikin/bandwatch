#!/usr/bin/env python3
"""Generate every rotation profile from one lane catalogue.

The catalogue and the profile membership both live in config/lanes.json. That
is deliberate, and it is a repair: this generator used to carry the catalogue
inline in Python while lanes were added and dwells tuned directly in the
generated JSON. Within weeks the generator was stale, regenerating would have
silently deleted six working lanes, and the only honest thing it could do was
refuse to run at all. A generator whose output is edited has no source of
truth. Now there is exactly one file to change, and the output is disposable.

Band split follows the ANTENNA, not the purpose -- you cannot retune an antenna
between duty cycles:
  dev0  low band  118-174 MHz   (dipole ~50cm, or a discone)
  dev1  high band 225-1090 MHz  (dipole ~17cm, or a discone)

Radios are pinned by SERIAL, never by index: librtlsdr order is not stable
across a replug, and a swapped index presents as BAD RECEPTION rather than an
error. A device with no serial in config is a hard failure, because generating
without one silently unpins the radio.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bandwatch_config as C  # noqa: E402

LIVE = C.PROFILE_DIR
# Generating to a scratch dir is always safe, so the drift guard below skips it.
#   BANDWATCH_PROFILES_OUT=/tmp/p python3 make_profiles.py   # then diff
OUT = os.environ.get("BANDWATCH_PROFILES_OUT") or LIVE


# --------------------------------------------------------------------------
# Lane builders. One per "kind" in config/lanes.json.
# --------------------------------------------------------------------------


# An RTL-SDR tunes ~2.4 MHz at a time, so rtl_power hops a wider range. This is
# what decides how long one pass takes, and therefore whether a pass can finish
# inside its lane slot at all.
HOP_MHZ = 2.4


def _span_mhz(low, high):
    """Width of a sweep range given rtl_power's "118M"/"1090M" spellings."""
    def mhz(v):
        v = str(v).strip()
        mult = {"k": 0.001, "M": 1.0, "G": 1000.0}
        return float(v[:-1]) * mult[v[-1]] if v and v[-1] in mult else float(v) / 1e6
    return abs(mhz(high) - mhz(low))


def _voice(lane, spec):
    conf = spec.get("conf") or (lane + ".conf")
    return dict(
        event_file=C.var("voice", lane),
        count_mode="mtime",
        expect_min_events_per_hour=spec.get("min_per_hour", 0),
        cmd=[os.path.join(C.ROOT, "bin", "lane-voice.sh"),
             "{DEV}", os.path.join(C.CONF_DIR, conf)])


def _rtl433(lane, spec):
    out = C.var("events", lane + ".jsonl")
    return dict(
        event_file=out,
        expect_min_events_per_hour=spec.get("min_per_hour", 0),
        cmd=["rtl_433", "-d", "{DEV}", "-f", spec["freq"], "-M", "utc",
             "-M", "time:iso", "-M", "protocol", "-M", "level",
             "-F", "json:" + out])


def _sweep(lane, spec):
    # rtl_power TRUNCATES its output and only flushes completed hops, so a
    # sweep killed at the end of its slot leaves nothing AND destroys what the
    # previous run left. This used to call rtl_power directly, which meant the
    # project shipped lane-survey.sh -- the wrapper written to fix exactly that,
    # after it cost one station a lane's entire history -- and then used it for
    # a single hand-edited lane. Every other sweep kept the bug the wrapper
    # exists to prevent. They all go through it now.
    #
    # mtime, not line count, is still the honest liveness signal: the file is
    # appended to rather than rewritten, so deltas are about growth.
    out = C.var("events", lane + ".csv")
    # -e must land INSIDE the slot, with room to write. Deriving it from the
    # dwell is the point: hand-maintained -e values drift away from the dwell
    # they were chosen for, and nothing flags it when they do.
    dwell = max(20, spec["seconds"] - 10)
    try:
        low, high, binsize = spec["range"].split(":")
    except ValueError:
        raise C.ConfigError(
            "lanes.json: lane %r has range %r; expected LOW:HIGH:BINSIZE, "
            "e.g. \"225M:400M:25k\"" % (lane, spec.get("range")))
    # ONE PASS must fit the slot, and -e alone does not guarantee it: rtl_power
    # honours -e at a PASS BOUNDARY, not mid-pass. A sweep whose pass takes
    # longer than its -e therefore runs until the pass completes, sails past
    # the dwell, and is SIGKILLed -- which is the thing that wedges the radio.
    #
    # Measured: air_survey needed 80s of pass in a 60s dwell, survey_fm 90s in
    # 45s, lora915 132s in 40s. All three were killed on most runs.
    #
    # So the integration time is clamped to whatever lets the pass finish. It
    # only ever goes DOWN: a configured value that already fits is untouched.
    interval = int(spec.get("interval", 10))
    hops = max(1, math.ceil(_span_mhz(low, high) / HOP_MHZ))
    fits = max(1, dwell // hops)
    if interval > fits:
        spec = dict(spec, _interval_clamped_from=interval)
        interval = fits
    return dict(
        event_file=out,
        self_terminating=True,
        count_mode="mtime",
        expect_min_events_per_hour=spec.get("min_per_hour", 1),
        sweep_hops=hops,
        sweep_pass_s=hops * interval,
        cmd=[os.path.join(C.ROOT, "bin", "lane-survey.sh"), "{DEV}",
             low, high, binsize, str(interval), str(dwell),
             out, str(spec.get("gain", "40"))])


def _script(lane, spec):
    """A lane whose command is one of bandwatch's own lane-*.sh helpers."""
    argv = [os.path.join(C.ROOT, "bin", spec["script"]), "{DEV}"]
    argv += [str(a) for a in spec.get("args", [])]
    return dict(
        event_file=C.var(spec["event_file"]),
        count_mode=spec.get("count_mode", "lines"),
        expect_min_events_per_hour=spec.get("min_per_hour", 0),
        cmd=argv)


def _raw(lane, spec):
    """An external decoder invoked directly. {DEV} is substituted at run time."""
    return dict(
        event_file=C.var(spec["event_file"]),
        count_mode=spec.get("count_mode", "lines"),
        expect_min_events_per_hour=spec.get("min_per_hour", 0),
        cmd=[str(a) for a in spec["cmd"]])


KINDS = {"voice": _voice, "rtl433": _rtl433, "sweep": _sweep,
         "script": _script, "raw": _raw}


def build_lane(lane_id, spec):
    kind = spec.get("kind")
    if kind not in KINDS:
        raise C.ConfigError(
            "lanes.json: lane %r has kind %r; expected one of %s"
            % (lane_id, kind, ", ".join(sorted(KINDS))))
    if not isinstance(spec.get("seconds"), int) or spec["seconds"] <= 0:
        raise C.ConfigError(
            "lanes.json: lane %r needs a positive integer 'seconds' (its dwell)" % lane_id)
    base = dict(id=lane_id, enabled=spec.get("enabled", True),
                seconds=spec["seconds"], note=spec.get("note", ""))
    base.update(KINDS[kind](lane_id, spec))
    return base


# min_per_hour belongs here for the same reason seconds does: what a lane should
# produce depends on how long it is given. A sweep pinned for 300s in the day
# profile and 180s during an aviation event is reasonably expected to deliver
# different amounts, and a watchdog holding it to one number in both either
# cries wolf in one profile or sleeps through a fault in the other.
OVERRIDABLE = ("seconds", "enabled", "min_per_hour")


def split_entry(entry, profile, dev):
    """A profile's lane entry -> (lane_id, overrides).

    Two spellings, because most lanes want the catalogue dwell and a few must
    differ by duty cycle:

        "adsb"                        the catalogue spec, unchanged
        {"id": "adsb", "seconds": 7200}   the catalogue spec, pinned

    Only `seconds` and `enabled` may be overridden. Anything else is refused
    rather than ignored: a profile that silently drops "second": 7200 leaves
    the lane on its catalogue dwell and still loads, rotates and looks correct,
    which is the one failure this schema exists to prevent.
    """
    where = "lanes.json: profile %r device %s" % (profile, dev)
    if isinstance(entry, str):
        return entry, {}
    if not isinstance(entry, dict):
        raise C.ConfigError(
            "%s has a lane entry that is neither a lane id nor an object: %r"
            % (where, entry))
    lane_id = entry.get("id")
    if not isinstance(lane_id, str) or not lane_id:
        raise C.ConfigError(
            "%s has a lane entry with no 'id': %r.\n"
            "  Write it as {\"id\": \"adsb\", \"seconds\": 7200}." % (where, entry))
    unknown = sorted(k for k in entry if k != "id" and k not in OVERRIDABLE)
    if unknown:
        raise C.ConfigError(
            "%s lane %r overrides unknown field(s): %s.\n"
            "  A profile may override only %s -- everything else about a lane\n"
            "  belongs in the catalogue, where there is one copy of it."
            % (where, lane_id, ", ".join(unknown), " and ".join(OVERRIDABLE)))
    if "seconds" in entry:
        secs = entry["seconds"]
        # bool is an int in Python and would sail through as a 1-second dwell.
        if isinstance(secs, bool) or not isinstance(secs, int) or secs <= 0:
            raise C.ConfigError(
                "%s lane %r has seconds %r; a dwell is a positive whole number "
                "of seconds" % (where, lane_id, secs))
    return lane_id, {k: entry[k] for k in OVERRIDABLE if k in entry}


def build_profiles(doc):
    lanes = doc.get("lanes")
    if not isinstance(lanes, dict) or not lanes:
        raise C.ConfigError("lanes.json has no 'lanes' object")
    devices = doc.get("devices") or {}
    built = []
    for p in doc.get("profiles") or []:
        out = {"name": p["name"], "description": p.get("description", ""), "devices": {}}
        for dev, ids in (p.get("devices") or {}).items():
            meta = devices.get(dev) or {}
            serial = meta.get("serial")
            if not serial:
                raise C.ConfigError(
                    "lanes.json: devices.%s has no serial.\n"
                    "  Pin every radio by serial -- librtlsdr index order is not stable\n"
                    "  across a replug, and a swapped index looks like bad reception,\n"
                    "  never like an error. Find yours with:  rtl_test -t" % dev)
            entries = [split_entry(e, p["name"], dev) for e in ids]
            missing = [i for i, _ in entries if i not in lanes]
            if missing:
                raise C.ConfigError(
                    "lanes.json: profile %r device %s references undefined lane(s): %s"
                    % (p["name"], dev, ", ".join(missing)))
            out["devices"][dev] = {
                "label": meta.get("label", ""),
                "serial": serial,
                # dict(catalogue) first: an override must never write through
                # into the catalogue, or it would follow the lane into every
                # profile built after this one.
                "lanes": [build_lane(i, dict(lanes[i], **ov)) for i, ov in entries],
            }
        built.append(out)
    if not built:
        raise C.ConfigError("lanes.json defines no profiles")
    return built


def drift(profiles, out_dir):
    """What writing `profiles` into out_dir would DESTROY.

    With the catalogue in config this should now always come back empty -- the
    generator can no longer fall behind its own output. It is kept because the
    day it reports something is the day someone hand-edited a generated file,
    and losing a working lane silently is exactly the failure this project
    exists to not have.
    """
    if os.path.abspath(out_dir) != os.path.abspath(LIVE):
        return []                       # scratch output destroys nothing
    lost = []
    for p in profiles:
        path = os.path.join(out_dir, p["name"] + ".json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as fh:
                live = json.load(fh)
        except (OSError, ValueError):
            continue
        for dk, dv in live.get("devices", {}).items():
            gen = p["devices"].get(dk, {})
            have = {l["id"] for l in gen.get("lanes", [])}
            for lane in dv.get("lanes", []):
                if lane["id"] not in have:
                    lost.append("%s dev%s: lane %s would be DELETED"
                                % (p["name"], dk, lane["id"]))
            if dv.get("serial") and not gen.get("serial"):
                lost.append("%s dev%s: serial %s would be LOST (radios unpinned)"
                            % (p["name"], dk, dv["serial"]))
    return lost


def main():
    doc = C.load("lanes")
    profiles = build_profiles(doc)
    problems = drift(profiles, OUT)
    if problems and "--force" not in sys.argv:
        print("REFUSING to regenerate: a generated profile has been hand-edited "
              "and regenerating would destroy the change.\n")
        for line in problems:
            print("  " + line)
        print("\nconfig/lanes.json is the source of truth. Move the edit there, "
              "then regenerate.\nTo inspect what this WOULD write, without "
              "touching anything:\n"
              "    BANDWATCH_PROFILES_OUT=/tmp/prof python3 make_profiles.py\n"
              "Only --force overrides this, and it will destroy the lanes listed "
              "above.")
        return 1
    os.makedirs(OUT, exist_ok=True)
    for p in profiles:
        with open(os.path.join(OUT, p["name"] + ".json"), "w") as fh:
            json.dump(p, fh, indent=2)
        tot = {d: sum(l["seconds"] for l in s["lanes"] if l["enabled"])
               for d, s in p["devices"].items()}
        cols = "   ".join(
            f"dev{d} {tot[d]:>5}s ({tot[d]/60:4.1f}m, "
            f"{len(p['devices'][d]['lanes'])} lanes)" for d in sorted(p["devices"]))
        print(f"{p['name']:<17} {cols}")
    print("\n  wrote %d profile(s) to %s" % (len(profiles), OUT))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except C.ConfigError as e:
        raise SystemExit("bandwatch: %s" % e)
