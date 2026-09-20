#!/usr/bin/env python3
"""Export filtered views of the SDR database, WITH provenance (T14).

docs/COVERAGE.md sets the standard of proof for this project: a transcript
is a lead, never a fact, on its own; coverage is not continuous because
lanes rotate; and a claim about what a model heard is only as good as
knowing which model produced it. A CSV/JSON dump that drops those facts on
the floor lets a reader treat instrument output as settled fact. So every
bundle this module writes carries a manifest that restates them explicitly,
next to a sha256 of every file so a reader can prove what they have matches
what was exported.

Pure stdlib + sqlite3. Read-only against events/voice/coverage -- this
module never writes to the database.

Run the tests: python3 analysis/test_export_policy.py -v
"""
import csv
import hashlib
import json
import os
from datetime import datetime, timezone

# --- schema knowledge ---------------------------------------------------
#
# There is no schema-version marker anywhere in the database itself (no row
# in `meta`, no version table) -- collector.py's MIGRATIONS list just adds
# columns to `voice` at connect time if they're missing. SCHEMA_VERSION
# below is this module's OWN declared understanding of the column set it
# reads, not a value read from the DB. Bump it if collector.py's SCHEMA or
# MIGRATIONS change a column this module depends on.
SCHEMA_VERSION = 1

EVENTS_COLUMNS = ["id", "ts", "lane", "kind", "device_key", "summary", "raw", "preserved"]

# Every column collector.py's MIGRATIONS list adds to voice, in the order
# they were introduced, is included here on purpose: sha256/model/
# transcribed_by/rejected_because are exactly the fields a reader needs to
# tell an accurate transcript from a low-confidence or rejected one.
VOICE_COLUMNS = [
    "id", "ts", "channel", "freq_mhz", "audio_path", "duration_s",
    "transcript", "transcribed_at", "watchlist_hit", "preserved",
    "preserved_at", "sha256", "transcribed_by", "model",
    "no_speech_prob", "avg_logprob", "compression_ratio", "rejected_because",
]

COVERAGE_COLUMNS = ["id", "device", "lane", "started_at", "ended_at", "events"]

_VALID_FORMATS = ("csv", "json")

# --- limitations, restated from docs/COVERAGE.md -------------------------
#
# This is the block export_bundle() writes into manifest.json. Every entry
# states something COVERAGE.md requires a reader to know before treating
# anything in this export as more than a lead. Wording paraphrases that
# document; nothing here is invented.
LIMITATIONS = {
    "transcript_is_a_lead_not_a_fact": (
        "A transcript in voice.transcript is a lead, never a fact, on its "
        "own. In this deployment Whisper rendered channel noise and a weak "
        "carrier as confident, fabricated sentences (e.g. \"Thank you.\"). "
        "voice.rejected_because records why a transcript failed the "
        "automatic hallucination filter, but that filter only catches "
        "recognisable artefacts -- it cannot catch a plausible-sounding "
        "misheard sentence. Establishing 'someone said X' requires the "
        "retained audio, a human listener, and a transcript that matches "
        "what a person actually hears; ASR output alone is never sufficient."
    ),
    "transcription_model_varies_per_row": (
        "There is no single model or transcriber for this export. Check "
        "voice.model and voice.transcribed_by on each row individually -- "
        "different rows in the same export may have been produced by "
        "different models, or re-transcribed by a human."
    ),
    "freq_mhz_is_reconstructed_not_measured": (
        "voice.freq_mhz was not recorded by the receiver at capture time. "
        "It is backfilled after the fact by matching voice.channel against "
        "the frequency currently assigned to that label in the rtl_airband "
        "configs (broker/chanmap.py). If a channel's assigned frequency is "
        "ever changed, older rows silently inherit whatever mapping was in "
        "place when the backfill ran, not necessarily what the receiver "
        "was actually tuned to when that row was captured. A NULL "
        "freq_mhz means the channel label matched no known frequency and "
        "was deliberately left unknown, not that nothing was heard."
    ),
    "coverage_is_not_continuous": (
        "coverage.csv/coverage.json is not proof of continuous listening. "
        "Only the pinned FRS radio never rotates; every other surveyed lane "
        "is sampled, so gaps between coverage rows are real gaps in what "
        "was heard, not just gaps in what was interesting. Absence of an "
        "event in a time window does not mean nothing happened there -- it "
        "may mean that lane was not being monitored at the time."
    ),
    "voice_identity_unavailable": (
        "Nothing in this export identifies a speaker. FRS/GMRS carry no "
        "identity; a voice is not an identifier, and 'Person P said X' is "
        "not supportable from this data alone."
    ),
    "watchlist_matched_after_transcription": (
        "voice.watchlist_hit is a post-transcription text match. Watchlist "
        "terms are never given to Whisper as a prompt, so a hit here "
        "reflects what the transcript contains, with all of that "
        "transcript's own unreliability -- it is not a separately verified "
        "detection."
    ),
    "receiving_is_not_republishing": (
        "This export is monitoring data, not a publication. Receiving RF "
        "in Idaho is lawful, but divulging the contents of some intercepted "
        "communications is a separate legal question this export does not "
        "settle -- take advice before publishing transmission contents."
    ),
}


def _check_fmt(fmt):
    if fmt not in _VALID_FORMATS:
        raise ValueError("fmt must be one of %r, got %r" % (_VALID_FORMATS, fmt))


def _open_out(out, newline):
    """Accept either a path (str/PathLike) or an already-open writable
    file object, so tests can target an io.StringIO() without touching
    disk. Returns (fileobj, owns_it) -- callers only close what they opened.
    """
    if hasattr(out, "write"):
        return out, False
    return open(out, "w", newline=newline, encoding="utf-8"), True


def _write_csv(rows, columns, out):
    f, owns = _open_out(out, newline="")
    try:
        w = csv.writer(f)
        w.writerow(columns)
        for row in rows:
            w.writerow(row)
    finally:
        if owns:
            f.close()
    return len(rows)


def _write_json(rows, columns, out):
    f, owns = _open_out(out, newline=None)
    try:
        records = [dict(zip(columns, row)) for row in rows]
        json.dump(records, f, indent=2, default=str)
        f.write("\n")
    finally:
        if owns:
            f.close()
    return len(rows)


def _write_rows(rows, columns, out, fmt):
    if fmt == "csv":
        return _write_csv(rows, columns, out)
    return _write_json(rows, columns, out)


def _select(conn, table, columns, ts_col, extra_where, extra_params, since, until, order_by):
    where = list(extra_where)
    params = list(extra_params)
    if since is not None:
        where.append("%s >= ?" % ts_col)
        params.append(since)
    if until is not None:
        where.append("%s <= ?" % ts_col)
        params.append(until)
    sql = "SELECT %s FROM %s" % (", ".join(columns), table)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY %s" % order_by
    return conn.execute(sql, params).fetchall()


def export_events(conn, out, fmt="csv", kind=None, since=None, until=None):
    """Export the events table, optionally filtered by kind and/or a ts
    window (inclusive). Returns the number of rows written.
    """
    _check_fmt(fmt)
    extra_where, extra_params = [], []
    if kind is not None:
        extra_where.append("kind = ?")
        extra_params.append(kind)
    rows = _select(conn, "events", EVENTS_COLUMNS, "ts", extra_where, extra_params,
                    since, until, "ts")
    return _write_rows(rows, EVENTS_COLUMNS, out, fmt)


def export_voice(conn, out, fmt="csv", since=None, until=None):
    """Export the voice table, with the provenance columns a reader needs
    to tell an accurate transcript from a low-confidence or rejected one
    (sha256, model, transcribed_by, rejected_because). Returns the number
    of rows written.
    """
    _check_fmt(fmt)
    rows = _select(conn, "voice", VOICE_COLUMNS, "ts", [], [], since, until, "ts")
    return _write_rows(rows, VOICE_COLUMNS, out, fmt)


def export_coverage(conn, out, fmt="csv", since=None, until=None):
    """Export the coverage table, filtered on started_at. Returns the
    number of rows written.
    """
    _check_fmt(fmt)
    rows = _select(conn, "coverage", COVERAGE_COLUMNS, "started_at", [], [],
                    since, until, "started_at")
    return _write_rows(rows, COVERAGE_COLUMNS, out, fmt)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _observed_range(conn, table, ts_col, since, until):
    """MIN/MAX of ts_col over the same filter window used for the export,
    so the manifest's observed_range reflects what was actually written,
    not just what was asked for. table/ts_col are internal constants, never
    caller input, so building the SQL string is safe here.
    """
    where, params = [], []
    if since is not None:
        where.append("%s >= ?" % ts_col)
        params.append(since)
    if until is not None:
        where.append("%s <= ?" % ts_col)
        params.append(until)
    sql = "SELECT MIN(%s), MAX(%s) FROM %s" % (ts_col, ts_col, table)
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = conn.execute(sql, params).fetchone()
    return (row[0], row[1]) if row else (None, None)


def export_bundle(conn, out_dir, since=None, until=None, fmt="csv"):
    """Write events/voice/coverage plus manifest.json into out_dir.

    manifest.json carries: row counts, the requested and observed time
    range, a sha256 of each written file, the schema version, and the
    limitations block above. Returns the manifest dict (also written to
    disk as manifest.json).
    """
    _check_fmt(fmt)
    os.makedirs(out_dir, exist_ok=True)

    events_path = os.path.join(out_dir, "events.%s" % fmt)
    voice_path = os.path.join(out_dir, "voice.%s" % fmt)
    coverage_path = os.path.join(out_dir, "coverage.%s" % fmt)

    events_rows = export_events(conn, events_path, fmt=fmt, since=since, until=until)
    voice_rows = export_voice(conn, voice_path, fmt=fmt, since=since, until=until)
    coverage_rows = export_coverage(conn, coverage_path, fmt=fmt, since=since, until=until)

    files = {}
    for path, table, rows in (
        (events_path, "events", events_rows),
        (voice_path, "voice", voice_rows),
        (coverage_path, "coverage", coverage_rows),
    ):
        files[os.path.basename(path)] = {
            "table": table,
            "rows": rows,
            "sha256": _sha256_file(path),
        }

    ranges = [
        _observed_range(conn, "events", "ts", since, until),
        _observed_range(conn, "voice", "ts", since, until),
        _observed_range(conn, "coverage", "started_at", since, until),
    ]
    observed_values = [v for pair in ranges for v in pair if v is not None]

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "requested_range": {"since": since, "until": until},
        "observed_range": {
            "min_ts": min(observed_values) if observed_values else None,
            "max_ts": max(observed_values) if observed_values else None,
        },
        "files": files,
        "limitations": dict(LIMITATIONS),
    }

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    return manifest
