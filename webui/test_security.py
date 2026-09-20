"""The console's access control, in both directions.

Before this existed the console bound 0.0.0.0 and every mutating endpoint was
unauthenticated: anything on the network could stop the pipeline, take a radio
out of the rotation and retune it, or silence alerting -- and nothing about the
console would have looked wrong afterwards. /api/transcript was authenticated,
with a comment explaining exactly why; the endpoints that drive the radios had
simply never been given the same treatment.

Every test here has its negative control, because the failure that matters is
an access check that reports healthy and permits everything.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


class Base(unittest.TestCase):
    def setUp(self):
        self._real = server.read_config
        self.cfg = {}
        server.read_config = lambda: self.cfg

    def tearDown(self):
        server.read_config = self._real


class TestBindDefault(Base):
    def test_default_is_loopback(self):
        """No ui_bind at all must mean this machine only.

        The default is the whole control: most people never edit it, and an
        unconfigured install must not be reachable from the network.
        """
        self.assertEqual(server.ui_bind(), "127.0.0.1")
        self.assertFalse(server.exposed())

    def test_explicit_loopback_is_not_exposed(self):
        for addr in ("127.0.0.1", "::1", "localhost"):
            self.cfg = {"ui_bind": addr}
            self.assertFalse(server.exposed(), "%s counted as exposed" % addr)

    def test_wildcard_and_lan_addresses_are_exposed(self):
        """NEGATIVE CONTROL: the check must actually be able to say yes."""
        for addr in ("0.0.0.0", "::", "172.16.4.20", "10.0.0.4"):
            self.cfg = {"ui_bind": addr}
            self.assertTrue(server.exposed(), "%s not counted as exposed" % addr)

    def test_whitespace_does_not_smuggle_an_address_past_the_check(self):
        self.cfg = {"ui_bind": "  0.0.0.0  "}
        self.assertTrue(server.exposed())


class TestConsolePassword(Base):
    def test_no_password_means_no_gate(self):
        """On loopback, an unset password leaves the console usable."""
        self.cfg = {}
        self.assertFalse(server.console_auth_required())
        self.assertTrue(server.console_ok(None))
        self.assertTrue(server.console_ok(""))

    def test_a_set_password_gates_everything(self):
        self.cfg = {"ui_password": "correct horse"}
        self.assertTrue(server.console_auth_required())
        self.assertTrue(server.console_ok("correct horse"))

    def test_wrong_empty_and_missing_are_rejected(self):
        """NEGATIVE CONTROL. A check that only ever returns True is not a check."""
        self.cfg = {"ui_password": "correct horse"}
        for bad in ("wrong", "", None, " ", "correct hors", "correct horse "):
            self.assertFalse(server.console_ok(bad),
                             "console_ok accepted %r" % (bad,))

    def test_near_miss_is_rejected(self):
        self.cfg = {"ui_password": "abcdef"}
        self.assertFalse(server.console_ok("abcdeg"))
        self.assertFalse(server.console_ok("abcde"))
        self.assertFalse(server.console_ok("abcdefg"))

    def test_non_string_password_does_not_crash_the_compare(self):
        """A JSON body can put anything in that field."""
        self.cfg = {"ui_password": "abc"}
        for bad in (0, 1, [], {}, True):
            self.assertFalse(server.console_ok(bad), "accepted %r" % (bad,))


class TestUnsafeCombination(Base):
    """Exposed AND unauthenticated is the configuration that must be unreachable.

    The startup path raises SystemExit on it rather than logging a warning,
    because a warning in a log nobody reads is how this stays broken. These
    assert the condition the startup check tests, so the rule is pinned even if
    that block is refactored.
    """

    def test_exposed_without_password_is_the_refused_case(self):
        self.cfg = {"ui_bind": "0.0.0.0", "ui_password": ""}
        self.assertTrue(server.exposed() and not server.console_auth_required())

    def test_exposed_with_password_is_allowed(self):
        self.cfg = {"ui_bind": "0.0.0.0", "ui_password": "pw"}
        self.assertFalse(server.exposed() and not server.console_auth_required())

    def test_loopback_without_password_is_allowed(self):
        self.cfg = {"ui_bind": "127.0.0.1", "ui_password": ""}
        self.assertFalse(server.exposed() and not server.console_auth_required())

    def test_the_startup_guard_still_references_both_conditions(self):
        """The refusal lives in __main__, which the tests do not execute.

        So assert the source still checks both halves. A refactor that drops
        either one re-opens the hole silently, and no behavioural test here
        would notice.
        """
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "server.py")) as fh:
            src = fh.read()
        guard = "if bind not in LOOPBACK and not console_auth_required():"
        self.assertIn(guard, src,
                      "the startup refusal for exposed-and-unauthenticated is gone")
        self.assertIn("raise SystemExit", src.split(guard, 1)[1][:400],
                      "the guard no longer refuses to start")


class TestSecurityState(Base):
    def test_reports_loopback_honestly(self):
        self.cfg = {"ui_bind": "127.0.0.1"}
        st = server.security_state()
        self.assertFalse(st["exposed"])
        self.assertEqual(st["reach"], "this machine only")
        self.assertFalse(st["password_set"])

    def test_does_not_echo_the_wildcard_back_as_an_address(self):
        """0.0.0.0 is a bind wildcard, not somewhere you can point a browser."""
        self.cfg = {"ui_bind": "0.0.0.0", "ui_password": "pw"}
        st = server.security_state()
        self.assertTrue(st["exposed"])
        self.assertNotIn("0.0.0.0", st["reach"])
        self.assertIn("network", st["reach"])

    def test_never_leaks_the_password_itself(self):
        """The panel is served to anyone who can read the console."""
        self.cfg = {"ui_bind": "0.0.0.0", "ui_password": "s3cret-value"}
        blob = repr(server.security_state())
        self.assertNotIn("s3cret-value", blob)
        self.assertTrue(server.security_state()["password_set"])


if __name__ == "__main__":
    unittest.main()
