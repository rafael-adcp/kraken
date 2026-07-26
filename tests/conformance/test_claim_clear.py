#!/usr/bin/env python3
"""kraken.py claim on a clear task: exit 0, the claim ref created (the CAS —
the lock itself), in-progress added as projection, disclaimer + claim marker
posted as narrative (the visible prose is human courtesy only)."""
import json
import os
import unittest

from harness import KrakenConformanceTest


class ClaimClearTests(KrakenConformanceTest):
    def test_claim_clear(self):
        self.mk_issue(7, "a task", "kraken-task", "project:app")

        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "clean claim exit")
        self.assertEqual(r.out, "claim: claimed issue=7 worker=w1", "machine line")

        # The lock: refs/kraken/claims/7 exists and its commit message is the
        # claim marker naming the worker.
        sha = self.claim_ref(7)
        self.assertIsNotNone(sha, "claim ref missing after a won claim")
        with open(os.path.join(self.state, "objects", sha + ".json"),
                  encoding="utf-8") as f:
            commit = json.load(f)
        self.assertIn('"type":"claim"', commit["message"], "claim commit marker type")
        self.assertIn('"worker":"w1"', commit["message"], "claim commit marker worker")

        # The projection and the narrative.
        self.assertTrue(self.has_label(7, "in-progress"), "in-progress label missing after claim")
        self.assertEqual(self.comment_count(7), 1, "exactly one comment posted")
        self.assert_disclaimer(7, "w1")
        self.assert_marker(7, '{"type":"claim","worker":"w1"}')
        # The retired protocol/1 visible line is NOT emitted.
        self.assertFalse(
            any(l.startswith("claimed-by:") for l in self.last_comment(7).split("\n")),
            "producer emitted a legacy claimed-by: line")


if __name__ == "__main__":
    unittest.main()
