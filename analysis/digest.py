#!/usr/bin/env python3
"""LLM digest over incidents (T12).

correlate.py's find_incidents() (T6) is deterministic DSP + set logic and
must keep working with every LLM host down -- the model is NEVER in the
ingest path. This module is a batch CONSUMER that runs after the fact: it
takes incidents that already exist, asks an injected model client to
describe them in words, and never feeds anything back into detection.

The "client" is any callable `client(prompt: str) -> str`. This module does
not import an HTTP client and does not know about the Studio (:8081, behind
a GPU arbiter lease) or the M4 (off limits for this project by operator
directive) -- wiring an actual model host to a `client` callable is the
caller's job (the webui "Models" toggle this ticket is for). A `client` may
optionally carry a `model_name` attribute (e.g. a functools.partial with
that attribute set, or any object implementing `__call__`); it is recorded
in `summarise()`'s output and is "unknown" otherwise.

STANDARD OF PROOF -- see docs/COVERAGE.md
----------------------------------------------------------------------
"A transcript is a lead. It is never, on its own, a fact." Every prompt
built here repeats that rule, because the model is never shown a verbatim
transcript (find_incidents()'s incident dict carries only "source:id"
event references, not event content) and must not be allowed to invent one.

correlate.py tags its joins with three evidence classes -- 'hex match'
(fact), 'tail match' (fact, second source), 'time+channel proximity'
(INFERENCE) -- and is explicit that a caller must always be able to tell a
hex match from a coincidence. An incident's "kinds" list is enough to
recover which of those classes are in play, because find_incidents() only
ever cross-links acars/voice into an aircraft's cluster via exactly those
mechanisms (see correlate.py's find_incidents() step 2): acars appearing
alongside adsb in one incident got there by tail match; voice appearing
alongside adsb got there by time+channel proximity. evidence_classes_for()
below is the single place that derives this, and both build_prompt() and
store_summaries() read it from there so the prompt and the stored record
never disagree about how strong a join was.

Pure stdlib + sqlite3. store_summaries() is the only write, and it only
touches a new `incident_summaries` table it creates.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# --- evidence classification --------------------------------------------

_EVIDENCE_EXPLAIN = {
    "hex match": (
        "hex match -- ADS-B rows keyed to the aircraft's own ICAO hex. "
        "Fact."
    ),
    "tail match": (
        "tail match -- an ACARS message whose parsed tail equals this "
        "aircraft's FAA registration. Fact, from a second independent "
        "source (not raw ADS-B)."
    ),
    "time+channel proximity": (
        "time+channel proximity -- a voice transmission on an airband "
        "channel near this aircraft's own position report in time. "
        "INFERENCE ONLY: two things being close in time does not prove "
        "they are the same thing."
    ),
    "same device/channel": (
        "same device/channel -- these rows share one raw identifier "
        "(device key or radio channel); no cross-lane join was made, so "
        "there is nothing here to infer beyond channel/device activity."
    ),
}


def evidence_classes_for(incident):
    """The correlate.py evidence classes present in one incident, derived
    from its "kinds" list (see module docstring for why this is safe:
    find_incidents() only ever cross-links acars/voice into an adsb
    cluster via tail match / time+channel proximity respectively).

    Always returns at least one class -- a single-lane incident (e.g. a
    burst of "ism" pings from one TPMS sensor) is still a real grouping,
    just not one of the three cross-lane join types.
    """
    kinds = set(incident.get("kinds") or [])
    classes = []
    if "adsb" in kinds:
        classes.append("hex match")
    if "acars" in kinds and "adsb" in kinds:
        classes.append("tail match")
    if "voice" in kinds and "adsb" in kinds:
        classes.append("time+channel proximity")
    if not classes:
        classes.append("same device/channel")
    return classes


# --- entity description ---------------------------------------------------


def _describe_entity(entity, identities):
    """Human-readable description of one incident["entities"] value.

    `identities` maps a bare device_key (an ICAO hex, per correlate.py's
    aircraft entity keys) to a dict shaped like
    correlate.aircraft_identities()'s return value: {"hex", "tail",
    "callsign", "label", "registration"}. Callers that have a `conn` build
    this dict themselves (e.g. {h: aircraft_identities(conn, h) for h in
    hexes}) -- this module never opens a connection to look one up, so a
    missing/omitted `identities` degrades gracefully to bare entity keys.
    """
    if entity.startswith("acars/"):
        return "ACARS tail %s" % entity[len("acars/"):]
    if entity.startswith("channel:"):
        return "voice channel %s" % entity[len("channel:"):]
    if entity.startswith("tpms/"):
        return (
            "device %s (TPMS tyre sensor -- per project policy this "
            "identifies a sensor, never a vehicle, owner, or person)"
            % entity[len("tpms/"):]
        )
    ident = (identities or {}).get(entity)
    if ident:
        bits = ["hex %s" % entity]
        if ident.get("tail"):
            bits.append("registration %s (FAA record, tail match)" % ident["tail"])
        if ident.get("callsign"):
            bits.append("callsign %s (self-reported, unverified)" % ident["callsign"])
        return ", ".join(bits)
    return "device %s (unidentified)" % entity


# --- prompt ----------------------------------------------------------------


def build_prompt(incident, identities=None):
    """A compact, factual prompt describing ONE incident.

    Carries the evidence class of every join in this incident (see
    evidence_classes_for()) so the model is always told which parts are
    inference, and always repeats the "transcript is a lead, not a fact"
    rule from docs/COVERAGE.md, even though no verbatim transcript text is
    ever included here (incident["events"] is only "source:id" refs).
    """
    identities = identities or {}
    kinds = incident.get("kinds") or []
    entities = incident.get("entities") or []
    events = incident.get("events") or []

    lines = [
        "You are drafting a short factual note about ONE radio/ADS-B "
        "monitoring incident for a human operator.",
        "Follow these rules exactly:",
        "- State only what the data below supports. Never invent a "
        "detail not present here.",
        "- A radio transcript, if one is mentioned, is a LEAD, never a "
        "fact -- do not present transcript content as confirmed, and "
        "never attribute it to a named person.",
        "- Never assert an identity that rests only on time proximity. "
        "Phrase those as 'heard near' or 'coincided with', never as "
        "'from' or 'by'.",
        "- If there is nothing safe to say, say so plainly; do not pad "
        "with a guess.",
        "",
        "INCIDENT DATA",
        "Window: %s to %s" % (incident.get("start"), incident.get("end")),
        "Lanes involved: %s" % (", ".join(kinds) if kinds else "none"),
        "Raw event count: %d" % len(events),
        "Score: %s" % incident.get("score"),
        "",
        "Entities:",
    ]
    if entities:
        for e in entities:
            lines.append("  - %s" % _describe_entity(e, identities))
    else:
        lines.append("  - none recorded")

    lines.append("")
    lines.append("Evidence basis for grouping these events into one incident:")
    for cls in evidence_classes_for(incident):
        lines.append("  - %s" % _EVIDENCE_EXPLAIN.get(cls, cls))

    lines.append("")
    lines.append(
        "Write 2-4 sentences for the operator, using the hedging language "
        "above for anything that is not a hex/tail match."
    )
    return "\n".join(lines)


# --- refusal detection -----------------------------------------------------

# Deliberately simple substring markers, not a classifier: this project has
# been burned by a stored refusal reading as fact later, so err toward
# flagging a borderline reply as a refusal rather than storing it as a
# summary. False positives here just mean a synopsis line is withheld, not
# that a false fact gets published.
_REFUSAL_MARKERS = (
    "i cannot", "i can't", "i'm unable", "i am unable",
    "as an ai", "as a language model", "i won't", "i will not",
)


def _looks_like_refusal(text):
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _confidence_note(incident):
    kinds = set(incident.get("kinds") or [])
    if "voice" in kinds and "adsb" in kinds:
        return (
            "voice content in this window is linked by time+channel "
            "proximity (inference), not a hex/tail match -- treat any "
            "reference to it as unverified"
        )
    if "voice" in kinds:
        return (
            "includes voice/ASR content, which is a lead, not a verified "
            "fact (docs/COVERAGE.md)"
        )
    return (
        "all joins in this incident are direct matches (hex/tail/device); "
        "no time-proximity inference"
    )


# --- summarise ---------------------------------------------------------


# Two different over-claims, licensed by two different kinds of evidence.
#
# A hex match proves WHERE THE AIRCRAFT WAS. With one, "N414LF was overhead"
# is a fact and flagging it would train the reader to ignore every warning.
# Without one, it is unsupported.
_POSITION_MARKERS = (
    " was overhead", " was over ", " was at ", " was above",
    " flew over", " flying over", " was circling", " circled over",
    " operated by", " piloted by", " belongs to",
)
# NOTHING in this system proves who transmitted. Voice is joined to an aircraft
# only by time and channel, which is coincidence until proven otherwise, so
# these are unsupported even when the hex match is solid.
_ATTRIBUTION_MARKERS = (
    " transmitted from", " came from", " broadcast from", " called from",
    " was in contact", " communicated with", " spoke to", " responded to",
    " radioed", " reported by the pilot", " the pilot said",
    " called the tower", " contacted ",
)
_HEDGE_MARKERS = (
    "heard near", "coincided", "may ", "might ", "possibly", "appears",
    "unconfirmed", "cannot be confirmed", "not confirmed", "inference",
    "proximity", "near in time", "around the same time", "no evidence",
)


def check_reply(text, evidence_classes):
    """Does the reply claim more than the evidence licenses?

    Returns (ok, note). Flagging is not censoring: the text is still stored,
    but the warning travels with it so a reader sees the disagreement rather
    than inheriting the model's confidence.
    """
    if not text:
        return True, None
    classes = set(evidence_classes or ())
    proximity = "time+channel proximity" in classes
    identified = bool(classes & {"hex match", "tail match"})
    low = " " + text.lower().replace("\n", " ") + " "
    hedged = any(h in low for h in _HEDGE_MARKERS)

    bad = []
    # attributing speech to an aircraft is never supported here
    if proximity:
        bad += [m.strip() for m in _ATTRIBUTION_MARKERS if m in low]
    # claiming a position is only unsupported without a hex/tail match
    if not identified:
        bad += [m.strip() for m in _POSITION_MARKERS if m in low]

    if bad and not hedged:
        return False, ("UNSUPPORTED ASSERTION: says %s, but %s. "
                       % (", ".join("'%s'" % b for b in bad),
                          "voice is linked to this aircraft only by time and "
                          "channel -- coincidence, not identity" if proximity
                          else "no hex or tail match establishes this"))
    if bad:
        return True, ("assertive phrasing (%s) with hedging present; read "
                      "with care" % ", ".join("'%s'" % b for b in bad))
    if proximity and not hedged:
        return True, ("proximity-only voice link and no explicit hedge; treat "
                      "any attribution as unconfirmed")
    return True, None


def summarise(incident, client, identities=None):
    """Summarise one incident via `client(prompt) -> str`.

    Returns {"text", "model", "refused", "confidence_note"}. "text" is
    None whenever there is nothing safe to store -- a refusal (refused is
    then True) or a client exception (refused stays False; the failure is
    named in confidence_note) both leave "text" as None rather than ever
    storing a refusal string as though it were a summary.
    """
    prompt = build_prompt(incident, identities)
    model_name = getattr(client, "model_name", "unknown")

    try:
        raw = client(prompt)
    except Exception as exc:
        return {
            "text": None,
            "model": model_name,
            "refused": False,
            "confidence_note": "model call failed (%s): %s" % (type(exc).__name__, exc),
        }

    text = (raw or "").strip()

    if _looks_like_refusal(text):
        return {
            "text": None,
            "model": model_name,
            "refused": True,
            "confidence_note": (
                "model declined to summarise; refusal text withheld, not "
                "stored as a summary"
            ),
        }

    if not text:
        return {
            "text": None,
            "model": model_name,
            "refused": False,
            "confidence_note": "model returned an empty response",
        }

    # Instructing the model to hedge is a request. This is the check: a reply
    # that asserts an identity resting only on time proximity is flagged, and
    # the flag travels WITH the text so a reader sees the disagreement instead
    # of inheriting the model's confidence.
    note = _confidence_note(incident)
    ok, warn = check_reply(text, evidence_classes_for(incident))
    if warn:
        note = ("%s | %s" % (note, warn)) if note else warn
    return {
        "text": text,
        "model": model_name,
        "refused": False,
        "unsupported_assertion": not ok,
        "confidence_note": note,
    }


# --- digest ----------------------------------------------------------------


def _fmt_score(incident):
    score = incident.get("score")
    try:
        return "%.2f" % float(score)
    except (TypeError, ValueError):
        return str(score)


def digest(incidents, client, limit=10):
    """One operator-facing digest string over `incidents`
    (find_incidents()'s output), ordered by score (most notable first).

    Never raises on an empty incident list or a failing/refusing client --
    both are reported in the text, not swallowed. Only the top `limit`
    incidents get a model summary; the rest are counted as omitted, never
    silently dropped.
    """
    if not incidents:
        return (
            "SDR incident digest\n"
            "Window: no incidents in range.\n"
            "0 incidents in window; 0 included, 0 omitted.\n"
        )

    ordered = sorted(
        incidents, key=lambda inc: (-(inc.get("score") or 0.0), inc.get("start") or "")
    )
    total = len(ordered)
    included = ordered[:limit]
    omitted = total - len(included)
    window_start = min(inc.get("start") for inc in incidents)
    window_end = max(inc.get("end") for inc in incidents)

    lines = [
        "SDR incident digest",
        "Window: %s to %s" % (window_start, window_end),
        "%d incidents in window; %d included below, %d omitted."
        % (total, len(included), omitted),
        "",
    ]

    for idx, incident in enumerate(included, start=1):
        result = summarise(incident, client)
        kinds = ", ".join(incident.get("kinds") or [])
        lines.append(
            "%d. [score %s] %s to %s (%s)"
            % (idx, _fmt_score(incident), incident.get("start"), incident.get("end"), kinds)
        )
        entities = incident.get("entities") or []
        if entities:
            lines.append("   entities: %s" % ", ".join(entities))
        if result["refused"]:
            lines.append(
                "   [model declined to summarise this incident -- refusal "
                "withheld, not stored as a summary]"
            )
        elif result["text"] is None:
            lines.append("   [summary unavailable: %s]" % result["confidence_note"])
        else:
            lines.append("   %s" % result["text"])
            lines.append("   note: %s" % result["confidence_note"])
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


# --- storage -----------------------------------------------------------


def store_summaries(conn, rows):
    """Persist digest results into a NEW `incident_summaries` table
    (CREATE IF NOT EXISTS -- no existing table is ever touched).

    Each row in `rows` is a dict: {"prompt_hash", "model",
    "evidence_classes" (a list, e.g. from evidence_classes_for()),
    "refused" (bool), "text" (the summary, or None)}. Upserts on
    prompt_hash (its PRIMARY KEY), so calling this twice with the same
    prompt_hash leaves exactly one row, not a duplicate -- idempotent.

    Defensive by construction: if a row claims refused=True, its stored
    summary is forced to NULL regardless of what "text" holds, so a
    refusal can never be written into the summary column and later read
    back as though it were one.

    Returns the number of rows processed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_summaries (
          prompt_hash TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          evidence_classes TEXT NOT NULL,
          refused INTEGER NOT NULL,
          summary TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for row in rows:
        refused = bool(row.get("refused"))
        conn.execute(
            """
            INSERT INTO incident_summaries
              (prompt_hash, model, evidence_classes, refused, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(prompt_hash) DO UPDATE SET
              model=excluded.model,
              evidence_classes=excluded.evidence_classes,
              refused=excluded.refused,
              summary=excluded.summary,
              created_at=excluded.created_at
            """,
            (
                row["prompt_hash"],
                row.get("model", "unknown"),
                json.dumps(row.get("evidence_classes") or []),
                1 if refused else 0,
                None if refused else row.get("text"),
                now,
            ),
        )
        n += 1
    conn.commit()
    return n


def prompt_hash(prompt):
    """sha256 hex digest of a prompt string -- the key store_summaries()
    upserts on. A small named helper so every caller hashes the same way
    build_prompt()'s output is hashed in tests."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()
