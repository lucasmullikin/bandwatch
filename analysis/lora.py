"""T17 -- detect LoRa/MeshCore activity at 915 MHz. Detection, never decode.

Be clear about what this is, because the difference matters:

  DECODING LoRa with an RTL-SDR is not practical. LoRa is chirp spread
  spectrum; demodulating it needs coherent processing of a signal that is
  usually BELOW the noise floor, which is the whole point of the modulation.
  There is no rtl_433 equivalent for it. If you want to read MeshCore traffic
  you need a LoRa radio, which is what the node on order is for.

  DETECTING it is entirely practical, because a LoRa transmission occupies a
  characteristic 125, 250 or 500 kHz of bandwidth for tens to hundreds of
  milliseconds. That is a wide, flat plateau in a power sweep, and it looks
  nothing like the narrow spikes the 902-928 ISM band is otherwise full of.

So this answers "is there mesh/LoRa traffic near me, when, and on which
channel" -- which is the useful question before a node arrives, and remains
useful afterwards as an independent check on what the node reports hearing.

The discriminator is WIDTH, not power. A gas meter (SCMplus, already decoded
here at 23-32 dB SNR), a tyre sensor and a doorbell are all narrowband: tens of
kHz. Nothing else in this band routinely occupies a quarter of a megahertz.
"""
import math

# LoRa channel bandwidths in use on the US 902-928 ISM band, in Hz.
LORA_BANDWIDTHS_HZ = (125_000, 250_000, 500_000)
# A detected plateau may be off by roughly one sweep bin at each edge (the
# survey uses 12.5 kHz bins, so ~25 kHz total). 20% covers that with room to
# spare and, critically, leaves GAPS between the three LoRa bandwidths:
#   125 kHz -> 100-150    250 kHz -> 200-300    500 kHz -> 400-600
# At the 40% this started with, the three ranges overlapped into one continuous
# 75-700 kHz band and the "not LoRa" branch was effectively unreachable -- a
# classifier that can only say yes is not a classifier. A 180 kHz signal now
# correctly falls in the gap and is rejected.
WIDTH_TOLERANCE = 0.20
# narrower than this and it is an ordinary ISM device, not LoRa
MIN_PLATEAU_HZ = 80_000
# wider than this and it is not a LoRa channel either -- more likely a pager
# transmitter, an image, or front-end overload smearing across the sweep
MAX_PLATEAU_HZ = 800_000
# a plateau must stand this far above the band's own median
MIN_SNR_DB = 6.0
# MeshCore's default US channel. Present so a hit can be named rather than
# just located; it is NOT used to filter, because a mesh may be anywhere.
MESHCORE_DEFAULT_HZ = 906_875_000
MESHCORE_TOLERANCE_HZ = 300_000


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def find_plateaus(points, min_snr=MIN_SNR_DB):
    """Contiguous runs of bins above the band's own median.

    The floor is the median, not the minimum: a single dead bin would drag a
    minimum-based floor down and make the whole sweep look occupied.
    """
    if not points or len(points) < 3:
        return []
    pts = sorted(points)
    floor = _median([d for _, d in pts])
    step = _median([pts[i + 1][0] - pts[i][0] for i in range(len(pts) - 1)]) or 0
    if step <= 0:
        return []

    out = []
    run = []
    for f, d in pts:
        hot = (d - floor) >= min_snr
        if hot:
            # a gap larger than one bin ends the run: two separate signals must
            # not be merged into one implausibly wide plateau
            if run and f - run[-1][0] > step * 1.6:
                out.append(run)
                run = []
            run.append((f, d))
        elif run:
            out.append(run)
            run = []
    if run:
        out.append(run)

    plateaus = []
    for r in out:
        if len(r) < 2:
            continue
        width = (r[-1][0] - r[0][0]) + step
        centre = (r[0][0] + r[-1][0]) / 2.0
        peak = max(d for _, d in r)
        plateaus.append({
            "centre_hz": centre,
            "width_hz": width,
            "bins": len(r),
            "peak_snr_db": round(peak - floor, 1),
        })
    return plateaus


def classify_plateau(p):
    """Which LoRa bandwidth, if any, this plateau is consistent with.

    Returns (bandwidth_hz or None, reason). A plateau that matches no LoRa
    bandwidth is reported as such rather than being forced into the nearest
    one -- most energy in this band is not LoRa and saying so is the point.
    """
    w = p["width_hz"]
    if w < MIN_PLATEAU_HZ:
        return None, ("narrowband (%.0f kHz) -- an ordinary ISM device, "
                      "not LoRa" % (w / 1000.0))
    if w > MAX_PLATEAU_HZ:
        return None, ("too wide (%.0f kHz) for a LoRa channel -- suspect a "
                      "pager transmitter, an image, or front-end overload"
                      % (w / 1000.0))
    for bw in LORA_BANDWIDTHS_HZ:
        if abs(w - bw) <= bw * WIDTH_TOLERANCE:
            return bw, ("width %.0f kHz is consistent with a %d kHz LoRa "
                        "channel" % (w / 1000.0, bw // 1000))
    return None, ("width %.0f kHz matches no standard LoRa bandwidth "
                  "(125/250/500 kHz)" % (w / 1000.0))


def is_meshcore_default(centre_hz):
    return abs(centre_hz - MESHCORE_DEFAULT_HZ) <= MESHCORE_TOLERANCE_HZ


def detect(points, min_snr=MIN_SNR_DB):
    """Full pass over one sweep. Returns candidates and everything rejected.

    Rejections are returned, not discarded. A detector that only reports hits
    cannot be checked; being able to see what it threw away and why is what
    makes it possible to tell a quiet band from a broken detector.
    """
    candidates, rejected = [], []
    for p in find_plateaus(points, min_snr):
        bw, reason = classify_plateau(p)
        row = dict(p)
        row["reason"] = reason
        if bw:
            row["bandwidth_hz"] = bw
            row["meshcore_default_channel"] = is_meshcore_default(p["centre_hz"])
            candidates.append(row)
        else:
            rejected.append(row)
    candidates.sort(key=lambda r: -r["peak_snr_db"])
    return {
        "candidates": candidates,
        "rejected": rejected,
        "note": ("LoRa DETECTION only. These are transmissions whose occupied "
                 "bandwidth is consistent with LoRa; nothing here demodulates "
                 "them, and no claim is made about their contents, network or "
                 "sender. Decoding requires a LoRa radio."),
    }


def summarise(result):
    """One operator-facing line."""
    c = result.get("candidates") or []
    if not c:
        n = len(result.get("rejected") or [])
        return ("no LoRa-width activity in this sweep (%d other signal%s seen)"
                % (n, "" if n == 1 else "s"))
    best = c[0]
    return ("%d LoRa-width transmission%s; strongest %.4f MHz, %d kHz wide, "
            "+%.1f dB%s"
            % (len(c), "" if len(c) == 1 else "s",
               best["centre_hz"] / 1e6, int(best["width_hz"] / 1000),
               best["peak_snr_db"],
               " (MeshCore default channel)"
               if best.get("meshcore_default_channel") else ""))
