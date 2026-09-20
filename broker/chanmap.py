"""Channel label -> frequency, parsed from the rtl_airband configs.

Why this exists: voice.freq_mhz has been NULL for every one of the 411 recorded
transmissions since the table was created. Nothing ever wrote it. Any feature
that joins voice to a frequency -- "what was active recently", a station badge,
a per-frequency occupancy view -- silently returns NOTHING rather than failing,
which reads exactly like a quiet band.

The airband configs are the authority: each channel block carries both the
label the recorder writes into the filename and the frequency it was tuned to.

One trap this deliberately avoids: `freq` is a substring of `centerfreq`.
Matching without a line anchor picks up the tuner centre and assigns every
channel in a block the same wrong frequency -- the DC-spike frequency, at that.
This project has already shipped that bug once in a config validator.
"""
import os
import re

# anchored at line start so `centerfreq = ...` cannot match
FREQ_RE = re.compile(r"^\s*freq\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;", re.M)
LABEL_RE = re.compile(r"^\s*label\s*=\s*\"([^\"]+)\"\s*;", re.M)
CENTER_RE = re.compile(r"^\s*centerfreq\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*;", re.M)


def parse_conf(text):
    """Return {label: freq_mhz} for one rtl_airband config.

    Pairs each freq with the next label that follows it, which is the layout
    rtl_airband requires inside a channel block. A freq with no following
    label is dropped rather than guessed at.
    """
    marks = []
    for m in FREQ_RE.finditer(text):
        marks.append((m.start(), "freq", float(m.group(1))))
    for m in LABEL_RE.finditer(text):
        marks.append((m.start(), "label", m.group(1)))
    marks.sort()

    out = {}
    pending = None
    for _, kind, val in marks:
        if kind == "freq":
            pending = val          # a second freq before any label discards the first
        else:
            if pending is not None:
                out[val] = pending
                pending = None
    return out


def centers(text):
    """Tuner centre frequencies in a config. Never a channel."""
    return [float(m.group(1)) for m in CENTER_RE.finditer(text)]


def load(conf_dir):
    """Merge every .conf in a directory into one label -> freq map."""
    out = {}
    if not os.path.isdir(conf_dir):
        return out
    for name in sorted(os.listdir(conf_dir)):
        if not name.endswith(".conf"):
            continue
        try:
            with open(os.path.join(conf_dir, name)) as fh:
                text = fh.read()
        except OSError:
            continue
        for label, freq in parse_conf(text).items():
            # first definition wins, and a conflict is worth knowing about
            if label in out and abs(out[label] - freq) > 1e-6:
                continue
            out[label] = freq
    return out


def backfill(con, conf_dir, table="voice", limit=None):
    """Stamp freq_mhz on rows that have a known channel but no frequency.

    Returns (updated, unmatched_labels). Rows whose channel is not in the map
    are LEFT NULL -- an unknown channel must stay unknown rather than take a
    neighbouring frequency.
    """
    cmap = load(conf_dir)
    if not cmap:
        return 0, []
    rows = con.execute(
        "SELECT id, channel FROM %s WHERE freq_mhz IS NULL AND channel IS NOT NULL"
        % table + (" LIMIT %d" % limit if limit else "")).fetchall()
    updates, unmatched = [], set()
    for rid, ch in rows:
        f = cmap.get(ch)
        if f is None:
            unmatched.add(ch)
            continue
        updates.append((f, rid))
    if updates:
        con.executemany("UPDATE %s SET freq_mhz=? WHERE id=?" % table, updates)
        con.commit()
    return len(updates), sorted(unmatched)
