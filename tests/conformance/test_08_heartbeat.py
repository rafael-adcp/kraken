#!/usr/bin/env python3
"""kraken.py heartbeat: force-move the claim ref to a fresh commit carrying the
progress text — NO timeline comment, no label change — and the task stays
locked afterwards (a worker heartbeating must never free its own claim)."""
import json
import os
import unittest

from harness import KrakenConformanceTest


class HeartbeatTests(KrakenConformanceTest):
    def test_heartbeat_advances_the_ref_and_posts_nothing(self):
        self.mk_issue(7, "long task", "kraken-task", "project:app", "in-progress")
        old_sha = self.mk_claim_ref(7, "w1", age_hours=3)

        r = self.kraken("heartbeat", "OWNER/tasks", 7, "w1", "tests green, writing docs")
        self.assertEqual(r.rc, 0, "heartbeat exit")
        self.assertEqual(r.out, "heartbeat: advanced issue=7 worker=w1", "machine line")

        # The ref moved to a fresh commit carrying the marker + progress text.
        new_sha = self.claim_ref(7)
        self.assertIsNotNone(new_sha, "heartbeat deleted the claim ref")
        self.assertNotEqual(new_sha, old_sha, "heartbeat did not advance the ref")
        with open(os.path.join(self.state, "objects", new_sha + ".json"),
                  encoding="utf-8") as f:
            commit = json.load(f)
        self.assertIn('"type":"heartbeat"', commit["message"], "heartbeat marker type")
        self.assertIn('"worker":"w1"', commit["message"], "heartbeat marker worker")
        self.assertIn("tests green, writing docs", commit["message"],
                      "progress message missing from the heartbeat commit")

        # No timeline noise, no label change.
        self.assertEqual(self.comment_count(7), 0, "heartbeat posted a comment")
        self.assertTrue(self.has_label(7, "in-progress"), "heartbeat touched the labels")
        self.assertFalse(any("issue edit" in l for l in self.log_lines()),
                         "heartbeat ran an issue edit")

        # Lock invariant: the task is still held after its own heartbeat, even
        # if the label projection is lost out of band.
        self.set_labels(7, ["kraken-task", "project:app"])
        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 10, "claim against a heartbeated ref")
        self.assertEqual(r.out,
                         "claim: lost-cas issue=7 — another worker holds the claim ref",
                         "heartbeat freed its own claim")


if __name__ == "__main__":
    unittest.main()
