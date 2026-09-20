#!/usr/bin/env python3
"""Tests for digest.py (T12 -- LLM digest over incidents).

Project rule: a detector nobody has watched DECLINE to fire is not a
detector. No test here calls a real model -- every `client` is an
injected fake, per the ticket ("Do not actually call any model in your
tests. Inject the client."). Every positive case below has a matching
negative control.

Run: python3 analysis/test_digest.py -v
"""
import json
import sqlite3
import unittest

from digest import (
    build_prompt, summarise, digest, store_summaries,
    evidence_classes_for, prompt_hash,
)


# --- fixtures ------------------------------------------------------------


def make_incident(start="2026-01-05T10:00:00+00:00", end="2026-01-05T10:02:00+00:00",
                   kinds=None, entities=None, events=None, score=10.0):
    return {
        "start": start, "end": end,
        "kinds": kinds if kinds is not None else ["adsb"],
        "entities": entities if entities is not None else ["A4E3FC"],
        "events": events if events is not None else ["events:1", "events:2", "events:3"],
        "score": score,
    }


AIRCRAFT_IDENTITY = {
    "hex": "A4E3FC", "tail": "N412LF", "callsign": "LIFE23",
    "label": "N412LF BELL 407", "registration": "N412LF",
}


def echo_client(reply):
    """A fake client that ignores the prompt and returns a fixed reply."""
    def _client(prompt):
        return reply
    return _client


def raising_client(exc):
    def _client(prompt):
        raise exc
    return _client


# --- evidence_classes_for --------------------------------------------------


class TestEvidenceClassesFor(unittest.TestCase):
    def test_adsb_only_is_hex_match(self):
        inc = make_incident(kinds=["adsb"])
        self.assertEqual(evidence_classes_for(inc), ["hex match"])

    def test_adsb_plus_acars_adds_tail_match(self):
        inc = make_incident(kinds=["acars", "adsb"])
        self.assertEqual(evidence_classes_for(inc), ["hex match", "tail match"])

    def test_adsb_plus_voice_adds_time_channel_proximity(self):
        inc = make_incident(kinds=["adsb", "voice"])
        self.assertEqual(evidence_classes_for(inc), ["hex match", "time+channel proximity"])

    def test_all_three_lanes(self):
        inc = make_incident(kinds=["acars", "adsb", "voice"])
        self.assertEqual(
            evidence_classes_for(inc),
            ["hex match", "tail match", "time+channel proximity"],
        )

    def test_single_lane_ism_is_same_device_not_a_cross_lane_class(self):
        """Negative control: a TPMS-only burst never gets 'tail match' or
        'time+channel proximity' -- those require an adsb anchor."""
        inc = make_incident(kinds=["ism"], entities=["tpms/AABBCC"])
        self.assertEqual(evidence_classes_for(inc), ["same device/channel"])

    def test_voice_only_is_same_device_not_proximity(self):
        """Negative control: voice alone (no adsb) never claims a
        time+channel proximity join -- there is nothing to be proximate to."""
        inc = make_incident(kinds=["voice"], entities=["channel:GMRS15"])
        self.assertEqual(evidence_classes_for(inc), ["same device/channel"])

    def test_acars_only_is_same_device_not_tail_match(self):
        """Negative control: acars alone (no adsb) never claims tail match
        -- tail match is specifically the acars<->adsb cross-lane join."""
        inc = make_incident(kinds=["acars"], entities=["acars/N412LF"])
        self.assertEqual(evidence_classes_for(inc), ["same device/channel"])


# --- build_prompt -----------------------------------------------------------


class TestBuildPrompt(unittest.TestCase):
    def test_carries_evidence_class_of_every_join(self):
        inc = make_incident(kinds=["acars", "adsb", "voice"])
        prompt = build_prompt(inc, {"A4E3FC": AIRCRAFT_IDENTITY})
        self.assertIn("hex match", prompt)
        self.assertIn("tail match", prompt)
        self.assertIn("time+channel proximity", prompt)

    def test_always_states_transcript_is_a_lead_not_a_fact(self):
        for kinds in (["adsb"], ["adsb", "voice"], ["ism"]):
            prompt = build_prompt(make_incident(kinds=kinds))
            self.assertIn("LEAD", prompt)
            self.assertIn("never a", prompt.lower())

    def test_time_proximity_only_join_produces_hedging_not_assertion(self):
        """Required control: an incident joined ONLY by time proximity
        (adsb + voice, no acars/tail match) must carry the hedge language,
        and its evidence line must say INFERENCE, not state the join as
        settled fact."""
        inc = make_incident(kinds=["adsb", "voice"], entities=["A4E3FC"])
        prompt = build_prompt(inc, {"A4E3FC": AIRCRAFT_IDENTITY})
        self.assertIn("heard near", prompt)
        self.assertIn("INFERENCE", prompt)
        # the callsign is present (it's a real self-reported field on the
        # aircraft, not something derived from the voice join) but nothing
        # in the evidence section may claim the voice content as settled:
        evidence_section = prompt.split("Evidence basis")[1]
        self.assertNotIn("Fact.", evidence_section.split("time+channel")[1].split("\n")[0])

    def test_never_fabricates_a_detail_not_in_the_incident(self):
        """No hardcoded example location/agency/etc leaks into the prompt
        -- only fields drawn from the incident dict and identities appear."""
        inc = make_incident(kinds=["adsb"], entities=["FFFFFF"], score=3.5)
        prompt = build_prompt(inc, identities={})
        self.assertIn("FFFFFF", prompt)
        self.assertIn("unidentified", prompt)
        self.assertNotIn("N412LF", prompt)

    def test_entity_prefixes_are_described_without_identities(self):
        inc = make_incident(
            kinds=["acars", "voice", "ism"],
            entities=["acars/N412LF", "channel:BOI_TWR_118", "tpms/AABBCC"],
        )
        prompt = build_prompt(inc)
        self.assertIn("ACARS tail N412LF", prompt)
        self.assertIn("voice channel BOI_TWR_118", prompt)
        self.assertIn("TPMS tyre sensor", prompt)
        self.assertIn("never a vehicle, owner, or person", prompt)

    def test_empty_entities_does_not_crash(self):
        inc = make_incident(entities=[])
        prompt = build_prompt(inc)
        self.assertIn("none recorded", prompt)

    def test_identities_defaults_to_none_safely(self):
        inc = make_incident()
        prompt = build_prompt(inc)  # identities omitted entirely
        self.assertIn("unidentified", prompt)


# --- summarise ---------------------------------------------------------


class TestSummarise(unittest.TestCase):
    def test_normal_reply_is_stored_as_text(self):
        inc = make_incident()
        result = summarise(inc, echo_client("Aircraft N412LF made three ADS-B position reports."))
        self.assertFalse(result["refused"])
        self.assertEqual(result["text"], "Aircraft N412LF made three ADS-B position reports.")
        self.assertIn("confidence_note", result)

    def test_model_name_from_client_attribute(self):
        client = echo_client("fine")
        client.model_name = "qwen3-14b-maxagent"
        result = summarise(make_incident(), client)
        self.assertEqual(result["model"], "qwen3-14b-maxagent")

    def test_model_name_unknown_when_client_has_no_attribute(self):
        result = summarise(make_incident(), echo_client("fine"))
        self.assertEqual(result["model"], "unknown")

    def test_refusal_sets_refused_true_and_does_not_store_refusal_as_summary(self):
        """Required negative control."""
        for refusal in (
            "I cannot provide identifying information about individuals.",
            "I'm unable to summarise this safely.",
            "As an AI, I don't have the ability to confirm identities.",
        ):
            with self.subTest(refusal=refusal):
                result = summarise(make_incident(), echo_client(refusal))
                self.assertTrue(result["refused"])
                self.assertIsNone(result["text"])
                self.assertNotIn(refusal, str(result["text"]))

    def test_client_exception_is_handled_not_raised(self):
        """Required negative control."""
        result = summarise(make_incident(), raising_client(RuntimeError("host down")))
        self.assertFalse(result["refused"])
        self.assertIsNone(result["text"])
        self.assertIn("host down", result["confidence_note"])

    def test_empty_reply_is_not_stored_as_text(self):
        result = summarise(make_incident(), echo_client("   "))
        self.assertIsNone(result["text"])
        self.assertFalse(result["refused"])

    def test_confidence_note_flags_voice_proximity_incidents(self):
        inc = make_incident(kinds=["adsb", "voice"])
        result = summarise(inc, echo_client("fine"))
        self.assertIn("proximity", result["confidence_note"])

    def test_confidence_note_for_pure_hex_match_incident(self):
        inc = make_incident(kinds=["adsb"])
        result = summarise(inc, echo_client("fine"))
        self.assertIn("no time-proximity inference", result["confidence_note"])


# --- digest --------------------------------------------------------------


class TestDigest(unittest.TestCase):
    def test_empty_incident_list_produces_valid_digest_not_a_crash(self):
        """Required negative control."""
        out = digest([], echo_client("unused"))
        self.assertIsInstance(out, str)
        self.assertIn("0 incidents", out)
        self.assertNotIn("Traceback", out)

    def test_orders_by_score_descending(self):
        low = make_incident(score=1.0, entities=["LOW"])
        high = make_incident(score=99.0, entities=["HIGH"])
        out = digest([low, high], echo_client("summary text"))
        self.assertLess(out.index("HIGH"), out.index("LOW"))

    def test_states_window_and_omitted_count(self):
        incidents = [
            make_incident(start="2026-01-05T10:00:00+00:00", end="2026-01-05T10:01:00+00:00", score=float(i))
            for i in range(15)
        ]
        out = digest(incidents, echo_client("summary text"), limit=10)
        self.assertIn("15 incidents in window", out)
        self.assertIn("10 included below", out)
        self.assertIn("5 omitted", out)
        self.assertIn("Window: 2026-01-05T10:00:00+00:00 to 2026-01-05T10:01:00+00:00", out)

    def test_refusing_client_noted_not_stored_as_text(self):
        out = digest([make_incident()], echo_client("I cannot help with that."))
        self.assertIn("declined to summarise", out)
        self.assertNotIn("I cannot help with that.", out)

    def test_raising_client_handled_failure_noted_digest_still_returns(self):
        """Required negative control: digest still returns, with the
        failure noted, and nothing is stored as if it succeeded (digest
        never touches a database at all -- only store_summaries does)."""
        out = digest([make_incident(), make_incident()], raising_client(ConnectionError("no route")))
        self.assertIsInstance(out, str)
        self.assertIn("summary unavailable", out)
        self.assertIn("no route", out)
        self.assertNotIn("Traceback", out)

    def test_limit_zero_still_reports_totals_without_summaries(self):
        out = digest([make_incident(), make_incident()], echo_client("x"), limit=0)
        self.assertIn("2 incidents in window", out)
        self.assertIn("0 included below, 2 omitted", out)


# --- store_summaries -------------------------------------------------------


def make_conn():
    return sqlite3.connect(":memory:")


class TestStoreSummaries(unittest.TestCase):
    def test_creates_table_and_stores_fields(self):
        conn = make_conn()
        row = {
            "prompt_hash": prompt_hash("prompt-1"),
            "model": "qwen3-14b-maxagent",
            "evidence_classes": ["hex match", "time+channel proximity"],
            "refused": False,
            "text": "Aircraft N412LF was heard near a voice transmission on BOI_TWR.",
        }
        n = store_summaries(conn, [row])
        self.assertEqual(n, 1)
        stored = conn.execute(
            "SELECT model, evidence_classes, refused, summary FROM incident_summaries "
            "WHERE prompt_hash = ?", (row["prompt_hash"],)
        ).fetchone()
        self.assertEqual(stored[0], "qwen3-14b-maxagent")
        self.assertEqual(json.loads(stored[1]), ["hex match", "time+channel proximity"])
        self.assertEqual(stored[2], 0)
        self.assertEqual(stored[3], row["text"])

    def test_refusal_row_never_stores_refusal_text_as_summary(self):
        """Required: a refusal saved as a summary later reads as fact --
        so even if a careless caller passes refusal text in "text", a
        refused=True row must land with summary NULL."""
        conn = make_conn()
        row = {
            "prompt_hash": prompt_hash("prompt-2"),
            "model": "qwen3-14b-maxagent",
            "evidence_classes": ["hex match"],
            "refused": True,
            "text": "I cannot help with that.",
        }
        store_summaries(conn, [row])
        summary, refused = conn.execute(
            "SELECT summary, refused FROM incident_summaries WHERE prompt_hash = ?",
            (row["prompt_hash"],),
        ).fetchone()
        self.assertIsNone(summary)
        self.assertEqual(refused, 1)

    def test_idempotent_on_same_prompt_hash(self):
        """Required negative control: calling twice with the same
        prompt_hash must not create a duplicate row."""
        conn = make_conn()
        h = prompt_hash("prompt-3")
        row_v1 = {"prompt_hash": h, "model": "m1", "evidence_classes": ["hex match"],
                  "refused": False, "text": "first pass"}
        row_v2 = {"prompt_hash": h, "model": "m1", "evidence_classes": ["hex match"],
                  "refused": False, "text": "second pass, corrected"}
        store_summaries(conn, [row_v1])
        store_summaries(conn, [row_v2])
        count = conn.execute("SELECT COUNT(*) FROM incident_summaries").fetchone()[0]
        self.assertEqual(count, 1)
        summary = conn.execute(
            "SELECT summary FROM incident_summaries WHERE prompt_hash = ?", (h,)
        ).fetchone()[0]
        self.assertEqual(summary, "second pass, corrected")

    def test_empty_rows_creates_table_with_no_rows(self):
        conn = make_conn()
        n = store_summaries(conn, [])
        self.assertEqual(n, 0)
        count = conn.execute("SELECT COUNT(*) FROM incident_summaries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_does_not_touch_an_existing_events_table(self):
        """Never modify an existing table -- simulate one already present
        (as it would be in the real db alongside correlate.py's tables)."""
        conn = make_conn()
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT)")
        conn.execute("INSERT INTO events(ts) VALUES ('2026-01-05T10:00:00+00:00')")
        store_summaries(conn, [{
            "prompt_hash": prompt_hash("p"), "model": "m",
            "evidence_classes": [], "refused": False, "text": "x",
        }])
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(count, 1)


# --- integration: build_prompt + summarise + store_summaries --------------


class TestEndToEnd(unittest.TestCase):
    def test_full_pipeline_for_one_incident(self):
        conn = make_conn()
        inc = make_incident(kinds=["acars", "adsb", "voice"],
                             entities=["A4E3FC", "acars/N412LF", "channel:BOI_TWR_118"])
        identities = {"A4E3FC": AIRCRAFT_IDENTITY}

        prompt = build_prompt(inc, identities)
        result = summarise(inc, echo_client("N412LF (BELL 407) made position reports; "
                                             "a voice transmission was heard near it on "
                                             "BOI_TWR_118."), identities)
        row = {
            "prompt_hash": prompt_hash(prompt),
            "model": result["model"],
            "evidence_classes": evidence_classes_for(inc),
            "refused": result["refused"],
            "text": result["text"],
        }
        n = store_summaries(conn, [row])
        self.assertEqual(n, 1)

        stored = conn.execute(
            "SELECT evidence_classes, refused, summary FROM incident_summaries"
        ).fetchone()
        self.assertEqual(
            json.loads(stored[0]), ["hex match", "tail match", "time+channel proximity"]
        )
        self.assertEqual(stored[1], 0)
        self.assertIn("heard near", stored[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
