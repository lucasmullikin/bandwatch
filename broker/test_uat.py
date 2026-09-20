"""Tests for the UAT 978 parser.

The control that matters: a TIS-B message is ground radar's opinion about an
aircraft, rebroadcast. It must never be stored as though the aircraft reported
its own position, because those are different standards of evidence.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uat  # noqa: E402


class TestParse(unittest.TestCase):
    def test_own_broadcast_is_adsb(self):
        r = uat.parse_line(json.dumps({
            "address": "a4e3fc", "address_qualifier": "adsb_icao",
            "callsign": "N412LF", "altitude": 4500, "ground_speed": 110,
            "position": {"lat": 40.6, "lon": -76.2}}))
        self.assertEqual(r["source"], "ADS-B")
        self.assertFalse(r["tisb"])
        self.assertEqual(r["device_key"], "A4E3FC")
        self.assertEqual(r["lat"], 40.6)
        self.assertIn("N412LF", r["summary"])

    def test_tisb_is_labelled_as_a_rebroadcast(self):
        # NEGATIVE CONTROL: this is the radar's view relayed by a ground
        # station, not the aircraft's own claim. The summary must say so.
        r = uat.parse_line(json.dumps({
            "address": "abcdef", "address_qualifier": "tisb_icao",
            "altitude": 3000}))
        self.assertEqual(r["source"], "TIS-B")
        self.assertTrue(r["tisb"])
        self.assertIn("TIS-B", r["summary"])
        self.assertIn("not the aircraft", r["summary"])

    def test_own_broadcast_never_carries_the_tisb_caveat(self):
        r = uat.parse_line(json.dumps({
            "address": "a4e3fc", "address_qualifier": "adsb_icao",
            "altitude": 4500}))
        self.assertNotIn("TIS-B", r["summary"])

    def test_fisb_uplink_is_not_an_aircraft(self):
        # NEGATIVE CONTROL: a weather uplink has no aircraft behind it, so it
        # must never be keyed on an ICAO or counted as a contact.
        r = uat.parse_line(json.dumps({"uplink": True, "address": "000000"}))
        self.assertFalse(r["is_aircraft"])
        self.assertIsNone(r["icao"])
        self.assertEqual(r["device_key"], "uat/fisb")

    def test_integer_address_is_hex_formatted_to_match_adsb(self):
        # 1090 stores hex uppercase; an integer key would never join
        r = uat.parse_line(json.dumps({
            "address": 0xA4E3FC, "address_qualifier": "adsb_icao"}))
        self.assertEqual(r["device_key"], "A4E3FC")

    def test_message_without_an_address_is_dropped(self):
        self.assertIsNone(uat.parse_line(json.dumps({"altitude": 3000})))

    def test_garbage_is_skipped(self):
        for bad in ("", "not json", "[]", None, "3"):
            self.assertIsNone(uat.parse_line(bad))

    def test_summarise_separates_the_three_sources(self):
        rows = uat.parse_stream([
            json.dumps({"address": "a1", "address_qualifier": "adsb_icao"}),
            json.dumps({"address": "a1", "address_qualifier": "adsb_icao"}),
            json.dumps({"address": "b2", "address_qualifier": "tisb_icao"}),
            json.dumps({"uplink": True}),
        ])
        s = uat.summarise(rows)
        self.assertIn("2 own-broadcast", s)
        self.assertIn("1 TIS-B", s)
        self.assertIn("1 FIS-B", s)
        self.assertIn("2 distinct aircraft", s)

    def test_summarise_empty(self):
        self.assertEqual(uat.summarise([]), "no UAT messages")


if __name__ == "__main__":
    unittest.main(verbosity=2)
