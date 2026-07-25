#!/usr/bin/env python3
"""The reconcile pass run STAND-ALONE (kraken.py reap) — the operator-side escape
hatch for what a drain does on its own read (that path is pinned in
test_34_claim_next_reconcile.py). It makes the claim refs (the lock) and the
in-progress labels (the projection) agree.

Under protocol/6 an expired LEASE is not one of its jobs: expiry is applied by
whoever reads the queue and the next claim steals it, so the pass writes nothing
for one. What survives as rule 2 is the task that keeps expiring — after
LEASE_EXPIRY_ESCALATE steals it stops circulating and becomes the operator's
call."""
import unittest

from harness import KrakenConformanceTest, expiry_marker, lease_ttl
from kraken import LEASE_EXPIRY_ESCALATE


class ReclaimStaleTests(KrakenConformanceTest):
    def test_reconciler_rules(self):
        env = {"REPO": "OWNER/tasks"}

        # #1 UNFINISHABLE — lease expired, and the thread already records
        #    LEASE_EXPIRY_ESCALATE steals. Rule 2: reclaim + delete ref.
        self.mk_issue(1, "nobody can finish this", "kraken-task", "project:app",
                      "in-progress")
        self.mk_expired_lease(1, "dead-worker")
        for i in range(LEASE_EXPIRY_ESCALATE):
            self.mk_comment(1, expiry_marker("w%d" % i))

        # #2 ALIVE — lease renewed just now. Untouched.
        self.mk_issue(2, "live worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "live-worker", age_seconds=5, mtype="heartbeat", msg="still going")

        # #3 ORPHAN PROJECTION — in-progress label, NO ref. Rule 4: requeue.
        self.mk_issue(3, "crashed release", "kraken-task", "project:app", "in-progress")

        # #4 ORPHAN LOCK — a ref left behind on an escalated (needs-decision)
        #    issue: a crashed escalate. Rule 1: delete the ref, touch nothing else.
        self.mk_issue(4, "escalated but ref lingered", "kraken-task", "project:app", "needs-decision")
        self.mk_claim_ref(4, "gone-worker", age_seconds=60)

        # #5 HEAL — a fresh ref whose in-progress projection never landed. Rule 3.
        self.mk_issue(5, "label projection crashed", "kraken-task", "project:app")
        self.mk_claim_ref(5, "healthy-worker", age_seconds=5)

        # #6 ORPHAN LOCK ON CLOSED — a ref outlived the close. Rule 1: delete it.
        self.mk_issue(6, "closed with a lingering ref", "kraken-task", "project:app")
        self.set_issue_state(6, "closed")
        self.mk_claim_ref(6, "old-worker", age_seconds=120)

        # #7 EXPIRED, FIRST TIME — the protocol/6 case: expiry alone is not the
        #    reconciler's business. Left exactly as it is, for the thief.
        self.mk_issue(7, "silent worker", "kraken-task", "project:app", "in-progress")
        expired_sha = self.mk_expired_lease(7, "silent-worker")

        r = self.kraken("reap", "OWNER/tasks", env=env)
        self.assertEqual(r.rc, 0, "reap run: %s" % r.err)

        # #1 reclaimed: needs-decision, ref gone, stale-claim marker posted.
        self.assertTrue(self.has_label(1, "needs-decision"), "#1 (unfinishable) not moved to needs-decision")
        self.assertFalse(self.has_label(1, "in-progress"), "#1 (unfinishable) still in-progress")
        self.assertFalse(self.claim_ref_exists(1), "#1 (unfinishable) claim ref not deleted")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(1), "#1 missing stale-claim marker")

        # #2 untouched: still in-progress, ref intact, no comment.
        self.assertTrue(self.has_label(2, "in-progress"), "#2 (live) was reclaimed despite a fresh renewal")
        self.assertFalse(self.has_label(2, "needs-decision"), "#2 (live) wrongly moved to needs-decision")
        self.assertTrue(self.claim_ref_exists(2), "#2 (live) claim ref wrongly deleted")
        self.assertEqual(self.comment_count(2), 0, "#2 got a comment it should not have")

        # #7 left alone: an expired lease costs the reconciler ZERO writes.
        self.assertTrue(self.has_label(7, "in-progress"), "#7 (expired) lost its label to reap")
        self.assertFalse(self.has_label(7, "needs-decision"), "#7 (expired) was escalated on a first expiry")
        self.assertEqual(self.claim_ref(7), expired_sha, "#7 (expired) ref was touched")
        self.assertEqual(self.comment_count(7), 0, "#7 (expired) got a comment")

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
