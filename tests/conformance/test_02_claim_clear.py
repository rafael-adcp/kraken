#!/usr/bin/env python3
"""kraken.py claim on a clear task: exit 0, in-progress added, disclaimer + the
protocol/3 claim marker posted (the visible prose is human courtesy only)."""
import unittest

from harness import KrakenConformanceTest


class ClaimClearTests(KrakenConformanceTest):
    def test_claim_clear(self):
        self.mk_issue(7, "a task", "kraken-task", "project:app")

        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "clean claim exit")
        self.assertEqual(r.out, "claim: claimed issue=7 worker=w1", "machine line")

        self.assertTrue(self.has_label(7, "in-progress"), "in-progress label missing after claim")
        self.assertEqual(self.comment_count(7), 1, "exactly one comment posted")
        self.assert_disclaimer(7, "w1")
        self.assert_marker(7, '{"type":"claim","worker":"w1"}')
        # The retired protocol/1 visible line is NOT emitted by a protocol/3 producer.
        self.assertFalse(
            any(l.startswith("claimed-by:") for l in self.last_comment(7).split("\n")),
            "protocol/3 producer emitted a legacy claimed-by: line")


if __name__ == "__main__":
    unittest.main()
