"""Parse dump978 JSON into the shape the rest of the system speaks.

UAT carries three different things on one frequency, and conflating them would
be a real error:

  ADS-B  an aircraft reporting its OWN position. Same standing as 1090 ADS-B.
  TIS-B  ground radar's view of an aircraft, REBROADCAST. The aircraft may
         have no transmitter at all. The position is the radar's opinion, not
         the aircraft's claim, and it is older by however long the ground
         system took to relay it.
  FIS-B  weather and advisory products uplinked from a ground station. Not an
         aircraft at all.

So a TIS-B track is evidence that something was somewhere, sourced from a
third party -- weaker than an aircraft's own broadcast and it must not be
stored as though it were the same thing.
"""
import json

# dump978 marks the payload type; these are the values that matter here.
ADSB_TYPES = {"adsb_icao", "adsb_other", "adsr_icao", "adsb"}
TISB_TYPES = {"tisb_icao", "tisb_other", "tisb_trackfile", "tisb"}


def _addr(d):
    a = d.get("address")
    if a is None:
        return None
    if isinstance(a, int):
        return "%06X" % a
    s = str(a).strip().upper().replace("0X", "")
    return s or None


def parse_line(line):
    """One dump978 JSON message -> normalised dict, or None."""
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None

    kind = (d.get("address_qualifier") or d.get("type") or "").lower()
    icao = _addr(d)

    # FIS-B / uplink: ground-station weather and advisories, no aircraft
    if d.get("uplink") or d.get("fisb") or kind in ("uplink", "fisb"):
        return {
            "device_key": "uat/fisb",
            "source": "FIS-B",
            "icao": None,
            "is_aircraft": False,
            "summary": "FIS-B ground uplink (weather / advisory products)",
            "raw": line.strip(),
        }

    if not icao:
        return None

    tisb = kind in TISB_TYPES or "tisb" in kind
    callsign = (d.get("callsign") or d.get("flight") or "").strip() or None
    alt = d.get("altitude") or d.get("pressure_altitude") or d.get("geo_altitude")
    gs = d.get("ground_speed") or d.get("speed")
    pos = d.get("position") or {}
    lat = pos.get("lat") if isinstance(pos, dict) else d.get("lat")
    lon = pos.get("lon") if isinstance(pos, dict) else d.get("lon")

    bits = []
    if callsign:
        bits.append(callsign)
    if alt is not None:
        bits.append("%sft" % alt)
    if gs is not None:
        bits.append("%skt" % gs)
    if tisb:
        # never let a rebroadcast read like the aircraft's own report
        bits.append("[TIS-B: ground radar rebroadcast, not the aircraft]")
    summary = " ".join(bits) if bits else ("TIS-B contact" if tisb else "UAT")

    return {
        "device_key": icao,
        "source": "TIS-B" if tisb else "ADS-B",
        "icao": icao,
        "is_aircraft": True,
        "tisb": tisb,
        "callsign": callsign,
        "altitude_ft": alt,
        "ground_speed_kt": gs,
        "lat": lat,
        "lon": lon,
        "summary": summary[:200],
        "raw": line.strip(),
    }


def parse_stream(lines):
    out = []
    for ln in lines:
        r = parse_line(ln)
        if r:
            out.append(r)
    return out


def summarise(rows):
    if not rows:
        return "no UAT messages"
    own = sum(1 for r in rows if r.get("source") == "ADS-B")
    tis = sum(1 for r in rows if r.get("source") == "TIS-B")
    fis = sum(1 for r in rows if r.get("source") == "FIS-B")
    craft = len({r["icao"] for r in rows if r.get("icao")})
    return ("%d UAT messages: %d own-broadcast, %d TIS-B rebroadcast, "
            "%d FIS-B uplinks, %d distinct aircraft"
            % (len(rows), own, tis, fis, craft))
