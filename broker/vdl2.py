"""Parse dumpvdl2 JSON into the shape the rest of the system already speaks.

VDL Mode 2 is the modern replacement for plain ACARS: same kinds of message
(position reports, OOOI, weather, free text, CPDLC) over a far higher-capacity
digital link, on 136.975 MHz and a handful of other channels.

The reason it earns its radio time here is not volume alone. Every VDL2 frame
carries the aircraft's ICAO address in the AVLC header -- the SAME identifier
ADS-B broadcasts. Plain ACARS gives a tail number, which has to be matched to
a hex through a registry lookup that can be wrong. VDL2 gives the hex
directly, so a text message joins to a tracked aircraft on a HEX MATCH rather
than on inference. In the correlation module's own vocabulary that is a fact,
not a time-proximity guess.

Two things this refuses to do:

* It never reports an aircraft address for a frame sent BY a ground station.
  Both ends appear in the AVLC header and taking the wrong one would attribute
  a controller's message to an aeroplane.
* It never treats an empty or missing text field as an empty message. A frame
  with no ACARS payload is a link-layer frame and is stored as such, because
  "aircraft sent a blank message" is a different claim from "this frame had no
  message in it".
"""
import json

# dumpvdl2 labels the two ends of every frame. Only one of them is an aircraft.
AIRCRAFT_TYPES = {"Aircraft"}


def _addr(node):
    if not isinstance(node, dict):
        return None, None
    return node.get("addr"), node.get("type")


def parse_line(line):
    """One dumpvdl2 JSON line -> a normalised dict, or None if not a frame.

    Returns keys the collector already understands: device_key, summary, plus
    the fields that make a VDL2 frame worth more than a plain ACARS one.
    """
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    v = d.get("vdl2")
    if not isinstance(v, dict):
        return None

    avlc = v.get("avlc") or {}
    src_addr, src_type = _addr(avlc.get("src"))
    dst_addr, dst_type = _addr(avlc.get("dst"))

    # the aircraft may be either end; a ground station is never one
    icao, direction = None, None
    if src_type in AIRCRAFT_TYPES and src_addr:
        icao, direction = src_addr, "downlink"      # from the aircraft
    elif dst_type in AIRCRAFT_TYPES and dst_addr:
        icao, direction = dst_addr, "uplink"        # to the aircraft

    acars = avlc.get("acars") or {}
    reg = (acars.get("reg") or "").strip() or None
    flight = (acars.get("flight") or "").strip() or None
    label = (acars.get("label") or "").strip() or None
    # an ABSENT payload is not an empty message
    text = acars.get("msg_text")
    # an all-whitespace payload is not a message. Strip, then collapse the
    # empty result to None so "no text" has ONE representation rather than
    # two that behave differently downstream.
    text = (text.strip() or None) if isinstance(text, str) else None

    ts = None
    t = v.get("t") or {}
    if isinstance(t, dict) and t.get("sec"):
        ts = int(t["sec"])

    # device_key: prefer the ICAO hex, because that is what ADS-B keys on and
    # what makes this joinable. Fall back to the registration, then the
    # ground station, so a frame is never silently dropped for lacking a hex.
    if icao:
        key = icao.upper()
    elif reg:
        key = "vdl2/%s" % reg
    elif src_addr:
        key = "vdl2/gs-%s" % src_addr
    else:
        return None

    bits = []
    if flight:
        bits.append(flight)
    if reg and reg != flight:
        bits.append(reg)
    if label:
        bits.append("label %s" % label)
    if text:
        bits.append(text[:120])
    elif not acars:
        bits.append(avlc.get("frame_type") or "link frame")
    summary = " ".join(bits) if bits else (avlc.get("frame_type") or "VDL2")

    return {
        "device_key": key,
        "icao": icao.upper() if icao else None,
        "registration": reg,
        "flight": flight,
        "label": label,
        "text": text,
        "direction": direction,
        "freq_hz": v.get("freq"),
        "sig_level": v.get("sig_level"),
        "noise_level": v.get("noise_level"),
        "ts_unix": ts,
        "summary": summary[:200],
        "has_text": bool(text),
        "raw": line.strip(),
    }


def parse_stream(lines):
    """Parse many lines, skipping anything that is not a frame."""
    out = []
    for ln in lines:
        r = parse_line(ln)
        if r:
            out.append(r)
    return out


def summarise(rows):
    """One operator-facing line about a batch."""
    if not rows:
        return "no VDL2 frames"
    with_text = sum(1 for r in rows if r["has_text"])
    hexes = {r["icao"] for r in rows if r["icao"]}
    return ("%d VDL2 frames, %d with message text, %d distinct aircraft "
            "(by ICAO hex, joinable to ADS-B)"
            % (len(rows), with_text, len(hexes)))
