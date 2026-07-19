#!/usr/bin/env python3
"""The reconciler (kraken.py reap) makes the claim refs (the lock) and the
in-progress labels (the projection) agree. Staleness is anchored to the claim
ref's commit date — nothing on the issue timeline resets it — and the four
rules (reclaim stale, delete orphan lock, heal missing label, requeue orphan
projection) each get a scenario. Drives the real kraken.py reap through the
gh-stub."""
import unittest

from harness import KrakenConformanceTest


class ReclaimStaleTests(KrakenConformanceTest):
    def test_reconciler_rules(self):
        env = {"REPO": "OWNER/tasks", "MAX_HOURS": "6"}

        # #1 DEAD — claim ref 8h old, in-progress. Rule 2: reclaim + delete ref.
        self.mk_issue(1, "dead worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(1, "dead-worker", age_hours=8)

        # #2 ALIVE — claim ref heartbeated 30m ago. Untouched.
        self.mk_issue(2, "live worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "live-worker", age_hours=0, mtype="heartbeat", msg="still going")

        # #3 ORPHAN PROJECTION — in-progress label, NO ref. Rule 4: requeue.
        self.mk_issue(3, "crashed release", "kraken-task", "project:app", "in-progress")

        # #4 ORPHAN LOCK — a ref left behind on an escalated (needs-decision)
        #    issue: a crashed escalate. Rule 1: delete the ref, touch nothing else.
        self.mk_issue(4, "escalated but ref lingered", "kraken-task", "project:app", "needs-decision")
        self.mk_claim_ref(4, "gone-worker", age_hours=1)

        # #5 HEAL — a fresh ref whose in-progress projection never landed. Rule 3.
        self.mk_issue(5, "label projection crashed", "kraken-task", "project:app")
        self.mk_claim_ref(5, "healthy-worker", age_hours=0)

        # #6 ORPHAN LOCK ON CLOSED — a ref outlived the close. Rule 1: delete it.
        self.mk_issue(6, "closed with a lingering ref", "kraken-task", "project:app")
        self.set_issue_state(6, "closed")
        self.mk_claim_ref(6, "old-worker", age_hours=2)

        r = self.kraken("reap", "OWNER/tasks", env=env)
        self.assertEqual(r.rc, 0, "reap run: %s" % r.err)

        # #1 reclaimed: needs-decision, ref gone, stale-claim marker posted.
        self.assertTrue(self.has_label(1, "needs-decision"), "#1 (dead) not moved to needs-decision")
        self.assertFalse(self.has_label(1, "in-progress"), "#1 (dead) still in-progress")
        self.assertFalse(self.claim_ref_exists(1), "#1 (dead) claim ref not deleted")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(1), "#1 missing stale-claim marker")

        # #2 untouched: still in-progress, ref intact, no comment.
        self.assertTrue(self.has_label(2, "in-progress"), "#2 (live) was reclaimed despite a fresh heartbeat")
        self.assertFalse(self.has_label(2, "needs-decision"), "#2 (live) wrongly moved to needs-decision")
        self.assertTrue(self.claim_ref_exists(2), "#2 (live) claim ref wrongly deleted")
        self.assertEqual(self.comment_count(2), 0, "#2 got a comment it should not have")

        # #3 requeued: in-progress removed, a bot note posted.
        self.assertFalse(self.has_label(3, "in-progress"), "#3 (orphan projection) still in-progress")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(3), "#3 missing requeue note")

        # #4 orphan lock: ref deleted, needs-decision label untouched, no comment.
        self.assertFalse(self.claim_ref_exists(4), "#4 (orphan lock) ref not deleted")
        self.assertTrue(self.has_label(4, "needs-decision"), "#4 lost its needs-decision label")
        self.assertEqual(self.comment_count(4), 0, "#4 (orphan lock) got a spurious comment")

        # #5 healed: in-progress restored, ref intact, no comment.
        self.assertTrue(self.has_label(5, "in-progress"), "#5 (heal) label not restored")
        self.assertTrue(self.claim_ref_exists(5), "#5 (heal) ref wrongly deleted")
        self.assertEqual(self.comment_count(5), 0, "#5 (heal) got a spurious comment")

        # #6 orphan lock on a closed issue: ref deleted.
        self.assertFalse(self.claim_ref_exists(6), "#6 (closed) ref not deleted")


if __name__ == "__main__":
    unittest.main()
