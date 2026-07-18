#!/usr/bin/env python3
"""gh failures in the write transitions: exit 20 at every stage, and the
comment-first ordering means a half-executed transition is always held (never a
label state the machine lines can't explain)."""
import os
import unittest

from harness import KrakenConformanceTest


class WriteTransitionFailureTests(KrakenConformanceTest):
    def test_write_transition_failures(self):
        # escalate: comment fails -> nothing changed.
        self.mk_issue(7, "blocked task", "kraken-task", "project:app", "in-progress")
        q = os.path.join(self.state, "q.md")
        self._write(q, "which way?\n")
        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", q, fail="issue comment")
        self.assertEqual(r.rc, 20, "escalate exit on comment failure")
        self.assertEqual(r.out, "escalate: gh-failure issue=7 stage=comment", "escalate failure line")
        self.assertTrue(self.has_label(7, "in-progress"), "escalate touched labels before the comment landed")
        self.assertFalse(self.has_label(7, "needs-decision"), "escalate added needs-decision despite comment failure")

        # escalate: label swap fails -> comment landed, task still held by in-progress.
        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", q, fail="issue edit")
        self.assertEqual(r.rc, 20, "escalate exit on label failure")
        self.assertEqual(r.out, "escalate: gh-failure issue=7 stage=labels", "escalate label-failure line")
        self.assertTrue(self.has_label(7, "in-progress"), "task lost in-progress on a failed swap")

        # deliver: label swap fails -> result recorded, task still held by in-progress.
        self.mk_issue(8, "shipped task", "kraken-task", "project:app", "in-progress")
        rf = os.path.join(self.state, "r.md")
        self._write(rf, "done, validated\n")
        r = self.kraken("deliver", "OWNER/tasks", 8, "w1", rf, "https://x/pr/1", fail="issue edit")
        self.assertEqual(r.rc, 20, "deliver exit on label failure")
        self.assertEqual(r.out, "deliver: gh-failure issue=8 stage=labels", "deliver label-failure line")
        self.assertTrue(self.has_label(8, "in-progress"), "task lost in-progress on a failed swap")
        self.assertFalse(self.has_label(8, "awaiting-merge"), "deliver added awaiting-merge despite swap failure")

        # heartbeat: comment fails -> 20, nothing else to roll back.
        r = self.kraken("heartbeat", "OWNER/tasks", 8, "w1", "still here", fail="issue comment")
        self.assertEqual(r.rc, 20, "heartbeat exit on comment failure")
        self.assertEqual(r.out, "heartbeat: gh-failure issue=8", "heartbeat failure line")


if __name__ == "__main__":
    unittest.main()
