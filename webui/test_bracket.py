"""Regression test for the pgrep ghost-match.

This bug has now appeared three times in this project: in bandwatch, in the
supervisor, and in webui/server.py's running(). Each time the symptom was a
dead component reported as alive. A test is cheaper than finding it a fourth
time.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import server


class TestBracketed(unittest.TestCase):
    def test_inserts_brackets_on_first_alnum(self):
        self.assertEqual(server._bracketed("broker.py"), "[b]roker.py")
        self.assertEqual(server._bracketed("collector.py"), "[c]ollector.py")

    def test_is_idempotent(self):
        """Bracketing twice must not produce [[b]]roker.py."""
        once = server._bracketed("broker.py")
        self.assertEqual(server._bracketed(once), once)

    def test_path_pattern_keeps_its_path(self):
        self.assertEqual(server._bracketed("webui/server.py"),
                         "[w]ebui/server.py")

    def test_no_alnum_is_passed_through(self):
        self.assertEqual(server._bracketed("---"), "---")

    def test_bracket_form_actually_excludes_a_sibling_pgrep(self):
        """The behaviour the bracket exists for, measured rather than assumed.

        Two concurrent pgreps carrying the same bare pattern match each other,
        because pgrep excludes only its OWN pid. Uses a pattern that matches
        nothing real, so any hit is necessarily a ghost.
        """
        pat = "zzz_no_such_process_zzz.py"
        bare, brk = [], []
        for _ in range(6):
            a = subprocess.Popen(["pgrep", "-f", pat],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            b = subprocess.run(["pgrep", "-f", pat],
                               capture_output=True, text=True, timeout=5)
            a.communicate()
            bare.append(bool(b.stdout.strip()))

            a2 = subprocess.Popen(["pgrep", "-f", server._bracketed(pat)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            b2 = subprocess.run(["pgrep", "-f", server._bracketed(pat)],
                                capture_output=True, text=True, timeout=5)
            a2.communicate()
            brk.append(bool(b2.stdout.strip()))
        # the bracket form must never claim a nonexistent process is running
        self.assertFalse(any(brk),
                         "bracketed pattern produced a ghost match: %r" % brk)


if __name__ == "__main__":
    unittest.main()
