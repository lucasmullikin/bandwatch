#!/usr/bin/env python3
"""Cross-lane correlation (T5) and incident clustering (T6).

The collector runs several independent lanes -- adsb, acars, voice, ism,
pager -- into one `events` table (plus the separate `voice` and
`aircraft_positions` tables) and never joins them. One real-world aircraft
movement can show up as three unrelated console rows: an ADS-B hex, an
ACARS tail, and a tower transmission. T5 joins those identities back
together for a single aircraft; T6 groups the resulting stream into
incidents so the console can show "one thing happened" instead of a wall
of rows.

Pure stdlib + sqlite3. Read-only against events/devices/voice/
aircraft_positions -- store_incidents() is the only write, and it only
touches a new `incidents` table it creates.

EVIDENCE, NOT ONE BLOB
----------------------------------------------------------------------
Three join mechanisms are used, and they are not equally strong:
  * 'hex match'    -- the row's own device_key IS the ICAO hex. Fact.
  * 'tail match'   -- an ACARS row's parsed tail equals the aircraft's
                       registration. Also a fact (a tail number is a real,
                       publicly registered identifier), but a second,
                       independent data source, not raw ADS-B.
  * 'time+channel proximity' -- a voice transmission on an airband channel
                       happened close in time to one of the aircraft's own
                       position reports. This is INFERENCE: two things
                       being close in time does not prove they are the
                       same thing (there is more than one aircraft in the
                       sky, and more than one radio call in a given
                       minute). Every function below tags this evidence
                       type explicitly and never merges it with the other
                       two -- a caller (or a human reading the console)
                       must always be able to tell a hex match from a
                       coincidence.

REGISTRATION PARSING
----------------------------------------------------------------------
collector.py's enrich_aircraft() (collector.py:694-717) writes
devices.label for adsb rows as `("%s %s" % (registration, type)).strip()`.
When registration is empty this collapses to just the type ("BELL 407",
"A320") -- so the first whitespace token of label is NOT reliably a tail
number, it's only a tail number when a registration was actually present.
_parse_registration_from_label() only trusts a first token that matches
the FAA N-number shape (N + leading digit + up to 4 more alphanumerics),
which is what a US-based receiver mostly sees. Change the shape if your
airspace is dominated by another registry.
It will not recognise a foreign registration (e.g. "C-FABC") out of a
label -- documented limitation, not silently guessed at.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

# --- timestamps ----------------------------------------------------------


def _parse_ts(ts):
    """ISO8601 UTC string -> aware datetime, or None. Same tolerant parse
    used elsewhere in this project (see aircraft_behaviour.py's _parse_ts)
    -- collector.py's normalise_ts() always stores a real ISO8601 string,
    but test/replay data may use 'Z' where collector.py uses '+00:00'."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- registration parsing --------------------------------------------------

# FAA N-number: 'N' + a leading digit (no leading zero in practice, but
# that's a registration-issuance rule, not a shape rule -- not enforced
# here) + up to 4 more alphanumerics. Requiring the digit right after 'N'
# is what keeps this from matching the first word of a bare type
# description ("BELL", "A320" starts with 'A' anyway, but a hypothetical
# "N262"-style ICAO type code is the one real edge case this can't rule
# out -- see module docstring).
_TAIL_RE = re.compile(r"^N[0-9][0-9A-Z]{0,4}$")


def _parse_registration_from_label(label):
    """First token of a devices.label string, only if it looks like a
    real N-number. None otherwise (never guesses)."""
    if not label or not label.strip():
        return None
    first = label.strip().split(None, 1)[0].upper()
    return first if _TAIL_RE.match(first) else None


def hex_to_tail(conn, hex, lookup=None):
    """ICAO hex -> FAA registration ("tail"), or None.

    Primary source: devices.label, already enriched by collector.py's
    enrich_aircraft() -- see module docstring. This needs no network or
    dataset access for the common case where enrichment has already run,
    which is what makes it cheap to call from related_events()/
    find_incidents() for every hex seen.

    `lookup`, if given, is a fallback callable `lookup(hex) -> dict|None`
    with the same {"registration", "type", "operator"} shape as
    enrich.aircraft_db.lookup(), used only when devices.label doesn't
    resolve (not yet enriched, or enrichment ran before this hex was ever
    labelled). Injectable rather than imported directly so this module
    stays pure-stdlib and testable without the 32MB tar1090 dataset --
    real callers can pass `enrich.aircraft_db.lookup`. Unlike the label
    path, a `lookup` hit is trusted as-is (its "registration" field is a
    real registry field, not a composited/ambiguous string), so it is not
    restricted to the N-number shape.
    """
    h = (hex or "").strip().upper()
    if not h:
        return None
    row = conn.execute(
        "SELECT label FROM devices WHERE device_key = ? AND kind = 'adsb'", (h,)
    ).fetchone()
    tail = _parse_registration_from_label(row[0] if row else None)
    if tail:
        return tail
    if lookup is not None:
        try:
            info = lookup(h)
        except Exception:
            info = None
        if info:
            reg = (info.get("registration") or "").strip().upper()
            if reg:
                return reg
    return None


def aircraft_identities(conn, hex, lookup=None):
    """{"hex", "tail", "callsign", "label", "registration"} for one ICAO
    hex. "tail" and "registration" are intentionally the same value under
    two names -- "tail" for code that's about to match it against an ACARS
    device_key, "registration" for anything display-facing -- there is
    only one parse, not two independent identifiers.

    "callsign" is the most recent non-blank callsign this hex has reported
    in aircraft_positions, or None if it never has (many position reports
    carry no callsign field at all)."""
    h = (hex or "").strip().upper()
    tail = hex_to_tail(conn, h, lookup=lookup)
    row = conn.execute(
        "SELECT label FROM devices WHERE device_key = ? AND kind = 'adsb'", (h,)
    ).fetchone()
    label = row[0] if row else None
    csrow = conn.execute(
        "SELECT callsign FROM aircraft_positions "
        "WHERE hex = ? AND callsign IS NOT NULL AND TRIM(callsign) <> '' "
        "ORDER BY ts DESC LIMIT 1",
        (h,),
    ).fetchone()
    callsign = csrow[0].strip() if csrow and csrow[0] else None
    return {"hex": h, "tail": tail, "callsign": callsign, "label": label,
            "registration": tail}


# --- airband -----------------------------------------------------------

# Standard VHF civil aviation comm band (ICAO Annex 10): 118.000-136.975
# MHz. A frequency check, not a channel-label check, because rtl_airband
# channel `label`s (conf/*.conf) are free-text config and could be renamed
# without this module knowing -- the frequency the receiver actually tuned
# is the fact. This project's live airband lanes (air_tower, air_ground,
# guard121 -- see conf/*.conf) all fall inside this range; conf/milair.conf
# is UHF (225-400 MHz) and is deliberately NOT included here -- folding
# milair into "airband" is a real judgment call this brief didn't ask for,
# left to a future ticket rather than assumed.
_AIRBAND_MIN_MHZ = 118.0
_AIRBAND_MAX_MHZ = 136.975

# Operator ruling: UHF milair IS aircraft voice for this project. the local ANG base
# ANG is a named monitoring target and conf/milair.conf + conf/guard243.conf
# are live lanes, so excluding 225-400 MHz would drop exactly the military
# aircraft this system exists to notice. Both ranges are aeronautical voice;
# neither is a claim about WHICH aircraft is speaking -- that stays inference,
# tagged as time+channel proximity.
_BANDS_MHZ = (
    (118.0, 136.975),    # VHF civil airband, ICAO Annex 10
    (225.0, 400.0),      # UHF military air, incl. 243.0 guard
)


def _is_airband(freq_mhz):
    if freq_mhz is None:
        return False
    return any(lo <= freq_mhz <= hi for lo, hi in _BANDS_MHZ)


# --- T5: related_events ---------------------------------------------------


def related_events(conn, hex, window_s=300, lookup=None):
    """Every event plausibly about one aircraft: its own adsb rows (hex
    match), acars rows whose tail matches (tail match), and voice rows on
    an airband channel within window_s of one of its position reports
    (time+channel proximity -- inference, see module docstring).

    Each returned dict: {"source" ("events"|"voice"), "id", "ts", "kind",
    "device_key", "channel", "summary", "evidence"}. "source"+"id" together
    identify the row because `events` and `voice` are separate tables with
    independent id spaces -- a bare id would be ambiguous.

    Sorted by ts. Empty list (never an exception) for an unknown hex or an
    empty database.
    """
    h = (hex or "").strip().upper()
    if not h:
        return []

    results = []

    for eid, ts, kind, device_key, summary in conn.execute(
        "SELECT id, ts, kind, device_key, summary FROM events "
        "WHERE kind = 'adsb' AND device_key = ? ORDER BY ts",
        (h,),
    ):
        results.append({
            "source": "events", "id": eid, "ts": ts, "kind": kind,
            "device_key": device_key, "channel": None, "summary": summary,
            "evidence": "hex match",
        })

    tail = hex_to_tail(conn, h, lookup=lookup)
    if tail:
        acars_key = "acars/%s" % tail
        for eid, ts, kind, device_key, summary in conn.execute(
            "SELECT id, ts, kind, device_key, summary FROM events "
            "WHERE kind = 'acars' AND device_key = ? ORDER BY ts",
            (acars_key,),
        ):
            results.append({
                "source": "events", "id": eid, "ts": ts, "kind": kind,
                "device_key": device_key, "channel": None, "summary": summary,
                "evidence": "tail match",
            })

    pos_ts = [t for t in (
        _parse_ts(r[0]) for r in conn.execute(
            "SELECT ts FROM aircraft_positions WHERE hex = ? ORDER BY ts", (h,)
        ).fetchall()
    ) if t is not None]

    if pos_ts:
        for vid, vts, channel, freq_mhz, transcript in conn.execute(
            "SELECT id, ts, channel, freq_mhz, transcript FROM voice ORDER BY ts"
        ):
            if not _is_airband(freq_mhz):
                continue
            vdt = _parse_ts(vts)
            if vdt is None:
                continue
            if any(abs((vdt - p).total_seconds()) <= window_s for p in pos_ts):
                results.append({
                    "source": "voice", "id": vid, "ts": vts, "kind": "voice",
                    "device_key": None, "channel": channel, "summary": transcript,
                    "evidence": "time+channel proximity",
                })

    results.sort(key=lambda r: r["ts"])
    return results


# --- T6: find_incidents / store_incidents ----------------------------------

# Score weights. lane_diversity is squared and dominates: going from 1
# lane to 2 lanes confirming the same real-world event is far more
# significant than going from 5 events to 6 within a single lane (six
# TPMS pings from one tire sensor is not more "interesting" than three
# events split across adsb+acars+voice -- see module docstring / brief).
# The event-count term is log-scaled specifically so a large single-lane
# burst can't out-grow it linearly and eventually out-rank genuine
# cross-lane correlation just by having enough rows.
_LANE_DIVERSITY_WEIGHT = 10.0
_EVENT_COUNT_WEIGHT = 2.0


class _UnionFind:
    """Minimal path-compressed union-find over list indices. No external
    dependency -- this is ~6 lines, not worth a library for it."""

    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def find_incidents(conn, gap_s=240, min_events=3):
    """Cluster events (+ voice rows) into incidents by shared entity, then
    split each entity-linked cluster on time gaps > gap_s.

    Entity linkage reuses exactly the evidence classes related_events()
    uses -- same device_key/channel, hex<->acars-tail match, and
    hex<->voice time+channel proximity (using gap_s as the proximity
    window here, since find_incidents has no separate window_s param) --
    so an incident never merges two rows on a basis related_events()
    wouldn't accept as a join. Same raw device_key/channel always links
    (that's what makes six same-hex pings or a channel's own back-to-back
    transmissions one candidate cluster); cross-lane links only exist for
    aircraft (adsb/acars/voice), matching what T5 established.

    O(distinct_hexes * n) -- fine at this project's scale (dozens of
    aircraft, a modest events table); not indexed further since there is
    no evidence yet that it needs to be.

    Returns a list of {"start", "end", "kinds", "entities", "events",
    "score"}, "events" being "source:id" strings (see related_events()'s
    docstring on why a bare id is ambiguous), sorted by start. Empty list
    for an empty database, never an exception.
    """
    occ = []  # each: {source, id, ts, dt, kind, entity, freq_mhz}

    for eid, ts, kind, device_key, summary in conn.execute(
        "SELECT id, ts, kind, device_key, summary FROM events ORDER BY ts"
    ):
        dt = _parse_ts(ts)
        if dt is None:
            continue
        occ.append({"source": "events", "id": eid, "ts": ts, "dt": dt,
                     "kind": kind, "entity": device_key, "freq_mhz": None})

    for vid, vts, channel, freq_mhz, transcript in conn.execute(
        "SELECT id, ts, channel, freq_mhz, transcript FROM voice ORDER BY ts"
    ):
        dt = _parse_ts(vts)
        if dt is None:
            continue
        occ.append({"source": "voice", "id": vid, "ts": vts, "dt": dt,
                     "kind": "voice",
                     "entity": ("channel:%s" % channel) if channel else None,
                     "freq_mhz": freq_mhz})

    n = len(occ)
    if n == 0:
        return []

    uf = _UnionFind(n)
    # anchor occurrence index -> voice occurrence indices linked only by time
    # proximity. Kept out of the union structure on purpose (see below).
    nearby_voice = {}

    # 1. same raw entity (device_key, or "channel:<ch>" for voice, which
    #    has no device_key column) -> same base group, regardless of time.
    #    Step 3 below re-splits this by gap_s, so a device's history
    #    spanning weeks doesn't collapse into one incident.
    by_entity = {}
    for i, o in enumerate(occ):
        if o["entity"] is not None:
            by_entity.setdefault(o["entity"], []).append(i)
    for idxs in by_entity.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)

    # 2. cross-lane aircraft identity: hex <-> acars tail, hex <-> voice
    #    (airband, within gap_s of one of that hex's own position reports).
    hexes = sorted({o["entity"] for o in occ
                     if o["kind"] == "adsb" and o["entity"]})
    for h in hexes:
        adsb_idxs = [i for i, o in enumerate(occ)
                      if o["kind"] == "adsb" and o["entity"] == h]
        if not adsb_idxs:
            continue
        anchor = adsb_idxs[0]

        tail = hex_to_tail(conn, h)
        if tail:
            acars_key = "acars/%s" % tail
            for i, o in enumerate(occ):
                if o["kind"] == "acars" and o["entity"] == acars_key:
                    uf.union(anchor, i)

        # Time proximity is INFERENCE. It attaches voice to this aircraft's
        # incident but must never union two occurrences, because several
        # aircraft can be near the same transmission and union-find would then
        # merge them transitively -- which collapsed an entire day into one
        # 27,965-event "incident" on live data.
        pos_ts = [t for t in (
            _parse_ts(r[0]) for r in conn.execute(
                "SELECT ts FROM aircraft_positions WHERE hex = ? ORDER BY ts", (h,)
            ).fetchall()
        ) if t is not None]
        if pos_ts:
            for i, o in enumerate(occ):
                if o["source"] != "voice" or not _is_airband(o["freq_mhz"]):
                    continue
                if any(abs((o["dt"] - p).total_seconds()) <= gap_s for p in pos_ts):
                    nearby_voice.setdefault(anchor, set()).add(i)

    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    # 3. within each entity-linked group, split on gaps > gap_s.
    incidents_idxs = []
    for idxs in groups.values():
        idxs.sort(key=lambda i: occ[i]["dt"])
        chunk = [idxs[0]]
        for i in idxs[1:]:
            if (occ[i]["dt"] - occ[chunk[-1]]["dt"]).total_seconds() > gap_s:
                incidents_idxs.append(chunk)
                chunk = [i]
            else:
                chunk.append(i)
        incidents_idxs.append(chunk)

    # Attach proximity-linked voice to whichever chunk actually spans it. This
    # adds voice to an aircraft's incident without ever having merged aircraft.
    for anchor, voice_idxs in nearby_voice.items():
        for chunk in incidents_idxs:
            if anchor not in chunk:
                continue
            lo = min(occ[i]["dt"] for i in chunk)
            hi = max(occ[i]["dt"] for i in chunk)
            for vi in voice_idxs:
                if vi in chunk:
                    continue
                d = occ[vi]["dt"]
                if (lo - d).total_seconds() <= gap_s and \
                   (d - hi).total_seconds() <= gap_s:
                    chunk.append(vi)

    out = []
    for chunk in incidents_idxs:
        if len(chunk) < min_events:
            continue
        kinds = sorted({occ[i]["kind"] for i in chunk})
        entities = sorted({occ[i]["entity"] for i in chunk if occ[i]["entity"]})
        events_ids = sorted("%s:%s" % (occ[i]["source"], occ[i]["id"]) for i in chunk)
        start_dt = min(occ[i]["dt"] for i in chunk)
        end_dt = max(occ[i]["dt"] for i in chunk)
        lane_diversity = len(kinds)
        score = (_LANE_DIVERSITY_WEIGHT * (lane_diversity ** 2)
                 + _EVENT_COUNT_WEIGHT * math.log2(len(chunk) + 1))
        out.append({
            "start": start_dt.isoformat(), "end": end_dt.isoformat(),
            "kinds": kinds, "entities": entities, "events": events_ids,
            "score": round(score, 4),
        })

    out.sort(key=lambda inc: inc["start"])
    return out


def store_incidents(conn, incidents):
    """Persist find_incidents()'s output into a new `incidents` table
    (CREATE IF NOT EXISTS -- existing tables are never touched).

    Replaces the stored set on every call: incidents are always
    recomputed fresh from find_incidents(), so keeping old rows around
    would just accumulate stale duplicates of the same clusters under a
    different gap_s/min_events run. Returns the number of rows stored.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          start TEXT NOT NULL,
          end TEXT NOT NULL,
          kinds TEXT NOT NULL,
          entities TEXT NOT NULL,
          events TEXT NOT NULL,
          score REAL NOT NULL
        )
    """)
    conn.execute("DELETE FROM incidents")
    conn.executemany(
        "INSERT INTO incidents(start, end, kinds, entities, events, score) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(i["start"], i["end"], json.dumps(i["kinds"]), json.dumps(i["entities"]),
          json.dumps(i["events"]), i["score"]) for i in incidents],
    )
    conn.commit()
    return len(incidents)
