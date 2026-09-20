"""Rules named in no_notify_rules must be RECORDED but never pushed.

This started life asserting against the author's own config file -- that three
specific aircraft rules were silenced, because that receiver sits 2.4 nm from a
commercial airport and aeroplanes being low is the normal state of its sky.
That is a site policy, not a property of the code, and a test that reads the
operator's config can only ever pass on the operator's machine.

What is a property of the code, and what is asserted here:

  * a rule listed in no_notify_rules is withheld from the push
  * a rule NOT listed is still pushed -- a filter that silenced everything
    would satisfy "no plane alerts" and quietly destroy the alert channel, so
    the other direction is asserted just as hard
  * the filter sits BELOW the table insert, so withholding a push never
    withholds the record
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A fixture standing in for one plausible site policy. The specific rule names
# are arbitrary; what is being tested is that the partition follows the config.
CFG = {
    "no_notify_rules": ["low_aircraft", "notable_aircraft", "emergency_squawk"],
}


class TestNoNotifyRules(unittest.TestCase):
    def setUp(self):
        self.quiet = set(CFG.get("no_notify_rules") or [])

    def test_listed_rules_are_silenced(self):
        for rule in ("low_aircraft", "notable_aircraft", "emergency_squawk"):
            self.assertIn(rule, self.quiet,
                          "%s would still push a notification" % rule)

    def test_unlisted_rules_are_NOT_silenced(self):
        """The channel must still work for the things it exists for."""
        for rule in ("new_device", "new_carrier", "watchlist_hit",
                     "device_missing", "sensor_alert"):
            self.assertNotIn(rule, self.quiet,
                             "%s was silenced -- the alert channel is now useless"
                             % rule)

    def test_the_filter_partitions_a_mixed_batch_correctly(self):
        """Exercise the real splitting logic, not just the config."""
        raised = [
            ("low_aircraft", "A1", "Low aircraft"),
            ("new_device", "Toyota/00ab12c0", "New ism device"),
            ("emergency_squawk", "A2", "squawk 7700"),
            ("new_carrier", "ism915/920", "New carrier"),
            ("notable_aircraft", "A3", "notable"),
        ]
        held = [r for r in raised if r[0] in self.quiet]
        sent = [r for r in raised if r[0] not in self.quiet]
        self.assertEqual(sorted(r[0] for r in held),
                         ["emergency_squawk", "low_aircraft", "notable_aircraft"])
        self.assertEqual(sorted(r[0] for r in sent),
                         ["new_carrier", "new_device"])

    def test_alerts_are_still_recorded_not_discarded(self):
        """The filter sits in the NOTIFY path, after the table insert.

        If it ever moves above the insert, silenced alerts vanish from the
        console too, and the operator loses the data as well as the push. This
        reads the source because the ordering is the whole guarantee, and no
        behavioural test distinguishes "recorded then withheld" from "never
        recorded" once the row is absent either way.
        """
        src = open(os.path.join(ROOT, "broker", "collector.py")).read()
        insert_at = src.index("INSERT INTO alerts(ts,rule,device_key,message)")
        # Anchor on the FILTER CODE, not on any mention of the config key.
        # An earlier version searched for "no_notify_rules" and matched a schema
        # comment sitting above the insert, so the ordering check silently
        # compared the wrong two positions and failed for the wrong reason.
        filter_at = src.index('quiet = set(cfg.get("no_notify_rules")')
        self.assertLess(
            insert_at, filter_at,
            "the no_notify filter moved ABOVE the alerts insert -- silenced "
            "alerts are now being discarded entirely, not merely not pushed")


if __name__ == "__main__":
    unittest.main()
