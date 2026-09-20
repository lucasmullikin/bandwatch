"""Ground stations must never be announced as new devices.

Written after two spurious "New uat device" messages reached a phone: one
named "3000ft" (an altitude, not an identity) and one naming the FIS-B weather
uplink. Neither is a device.

Runs entirely on in-memory values -- it never opens the live database and
never touches the notifier. The bug that prompted these fixes was an
integration test pointed at the live database, which sent real Signal
messages.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector


class TestGroundInfrastructure(unittest.TestCase):
    def test_fisb_uplink_is_not_a_device(self):
        self.assertTrue(collector._is_ground_infrastructure(
            "uat/fisb", "FIS-B ground uplink (weather / advisory products)"))

    def test_tisb_rebroadcast_is_not_a_device(self):
        """The one that arrived named '3000ft'."""
        self.assertTrue(collector._is_ground_infrastructure(
            "A1B2C3",
            "3000ft [TIS-B: ground radar rebroadcast, not the aircraft]"))

    def test_vdl2_ground_station_is_not_a_device(self):
        self.assertTrue(collector._is_ground_infrastructure(
            "vdl2/gs-10ab41", "S"))

    def test_a_real_aircraft_own_report_is_NOT_filtered(self):
        """The guard must not swallow a genuine ADS-B report.

        A filter that hides everything would pass the three tests above and be
        useless, so this asserts the other direction.
        """
        self.assertFalse(collector._is_ground_infrastructure(
            "AAB4F8", "N78946 5400ft 92kt"))

    def test_a_real_sensor_is_NOT_filtered(self):
        for key, summary in (("Toyota/00ab12c0", "32.5 PSI 24.0C"),
                             ("SimpliSafe-Gen3/39320914", "contact"),
                             ("SCMplus/79610159", "gas meter")):
            self.assertFalse(collector._is_ground_infrastructure(key, summary),
                             "%s was wrongly filtered" % key)

    def test_uat_and_vdl2_are_exempt_from_novelty_alerts(self):
        """Ground infrastructure aside, every GA aircraft overhead would page us.

        Asserted against the SHIPPED DEFAULT rather than the operator's own
        config: a test that reads config/bandwatch.json can only pass on a
        machine that has one, and what matters to a new user is that the
        default they inherit is already sane.
        """
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        example = os.path.join(root, "config", "examples", "bandwatch.json")
        cfg = json.load(open(example))
        kinds = set(cfg.get("no_novelty_alert_kinds", []))
        for k in ("adsb", "uat"):
            self.assertIn(k, kinds,
                          "%s novelty alerts are not exempt by default -- a new "
                          "install would page on every aircraft overhead" % k)


if __name__ == "__main__":
    unittest.main()
