"""spectrum.py -- parse rtl_power CSV sweeps, find carriers, track new ones over time.

rtl_power writes one row per swept segment:

    date, time, Hz_low, Hz_high, Hz_step, samples, dB, dB, dB, ...

Each dB column after the first 6 fields is one frequency bin, starting at
Hz_low and spaced Hz_step apart (Hz_high is redundant with Hz_low + n*Hz_step
and is not used here -- this mirrors how rtl_power's own heatmap.py reads its
output). A run typically emits many rows: multiple segments per sweep, and
the sweep repeated over time.

IMPORTANT: rtl_power truncates its output file on every invocation. A CSV on
disk is one sweep, not history. History lives only in the sqlite `spectrum`
table that store() writes to -- that table is the only place "yesterday" is
remembered.
"""

import csv
import sqlite3
import statistics
import time


def parse(path):
    """Flatten an rtl_power CSV into a list of {"ts", "freq_hz", "db"} bins.

    Never raises. A missing/empty file yields []. A row needs at least the 6
    header fields (date, time, Hz_low, Hz_high, Hz_step, samples) plus one dB
    value to contribute anything; rows that don't parse are skipped whole. A
    non-numeric dB value inside an otherwise good row only drops that one
    bin, not the rest of the row.
    """
    bins = []
    try:
        fh = open(path, newline="")
    except OSError:
        return bins

    with fh:
        for row in csv.reader(fh):
            if len(row) < 7:
                continue
            try:
                hz_low = float(row[2])
                hz_step = float(row[4])
                ts = time.mktime(
                    time.strptime(f"{row[0].strip()} {row[1].strip()}", "%Y-%m-%d %H:%M:%S")
                )
            except (ValueError, IndexError, OverflowError):
                continue

            for i, raw in enumerate(row[6:]):
                try:
                    db = float(raw)
                except ValueError:
                    continue
                bins.append({"ts": ts, "freq_hz": int(round(hz_low + i * hz_step)), "db": db})

    return bins


def summarize(bins):
    """Return {"floor_db", "peak_db", "n_bins"} for a list of bins.

    floor_db is the MEDIAN, not the mean: a mean is dragged upward by strong
    carriers and would hide weak ones sitting just above the real noise floor.
    """
    if not bins:
        return {"floor_db": None, "peak_db": None, "n_bins": 0}
    dbs = [b["db"] for b in bins]
    return {
        "floor_db": statistics.median(dbs),
        "peak_db": max(dbs),
        "n_bins": len(dbs),
    }


def find_carriers(bins, min_over_floor=10.0, min_sep_hz=50000):
    """Return carrier peaks, strongest first.

    A bin is a carrier candidate if it sits at least min_over_floor dB above
    the sweep's median floor. Candidates are then clustered by proximity --
    consecutive candidates (by frequency) within min_sep_hz of each other are
    chained into the same cluster (a wide carrier's skirt produces several
    elevated bins in a row; they are one transmitter, not several) -- and
    each cluster collapses to its single strongest bin.
    """
    if not bins:
        return []

    floor_db = statistics.median(b["db"] for b in bins)
    candidates = sorted(
        (b for b in bins if b["db"] - floor_db >= min_over_floor),
        key=lambda b: b["freq_hz"],
    )
    if not candidates:
        return []

    clusters = [[candidates[0]]]
    for b in candidates[1:]:
        if b["freq_hz"] - clusters[-1][-1]["freq_hz"] <= min_sep_hz:
            clusters[-1].append(b)
        else:
            clusters.append([b])

    carriers = []
    for cluster in clusters:
        strongest = max(cluster, key=lambda b: b["db"])
        carriers.append(
            {
                "freq_hz": strongest["freq_hz"],
                "db": strongest["db"],
                "over_floor": strongest["db"] - floor_db,
            }
        )

    carriers.sort(key=lambda c: c["db"], reverse=True)
    return carriers


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spectrum (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            band TEXT NOT NULL,
            freq_hz INTEGER NOT NULL,
            db REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spectrum_band_ts ON spectrum(band, ts)")
    conn.commit()


def store(conn, band, bins, min_over_floor=10.0, min_sep_hz=50000):
    """Persist one sweep's carriers into sqlite. Creates the `spectrum` table
    if needed. Returns the number of rows inserted.

    STORAGE VOLUME DECISION: we store find_carriers() output, not raw bins.

    One milair sweep is 56,889 bins. Storing every bin from every sweep is
    not viable: even at a lazy one sweep/minute that is 56,889 * 1440 =
    ~81.9M rows/day (~30 billion rows/year) in a single sqlite file -- and
    almost all of it is noise we already summarized away.

    Storing only the de-duplicated carrier peaks instead: a busy band
    clusters down to on the order of 10-50 distinct carriers per sweep after
    find_carriers()'s min_sep_hz merge. At a generous 50 carriers/sweep and
    one sweep/minute that's 50 * 1440 = 72,000 rows/day (~26M/year) -- a
    >99.9% reduction versus raw bins, comfortably within sqlite's comfort
    zone, and it is exactly the data new_carriers() needs: it diffs carrier
    *lists*, never raw spectra. Anything below floor + min_over_floor is, by
    definition, the noise the median floor already characterizes.
    """
    _ensure_table(conn)
    carriers = find_carriers(bins, min_over_floor=min_over_floor, min_sep_hz=min_sep_hz)
    if not carriers:
        return 0
    ts = max(b["ts"] for b in bins)
    conn.executemany(
        "INSERT INTO spectrum (ts, band, freq_hz, db) VALUES (?, ?, ?, ?)",
        [(ts, band, c["freq_hz"], c["db"]) for c in carriers],
    )
    conn.commit()
    return len(carriers)


def new_carriers(conn, band, bins, history_days=3, min_over_floor=10.0, min_sep_hz=50000):
    """Carriers in `bins` that were NOT seen for this band in the last
    history_days. This is the actual product: "a transmitter appeared that
    was not here yesterday."

    "now" is taken from the sweep's own timestamps (max ts across bins), not
    wall-clock time, so this works against sweeps recorded at any point in
    the past, not just ones just captured. History rows at or after that
    "now" (e.g. this same sweep, if already store()d) are excluded so a
    sweep never counts as its own history.
    """
    _ensure_table(conn)
    current = find_carriers(bins, min_over_floor=min_over_floor, min_sep_hz=min_sep_hz)
    if not current:
        return []

    now_ts = max(b["ts"] for b in bins)
    cutoff = now_ts - history_days * 86400

    rows = conn.execute(
        "SELECT DISTINCT freq_hz FROM spectrum WHERE band = ? AND ts >= ? AND ts < ?",
        (band, cutoff, now_ts),
    ).fetchall()
    historical_freqs = [r[0] for r in rows]

    def seen_before(freq_hz):
        return any(abs(freq_hz - h) <= min_sep_hz for h in historical_freqs)

    return [c for c in current if not seen_before(c["freq_hz"])]
