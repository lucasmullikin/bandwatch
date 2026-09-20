"""The reply-side check: does the summary claim more than the evidence?

The digest prompt tells the model to hedge anything resting only on time
proximity. Instructing is not enforcing, and a non-compliant model writing
"N414LF was overhead" about a coincidence would previously be stored and
displayed exactly as written.

The controls that matter here are the cases where flagging would be WRONG:
a hex match earns a plain assertion, and warning there would train the reader
to ignore the warning.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import digest  # noqa: E402

PROX = ["hex match", "time+channel proximity"]
PROX_ONLY = ["time+channel proximity"]
HARD = ["hex match", "tail match"]


class TestReplyCheck(unittest.TestCase):
    def test_flags_a_bare_assertion_on_proximity_only(self):
        ok, note = digest.check_reply(
            "N414LF was overhead when the transmission occurred.", PROX_ONLY)
        self.assertFalse(ok)
        self.assertIn("UNSUPPORTED ASSERTION", note)
        self.assertIn("was overhead", note)

    def test_accepts_the_hedged_version_of_the_same_claim(self):
        ok, note = digest.check_reply(
            "A transmission was heard near the time N414LF reported a "
            "position; this is proximity only and unconfirmed.", PROX_ONLY)
        self.assertTrue(ok)
        self.assertIsNone(note)

    def test_does_not_flag_when_a_hex_match_exists(self):
        # NEGATIVE CONTROL: identity is established by the hex, so a plain
        # assertion is correct. Warning here would devalue every warning.
        ok, note = digest.check_reply(
            "N414LF was overhead at 3,300 ft.", HARD)
        self.assertTrue(ok)
        self.assertIsNone(note)

    def test_does_not_flag_a_single_lane_incident(self):
        # NEGATIVE CONTROL: nothing cross-lane was claimed at all
        ok, note = digest.check_reply(
            "A sensor reported 21.5 C repeatedly.", ["same device/channel"])
        self.assertTrue(ok)
        self.assertIsNone(note)

    def test_empty_text_is_not_a_violation(self):
        self.assertEqual(digest.check_reply("", PROX_ONLY), (True, None))
        self.assertEqual(digest.check_reply(None, PROX_ONLY), (True, None))

    def test_no_hedge_and_no_assertion_still_warns_softly(self):
        ok, note = digest.check_reply(
            "Several events were recorded in this window.", PROX_ONLY)
        self.assertTrue(ok)                 # not a violation
        self.assertIsNotNone(note)          # but worth telling the reader
        self.assertIn("unconfirmed", note)

    def test_properly_hedged_text_gets_no_note_at_all(self):
        # "may have been overhead" contains no bare assertion marker AND is
        # hedged, so there is nothing to warn about. Silence is correct here.
        ok, note = digest.check_reply(
            "N414LF may have been overhead; this is unconfirmed.", PROX_ONLY)
        self.assertTrue(ok)
        self.assertIsNone(note)

    def test_assertion_with_hedging_present_is_allowed_but_noted(self):
        # a bare marker IS present, but so is a hedge: allowed, and flagged
        # for the reader rather than silently accepted
        ok, note = digest.check_reply(
            "N414LF was overhead, possibly.", PROX_ONLY)
        self.assertTrue(ok)
        self.assertIn("read with care", note)

    def test_each_marker_is_detected(self):
        for phrase in ("was over the area", "flew over the site",
                       "operated by the county"):
            ok, _ = digest.check_reply("The aircraft %s." % phrase, PROX_ONLY)
            self.assertFalse(ok, phrase)
        for phrase in ("transmitted from the aircraft", "was in contact with tower"):
            ok, _ = digest.check_reply("The signal %s." % phrase, PROX)
            self.assertFalse(ok, phrase)

    def test_hex_match_licenses_position_but_not_attribution(self):
        # position: supported by the hex match
        ok, _ = digest.check_reply("N414LF was overhead at 3,300 ft.", PROX)
        self.assertTrue(ok)
        # attribution: never supported, hex match or not
        ok, note = digest.check_reply(
            "The transmission came from N414LF.", PROX)
        self.assertFalse(ok)
        self.assertIn("coincidence, not identity", note)

    def test_summarise_surfaces_the_flag(self):
        inc = {"start": "2026-09-01T00:00:00+00:00",
               "end": "2026-09-01T00:05:00+00:00",
               "kinds": ["adsb", "voice"], "entities": ["A4E3FC"],
               "events": ["events:1", "voice:2"], "score": 40.0}

        def bad_client(prompt):
            return "The transmission came from N414LF."

        r = digest.summarise(inc, bad_client)
        self.assertTrue(r.get("unsupported_assertion"))
        self.assertIn("UNSUPPORTED ASSERTION", r["confidence_note"])
        # the text is still returned -- flagging is not censoring
        self.assertIn("N414LF", r["text"])   # flagging is not censoring

    def test_summarise_does_not_flag_a_careful_reply(self):
        inc = {"start": "2026-09-01T00:00:00+00:00",
               "end": "2026-09-01T00:05:00+00:00",
               "kinds": ["adsb", "voice"], "entities": ["A4E3FC"],
               "events": ["events:1", "voice:2"], "score": 40.0}

        def good_client(prompt):
            return ("A voice transmission was heard near the time this "
                    "aircraft reported a position. The link is proximity "
                    "only and unconfirmed.")

        r = digest.summarise(inc, good_client)
        self.assertFalse(r.get("unsupported_assertion"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
