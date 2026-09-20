"""Tests for the VDL Mode 2 parser.

The controls that matter are about attribution: a frame has two ends and only
one of them is an aircraft, so the failure mode is confidently labelling a
ground station's message as an aeroplane's.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vdl2  # noqa: E402


def frame(src=None, dst=None, acars=None, freq=136975000, sig=-30.5, sec=1788000000):
    v = {"freq": freq, "sig_level": sig, "noise_level": -52.0,
         "t": {"sec": sec}, "avlc": {"frame_type": "I"}}
    if src:
        v["avlc"]["src"] = src
    if dst:
        v["avlc"]["dst"] = dst
    if acars is not None:
        v["avlc"]["acars"] = acars
    return json.dumps({"vdl2": v})


AC = {"addr": "a4e3fc", "type": "Aircraft", "status": "Airborne"}
GS = {"addr": "10ab41", "type": "Ground station"}


class TestParse(unittest.TestCase):
    def test_downlink_from_aircraft_keys_on_icao(self):
        r = vdl2.parse_line(frame(src=AC, dst=GS,
                                  acars={"reg": "N412LF", "flight": "SWA123",
                                         "label": "H1", "msg_text": "POS N43 W116"}))
        self.assertEqual(r["device_key"], "A4E3FC")
        self.assertEqual(r["icao"], "A4E3FC")
        self.assertEqual(r["direction"], "downlink")
        self.assertEqual(r["registration"], "N412LF")
        self.assertTrue(r["has_text"])
        self.assertIn("POS N43 W116", r["summary"])

    def test_uplink_to_aircraft_still_keys_on_the_aircraft(self):
        r = vdl2.parse_line(frame(src=GS, dst=AC, acars={"msg_text": "WX UPDATE"}))
        self.assertEqual(r["icao"], "A4E3FC")
        self.assertEqual(r["direction"], "uplink")

    def test_ground_to_ground_never_claims_an_aircraft(self):
        # NEGATIVE CONTROL: both ends are ground stations. Reporting an ICAO
        # here would attribute a controller's traffic to an aeroplane.
        r = vdl2.parse_line(frame(src=GS, dst={"addr": "10ab42",
                                               "type": "Ground station"}))
        self.assertIsNotNone(r)
        self.assertIsNone(r["icao"])
        self.assertTrue(r["device_key"].startswith("vdl2/gs-"))

    def test_missing_payload_is_not_an_empty_message(self):
        # NEGATIVE CONTROL: a link-layer frame carries no message at all.
        # "aircraft sent a blank message" is a different claim.
        r = vdl2.parse_line(frame(src=AC, dst=GS))
        self.assertFalse(r["has_text"])
        self.assertIsNone(r["text"])
        self.assertIn("I", r["summary"])

    def test_empty_string_payload_is_also_not_text(self):
        r = vdl2.parse_line(frame(src=AC, dst=GS, acars={"msg_text": "   "}))
        self.assertFalse(r["has_text"])
        self.assertIsNone(r["text"])

    def test_registration_fallback_when_no_icao(self):
        r = vdl2.parse_line(frame(src={"addr": "x", "type": "Unknown"},
                                  dst={"addr": "y", "type": "Unknown"},
                                  acars={"reg": "N999XX"}))
        self.assertEqual(r["device_key"], "vdl2/N999XX")
        self.assertIsNone(r["icao"])

    def test_icao_is_uppercased_to_match_adsb(self):
        # ADS-B stores hex uppercase; a lowercase key would never join
        r = vdl2.parse_line(frame(src=AC, dst=GS))
        self.assertEqual(r["device_key"], "A4E3FC")

    def test_signal_fields_carried(self):
        r = vdl2.parse_line(frame(src=AC, dst=GS, sig=-25.5))
        self.assertEqual(r["sig_level"], -25.5)
        self.assertEqual(r["freq_hz"], 136975000)

    def test_garbage_lines_are_skipped_not_fatal(self):
        for bad in ("", "not json", "{}", '{"other": 1}', None):
            self.assertIsNone(vdl2.parse_line(bad))

    def test_stream_skips_noise_and_keeps_frames(self):
        lines = ["garbage", frame(src=AC, dst=GS), "{}",
                 frame(src=GS, dst=AC, acars={"msg_text": "HI"})]
        rows = vdl2.parse_stream(lines)
        self.assertEqual(len(rows), 2)

    def test_summarise_counts_distinct_aircraft(self):
        rows = vdl2.parse_stream([
            frame(src=AC, dst=GS, acars={"msg_text": "A"}),
            frame(src=AC, dst=GS),
            frame(src={"addr": "abcdef", "type": "Aircraft"}, dst=GS,
                  acars={"msg_text": "B"}),
        ])
        s = vdl2.summarise(rows)
        self.assertIn("3 VDL2 frames", s)
        self.assertIn("2 with message text", s)
        self.assertIn("2 distinct aircraft", s)

    def test_summarise_empty(self):
        self.assertEqual(vdl2.summarise([]), "no VDL2 frames")


if __name__ == "__main__":
    unittest.main(verbosity=2)
