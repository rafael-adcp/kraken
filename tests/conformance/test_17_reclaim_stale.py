#!/usr/bin/env python3
"""The reaper anchors staleness to the worker's last liveness marker (the newest
claim/heartbeat marker), not the issue's updatedAt. An operator comment on a
dead worker's issue must NOT keep the claim alive. Drives kraken.py reap against
the gh-stub."""
import unittest

from harness import KrakenConformanceTest, ago_iso


class ReclaimStaleTests(KrakenConformanceTest):
    def test_reap(self):
        env = {"REPO": "OWNER/tasks", "MAX_HOURS": "6"}

        # #1 — DEAD: claimed 8h ago, operator commented 10m ago. Anchored to the
        # last liveness marker it is 8h silent and MUST be reclaimed. The claim
        # body carries the real shape (disclaimer + blank line + marker).
        self.mk_issue(1, "dead worker, operator poked it", "kraken-task", "project:app", "in-progress")
        self.mk_comment(1, '> disclaimer\n\n<!-- kraken {"type":"claim","worker":"dead-worker"} -->\n',
                        ago_iso(8))
        self.mk_comment(1, "any update here? — the operator", ago_iso(0))

        # #2 — ALIVE: a fresh heartbeat 30m ago is inside the window.
        self.mk_issue(2, "live worker heartbeating", "kraken-task", "project:app", "in-progress")
        self.mk_comment(2, '<!-- kraken {"type":"claim","worker":"live-worker"} -->', ago_iso(9))
        self.mk_comment(2, '<!-- kraken {"type":"heartbeat","worker":"live-worker"} -->', ago_iso(0))

        # #3 — MALFORMED: in-progress but no worker liveness marker at all.
        self.mk_issue(3, "in-progress, worker never spoke", "kraken-task", "project:app", "in-progress")
        self.mk_comment(3, "someone mislabeled this — the operator", ago_iso(0))

        # #4 — ALIVE PAST THE 100-COMMENT BOUNDARY.
        self.mk_issue(4, "live worker heartbeating past comment 100", "kraken-task", "project:app", "in-progress")
        self.mk_comment(4, '<!-- kraken {"type":"claim","worker":"marathon-worker"} -->', ago_iso(9))
        for i in range(1, 101):
            self.mk_comment(4, "noise %d" % i, ago_iso(1))
        self.mk_comment(4, '<!-- kraken {"type":"heartbeat","worker":"marathon-worker"} -->', ago_iso(0))

        # #5 — STALE PAST THE 100-COMMENT BOUNDARY.
        self.mk_issue(5, "dead worker, long noisy thread", "kraken-task", "project:app", "in-progress")
        self.mk_comment(5, '<!-- kraken {"type":"claim","worker":"lost-worker"} -->', ago_iso(8))
        for i in range(1, 106):
            self.mk_comment(5, "someone keeps commenting %d — the operator" % i, ago_iso(0))

        r = self.kraken("reap", "OWNER/tasks", env=env)
        self.assertEqual(r.rc, 0, "reap run")

        # #1 reclaimed.
        self.assertTrue(self.has_label(1, "needs-decision"), "#1 (dead) not moved to needs-decision")
        self.assertFalse(self.has_label(1, "in-progress"), "#1 (dead) still in-progress after reaping")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(1), "#1 missing stale-claim marker")

        # #2 untouched.
        self.assertTrue(self.has_label(2, "in-progress"), "#2 (live) was reclaimed despite a fresh heartbeat")
        self.assertFalse(self.has_label(2, "needs-decision"), "#2 (live) wrongly moved to needs-decision")
        self.assertEqual(self.comment_count(2), 2, "#2 got a stale-claim comment it should not have")

        # #3 reclaimed.
        self.assertTrue(self.has_label(3, "needs-decision"), "#3 (no machine line) not reclaimed")
        self.assertFalse(self.has_label(3, "in-progress"), "#3 (no machine line) still in-progress")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(3), "#3 missing stale-claim marker")

        # #4 untouched.
        self.assertTrue(self.has_label(4, "in-progress"), "#4 (live past boundary) reclaimed")
        self.assertFalse(self.has_label(4, "needs-decision"), "#4 (live past boundary) wrongly moved")
        self.assertNotIn('<!-- kraken {"type":"stale-claim"', self.last_comment(4),
                         "#4 got a stale-claim comment it should not have")

        # #5 reclaimed.
        self.assertTrue(self.has_label(5, "needs-decision"), "#5 (dead past boundary) not reclaimed")
        self.assertFalse(self.has_label(5, "in-progress"), "#5 (dead past boundary) still in-progress")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(5), "#5 missing stale-claim marker")


if __name__ == "__main__":
    unittest.main()
