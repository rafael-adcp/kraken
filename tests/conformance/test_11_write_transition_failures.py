#!/usr/bin/env python3
"""gh failures in the write transitions: exit 20 at every stage, and the
ordering (comment -> label -> ref delete, lock last) means a half-executed
transition always leaves the task held — never a state the labels/refs can't
explain. Heartbeat is a claim-ref advance (commit -> ref PATCH), not a comment."""
import os
import unittest

from harness import KrakenConformanceTest


class WriteTransitionFailureTests(KrakenConformanceTest):
    def test_write_transition_failures(self):
        # escalate: comment fails -> nothing changed.
        self.mk_issue(7, "blocked task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(7, "w1")
        q = os.path.join(self.state, "q.md")
        self._write(q, "which way?\n")
        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", q, fail="issue comment")
        self.assertEqual(r.rc, 20, "escalate exit on comment failure")
        self.assertEqual(r.out, "escalate: gh-failure issue=7 stage=comment", "escalate failure line")
        self.assertTrue(self.has_label(7, "in-progress"), "escalate touched labels before the comment landed")
        self.assertFalse(self.has_label(7, "needs-decision"), "escalate added needs-decision despite comment failure")
        self.assertTrue(self.claim_ref_exists(7), "escalate freed the lock despite comment failure")

        # escalate: label swap fails -> comment landed, task still held by in-progress.
        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", q, fail="issue edit")
        self.assertEqual(r.rc, 20, "escalate exit on label failure")
        self.assertEqual(r.out, "escalate: gh-failure issue=7 stage=labels", "escalate label-failure line")
        self.assertTrue(self.has_label(7, "in-progress"), "task lost in-progress on a failed swap")
        self.assertTrue(self.claim_ref_exists(7), "escalate freed the lock despite label failure")

        # escalate: ref delete fails -> the lock outlives a half-release; the
        # reaper's orphan-lock rule (needs-decision + a live ref) mops it up.
        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", q, fail="method DELETE")
        self.assertEqual(r.rc, 20, "escalate exit on ref-delete failure")
        self.assertEqual(r.out, "escalate: gh-failure issue=7 stage=ref", "escalate ref-failure line")
        self.assertTrue(self.claim_ref_exists(7), "ref should survive its own failed delete")

        # deliver: label swap fails -> result recorded, task still held by in-progress.
        self.mk_issue(8, "shipped task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(8, "w1")
        rf = os.path.join(self.state, "r.md")
        self._write(rf, "done, validated\n")
        r = self.kraken("deliver", "OWNER/tasks", 8, "w1", rf, "https://x/pr/1", fail="issue edit")
        self.assertEqual(r.rc, 20, "deliver exit on label failure")
        self.assertEqual(r.out, "deliver: gh-failure issue=8 stage=labels", "deliver label-failure line")
        self.assertTrue(self.has_label(8, "in-progress"), "task lost in-progress on a failed swap")
        self.assertFalse(self.has_label(8, "awaiting-merge"), "deliver added awaiting-merge despite swap failure")
        self.assertTrue(self.claim_ref_exists(8), "deliver freed the lock despite label failure")

        # heartbeat: the claim-commit POST fails -> 20, ref untouched.
        r = self.kraken("heartbeat", "OWNER/tasks", 8, "w1", "still here", fail="git/commits")
        self.assertEqual(r.rc, 20, "heartbeat exit on commit failure")
        self.assertEqual(r.out, "heartbeat: gh-failure issue=8 stage=commit", "heartbeat commit-failure line")

        # heartbeat: the ref PATCH fails -> 20 (the commit was made, the ref did
        # not move; the old anchor stands, which is the safe direction).
        r = self.kraken("heartbeat", "OWNER/tasks", 8, "w1", "still here", fail="method PATCH")
        self.assertEqual(r.rc, 20, "heartbeat exit on ref failure")
        self.assertEqual(r.out, "heartbeat: gh-failure issue=8 stage=ref", "heartbeat ref-failure line")


if __name__ == "__main__":
    unittest.main()
