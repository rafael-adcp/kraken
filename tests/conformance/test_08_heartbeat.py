#!/usr/bin/env python3
"""kraken.py heartbeat: progress comment posted, no label changes — and the
heartbeat marker must NOT reset the claim window (a worker heartbeating must
never make its own claim re-claimable)."""
import unittest

from harness import KrakenConformanceTest


class HeartbeatTests(KrakenConformanceTest):
    def test_heartbeat_posts_but_never_resets(self):
        self.mk_issue(7, "long task", "kraken-task", "project:app", "in-progress")
        self.mk_comment(7, '<!-- kraken {"type":"claim","worker":"w1"} -->')

        r = self.kraken("heartbeat", "OWNER/tasks", 7, "w1", "tests green, writing docs")
        self.assertEqual(r.rc, 0, "heartbeat exit")
        self.assertEqual(r.out, "heartbeat: posted issue=7 worker=w1", "machine line")

        self.assertTrue(self.has_label(7, "in-progress"), "heartbeat touched the labels")
        self.assert_disclaimer(7, "w1")
        self.assert_marker(7, '{"type":"heartbeat","worker":"w1"}')
        self.assertIn("tests green, writing docs", self.last_comment(7).split("\n"),
                      "progress message missing")
        self.assertFalse(any("issue edit" in l for l in self.log_lines()),
                         "heartbeat ran an issue edit")

        # Window invariant: w1's claim still wins after its own heartbeat.
        self.set_labels(7, ["kraken-task", "project:app"])
        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 10, "claim against heartbeated window")
        self.assertEqual(r.out, "claim: lost-tiebreaker issue=7 winner=w1",
                         "heartbeat did not reset the window")


if __name__ == "__main__":
    unittest.main()
