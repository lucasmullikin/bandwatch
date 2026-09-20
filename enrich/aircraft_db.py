"""
aircraft_db.py -- offline ICAO-hex enrichment for the SDR monitor.

Turns a bare ICAO 24-bit hex address (e.g. "A4E3FC") into a registration,
type, and (for a curated subset) an operator/category/tags. Two public
datasets are downloaded once via download(), cached on disk, and loaded
into flat in-memory dicts via load() so that lookup(hex) / notable(hex)
are O(1) with no network or file I/O per call.

DATA SOURCE 1: wiedehopf/tar1090-db (full registry, hex -> reg/type)
----------------------------------------------------------------------
Raw files live at:
    https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/<PREFIX>.js
There are ~75 shard files (confirmed via db/files.js, which lists every
valid prefix). Investigated with curl+gzip by hand before writing this
parser -- do not trust the ".js" extension:

  * Each db/<PREFIX>.js file's raw bytes ARE a gzip stream (confirmed via
    `curl -sI`: no Content-Encoding header, and content-length equals the
    compressed size -- this is the git blob itself, not HTTP transport
    compression). It must be gzip-decompressed after download, every time,
    regardless of what curl/requests negotiate over the wire.
  * Decompressed, each shard is a JSON object: {"<suffix>": [registration,
    icao_type, flags, long_description], ..., "children": [<sub-prefix>, ...]}
    The "children" key (when present) is shard-tree metadata, not an
    aircraft entry, and must be skipped.
  * There is no fixed prefix length. Sparse prefixes are a single hex
    digit ("0".."F"); busy ones (e.g. "A0", most of the US N-number
    range) are split further into their own shards ("A00", "A02", ...).
    The full 6-hex-digit ICAO address is simply prefix + suffix, and each
    address lives in exactly one shard (split children are fully removed
    from their parent, verified by direct inspection -- no overlap, no
    double counting).
  * The published shard array is [registration, icao_type, flags,
    long_description] -- NOT [registration, icao_type, operator] as the
    original interface sketch assumed. There is no operator field in this
    data source. (tar1090-db does carry owner/operator internally and
    publishes it in a *separate* aircraft.csv.gz on the repo's "csv"
    branch, not under db/, which is out of scope here -- see lookup()'s
    docstring.) "flags" (military / LADD bit string) is parsed but not
    currently exposed, since the required interface doesn't ask for it.
  * As of 2026-08-31: 75 shards, ~617k raw entries, ~566k with any actual
    data (the rest are placeholder PIA/anonymized-address rows with every
    field null) -- those empty rows are dropped at load() time rather
    than stored, since they'd never produce a useful lookup() result.

DATA SOURCE 2: sdr-enthusiasts/plane-alert-db (curated notables, CSV)
----------------------------------------------------------------------
    https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-db.csv
Confirmed via curl: plain UTF-8 CSV (no BOM, not gzipped), 17,223 data
rows, header exactly:
    $ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link
No duplicate $ICAO values (checked directly), so a flat dict keyed by
hex is safe -- no last-write-wins ambiguity to worry about.

MEMORY
----------------------------------------------------------------------
The merged tar1090 registry is stored as hex -> (registration, type,
description) 3-tuples (not per-entry dicts -- a dict per entry would cost
several times more overhead per record for no benefit here). Measured
directly: ~566k entries, ~23MB pickled, roughly 100-150MB resident as
live Python objects. A *fresh* download()+load() pass peaks around
~220MB RSS transiently (parsing 75 shard files' JSON one at a time --
CPython doesn't always hand freed parse buffers back to the OS
immediately); that transient peak, not the ~100-150MB steady state, is
the number to budget headroom for. plane-alert-db is trivial by
comparison (~17k rows, a few MB). If this project ever needs a smaller
footprint, the fix is to only load() the shard files actually seen in
traffic rather than the whole registry -- not implemented here, since
the collector wants simple O(1) lookups and simplicity was prioritized
over a footprint we don't yet know is a problem.

FAILURE MODE
----------------------------------------------------------------------
Enrichment is best-effort by design: a missing dest_dir, a missing or
corrupt shard, a missing plane-alert CSV, a malformed/empty hex -- none
of it raises. lookup()/notable() always return either a dict or None.
An enrichment miss must never break event ingest.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import urllib.error
import urllib.request

TAR1090_BASE = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db"
PLANE_ALERT_URL = (
    "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-db.csv"
)

_TAR1090_SUBDIR = "tar1090"
_FILES_INDEX_NAME = "files.json"
_PLANE_ALERT_FILENAME = "plane_alert.csv"

_HEX_RE = re.compile(r"^[0-9A-F]{6}$")

_USER_AGENT = "bandwatch-aircraft-db/1.0"
_FETCH_TIMEOUT = 15

# In-memory state populated by load(). Empty until load() is called (or if
# load() found nothing to load), so lookup()/notable() are always safe to
# call -- they just return None until real data is loaded.
_registry: dict[str, tuple] = {}
_notable: dict[str, dict] = {}


def _normalize_hex(hexid):
    """Uppercase + strip; return None for anything that isn't a clean
    6-hex-digit ICAO address, rather than let a bad value reach a dict
    lookup and (best case) silently miss or (worst case) raise."""
    if not isinstance(hexid, str):
        return None
    h = hexid.strip().upper()
    if not _HEX_RE.match(h):
        return None
    return h


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        return resp.read()


def download(dest_dir):
    """Fetch/refresh both datasets into dest_dir. Best-effort per file:
    one bad shard or a plane-alert fetch failure is recorded in the
    returned counts and does not stop the rest of the download. Only a
    local filesystem problem (e.g. can't create dest_dir) raises -- that's
    a caller configuration error, not a transient network hiccup.

    Returns a dict of counts, e.g.:
        {
          "tar1090_shards_total": 75, "tar1090_shards_ok": 75,
          "tar1090_shards_failed": 0, "plane_alert_ok": True,
          "plane_alert_rows": 17223, "errors": [],
        }
    """
    counts = {
        "tar1090_shards_total": 0,
        "tar1090_shards_ok": 0,
        "tar1090_shards_failed": 0,
        "plane_alert_ok": False,
        "plane_alert_rows": 0,
        "errors": [],
    }

    tar_dir = os.path.join(dest_dir, _TAR1090_SUBDIR)
    os.makedirs(tar_dir, exist_ok=True)

    prefixes = []
    try:
        raw = _fetch(f"{TAR1090_BASE}/files.js")
        prefixes = json.loads(gzip.decompress(raw))
        with open(os.path.join(tar_dir, _FILES_INDEX_NAME), "w", encoding="utf-8") as f:
            json.dump(prefixes, f)
    except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
        counts["errors"].append(f"files.js: {exc!r}")

    counts["tar1090_shards_total"] = len(prefixes)

    for prefix in prefixes:
        try:
            raw = _fetch(f"{TAR1090_BASE}/{prefix}.js")
            data = json.loads(gzip.decompress(raw))
            with open(os.path.join(tar_dir, f"{prefix}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
            counts["tar1090_shards_ok"] += 1
        except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
            counts["tar1090_shards_failed"] += 1
            counts["errors"].append(f"{prefix}.js: {exc!r}")

    try:
        raw = _fetch(PLANE_ALERT_URL)
        text = raw.decode("utf-8")
        with open(os.path.join(dest_dir, _PLANE_ALERT_FILENAME), "w", encoding="utf-8") as f:
            f.write(text)
        counts["plane_alert_rows"] = max(0, text.count("\n") - 1)
        counts["plane_alert_ok"] = True
    except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
        counts["errors"].append(f"plane-alert-db.csv: {exc!r}")

    return counts


def load(dest_dir):
    """Parse whatever download() cached under dest_dir into the flat
    in-memory dicts lookup()/notable() read from. Never raises: a missing
    dest_dir, a missing tar1090 index, a missing/corrupt shard file, or a
    missing/corrupt plane-alert CSV all just degrade to "nothing loaded
    for that part" rather than an exception. Safe to call more than once
    (e.g. after a fresh download()) -- each call fully replaces the
    previous in-memory state.

    Returns a dict of what actually ended up loaded, e.g.:
        {"tar1090_entries": 565768, "plane_alert_entries": 17223}
    """
    global _registry, _notable

    registry: dict[str, tuple] = {}
    tar_dir = os.path.join(dest_dir, _TAR1090_SUBDIR)
    prefixes = []
    try:
        with open(os.path.join(tar_dir, _FILES_INDEX_NAME), encoding="utf-8") as f:
            prefixes = json.load(f)
    except (OSError, ValueError):
        prefixes = []

    for prefix in prefixes:
        try:
            with open(os.path.join(tar_dir, f"{prefix}.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for suffix, entry in data.items():
            if suffix == "children":
                continue
            try:
                reg, typ, _flags, desc = entry
            except (TypeError, ValueError):
                continue
            if not (reg or typ or desc):
                continue  # placeholder/PIA row, nothing worth storing
            registry[prefix + suffix] = (reg, typ, desc)

    notable: dict[str, dict] = {}
    pa_path = os.path.join(dest_dir, _PLANE_ALERT_FILENAME)
    try:
        with open(pa_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                hexid = _normalize_hex(row.get("$ICAO"))
                if hexid is None:
                    continue
                tags = [
                    t.strip()
                    for t in (row.get("$Tag 1"), row.get("$#Tag 2"), row.get("$#Tag 3"))
                    if t and t.strip()
                ]
                notable[hexid] = {
                    "registration": (row.get("$Registration") or "").strip() or None,
                    "operator": (row.get("$Operator") or "").strip() or None,
                    "type": (row.get("$Type") or "").strip() or None,
                    "category": (row.get("Category") or "").strip() or None,
                    "tags": tags,
                    "mil_civ": (row.get("#CMPG") or "").strip() or None,
                    "link": (row.get("$#Link") or "").strip() or None,
                }
    except (OSError, csv.Error):
        pass

    _registry = registry
    _notable = notable
    return {"tar1090_entries": len(registry), "plane_alert_entries": len(notable)}


def lookup(hexid):
    """{"registration", "type", "operator"} for hexid, or None if it's
    unknown, malformed, or nothing has been load()ed yet.

    "type" prefers the long description ("BELL 407") over the bare ICAO
    type code ("B407") when both are present, since the point is to be
    meaningful to a human, not just correct.

    "operator" is always None: tar1090-db's published db/<PREFIX>.js
    shards (the source this module reads, per the task spec) don't carry
    an operator field at all -- see the module docstring. The key is
    still always present so callers can rely on the shape without
    special-casing a missing key.
    """
    h = _normalize_hex(hexid)
    if h is None:
        return None
    entry = _registry.get(h)
    if entry is None:
        return None
    reg, typ, desc = entry
    return {"registration": reg, "type": desc or typ, "operator": None}


def notable(hexid):
    """{"registration","operator","type","category","tags","mil_civ","link"}
    for hexid if it's in the curated plane-alert-db notables list, else
    None (including for anything malformed/empty, or if nothing has been
    load()ed yet)."""
    h = _normalize_hex(hexid)
    if h is None:
        return None
    entry = _notable.get(h)
    if entry is None:
        return None
    return dict(entry)  # defensive copy -- callers must not mutate our cache
