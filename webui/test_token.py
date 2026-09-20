"""The worker token guarding /api/transcript.

That endpoint rewrites the copy of record for what a transmission said, and a
watchlist match on the supplied text sends a real Signal push. Unauthenticated,
any device on the LAN could page the operator with a fabricated hit.
"""
import os
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


class TestWorkerToken(unittest.TestCase):
    def test_token_is_generated_and_stable(self):
        a = server.worker_token()
        self.assertTrue(a and len(a) >= 20, "token too short to be useful")
        self.assertEqual(a, server.worker_token(), "token must not rotate per call")

    def test_token_file_is_owner_only(self):
        """Any local user could otherwise read it and forge a watchlist alert."""
        server.worker_token()
        mode = stat.S_IMODE(os.stat(server.TOKEN_PATH).st_mode)
        self.assertEqual(mode, 0o600, "token file is %o, expected 600" % mode)

    def test_token_lives_outside_version_control(self):
        """config files can be shared; a secret in one would travel with them."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rel = os.path.relpath(server.TOKEN_PATH, root)
        ignored = open(os.path.join(root, ".gitignore")).read().split()
        self.assertTrue(any(rel.startswith(pat.rstrip("/")) for pat in ignored),
                        "%s is not covered by .gitignore" % rel)

    def test_correct_token_accepted(self):
        self.assertTrue(server.token_ok(server.worker_token()))

    def test_wrong_empty_and_missing_are_rejected(self):
        for bad in ("wrong", "", None, " ", server.worker_token() + "x"):
            self.assertFalse(server.token_ok(bad),
                             "token_ok accepted %r" % (bad,))

    def test_near_miss_is_rejected(self):
        """A token differing by one character must not pass."""
        good = server.worker_token()
        near = ("a" if good[0] != "a" else "b") + good[1:]
        self.assertFalse(server.token_ok(near))


if __name__ == "__main__":
    unittest.main()
