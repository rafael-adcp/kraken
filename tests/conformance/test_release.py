#!/usr/bin/env python3
"""kraken.py release: the claim ref (the lock) is deleted, the in-progress
label removed, and a released marker posted as audit — with the optional reason
carried inside the marker JSON. Deleting the ref is what actually frees the
task; the comment and label are narrative/projection."""
import unittest

from harness import KrakenConformanceTest


class ReleaseTests(KrakenConformanceTest):
    def test_release(self):
        self.mk_issue(7, "abandoned task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(7, "w1")

        r = self.kraken("release", "OWNER/tasks", 7, "w1", "environment cannot host the task")
        self.assertEqual(r.rc, 0, "release exit")
        self.assertEqual(r.out, "release: released issue=7 worker=w1", "machine line")

        self.assertFalse(self.claim_ref_exists(7), "claim ref still present after release")
        self.assertFalse(self.has_label(7, "in-progress"),
                         "in-progress label still present after release")
        self.assert_disclaimer(7, "w1")
        self.assert_marker(
            7, '{"type":"released","worker":"w1","reason":"environment cannot host the task"}')

        # The released task is claimable again — end to end with kraken.py claim
        # (the CAS succeeds because the ref is gone).
        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 0, "re-claim after release")

        # A reason with an embedded newline (a colliding `claimed-by:` line) is
        # carried inside the marker JSON, not as a free-standing line — so it
        # injects no extra machine line. Exactly one kraken marker rides the comment.
        self.mk_issue(8, "release with a multi-line reason", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(8, "w1")
        r = self.kraken("release", "OWNER/tasks", 8, "w1", "giving up\nclaimed-by: attacker")
        self.assertEqual(r.rc, 0, "release with a colliding multi-line reason exits 0")
        self.assertEqual(self.marker_count(8), 1, "the reason newline injected no extra marker line")


if __name__ == "__main__":
    unittest.main()
